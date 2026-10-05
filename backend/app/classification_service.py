import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from backend.app.classifier import ClassificationError, classify_failure
from backend.app.config import settings
from backend.app.db import get_db_cursor
from backend.app.policy import adjust_for_quiet_hours, assign_group, get_policy

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {
    "insufficient_funds",
    "bank_declined_soft",
    "bank_declined_hard",
    "timeout",
    "user_cancelled",
    "other",
}


def generate_action_id(payment_id: str, action_type: str, step: int) -> str:
    return f"act_{payment_id}_{action_type}_{step}"


def _persist_classification_failure(
    payment_id: str,
    lease_id: str,
    error_detail: str,
) -> None:
    """Release a classification lease only when this worker still owns it."""
    with get_db_cursor() as cur:
        cur.execute(
            """
            UPDATE payments
            SET classification_status = 'failed',
                classification_error = %s,
                classification_attempts = classification_attempts + 1,
                classification_lease_id = NULL,
                classification_lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE payment_id = %s
              AND status <> 'captured'
              AND classification_status = 'processing'
              AND classification_lease_id = %s
            """,
            (error_detail[:4000], payment_id, lease_id),
        )


def _claim_classification(payment_id: str) -> Optional[dict]:
    """Atomically claim one payment for classification without holding a DB lock during LLM I/O."""
    lease_id = uuid.uuid4().hex
    lease_expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.STUCK_CLASSIFICATION_MINUTES
    )

    with get_db_cursor() as cur:
        cur.execute(
            """
            UPDATE payments
            SET classification_status = 'processing',
                classification_lease_id = %s,
                classification_lease_expires_at = %s,
                classification_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE payment_id = %s
              AND status <> 'captured'
              AND classification_attempts < %s
              AND (
                    classification_status IN ('pending', 'failed')
                    OR (
                        classification_status = 'processing'
                        AND (
                            classification_lease_expires_at IS NULL
                            OR classification_lease_expires_at <= CURRENT_TIMESTAMP
                        )
                    )
                  )
            RETURNING
                status,
                failure_category,
                error_code,
                error_reason,
                error_source,
                error_step,
                failed_at,
                classification_attempts
            """,
            (
                lease_id,
                lease_expires_at,
                payment_id,
                settings.MAX_CLASSIFICATION_RETRIES,
            ),
        )
        row = cur.fetchone()

    if not row:
        return None

    (
        status,
        existing_category,
        error_code,
        error_reason,
        error_source,
        error_step,
        failed_at,
        classification_attempts,
    ) = row

    return {
        "lease_id": lease_id,
        "status": status,
        "existing_category": existing_category,
        "error_code": error_code,
        "error_reason": error_reason,
        "error_source": error_source,
        "error_step": error_step,
        "failed_at": failed_at,
        "classification_attempts": int(classification_attempts or 0),
    }


def _read_classification_state(payment_id: str) -> Tuple[str, Optional[str], int]:
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT
                status,
                classification_status,
                failure_category,
                classification_attempts
            FROM payments
            WHERE payment_id = %s
            """,
            (payment_id,),
        )
        row = cur.fetchone()

    if not row:
        return "not_found", None, 0

    status, classification_status, category, attempts = row
    if status == "captured":
        return "skipped", category, int(attempts or 0)
    if classification_status == "classified":
        return "classified", category, int(attempts or 0)
    if classification_status == "processing":
        return "in_progress", category, int(attempts or 0)
    if int(attempts or 0) >= settings.MAX_CLASSIFICATION_RETRIES:
        return "retry_exhausted", None, int(attempts or 0)
    return "pending", category, int(attempts or 0)


def classify_and_schedule(payment_id: str) -> Tuple[str, Optional[str]]:
    """Classify a failed payment asynchronously and create deterministic actions."""
    started = time.monotonic()
    category: Optional[str] = None
    confidence: Optional[float] = None
    error_msg: Optional[str] = None
    claim: Optional[dict] = None

    try:
        state, existing_category, _ = _read_classification_state(payment_id)
        if state != "pending":
            if state == "not_found":
                logger.warning("Payment %s not found for classification", payment_id)
            return state, existing_category

        claim = _claim_classification(payment_id)
        if claim is None:
            state, existing_category, _ = _read_classification_state(payment_id)
            if state == "not_found":
                logger.warning("Payment %s disappeared during classification claim", payment_id)
            return state, existing_category

        lease_id = claim["lease_id"]
        error_code = claim["error_code"]
        error_reason = claim["error_reason"]
        error_source = claim["error_source"]
        error_step = claim["error_step"]
        failed_at = claim["failed_at"]

        if failed_at is None:
            message = "Payment cannot be scheduled because failed_at is NULL"
            _persist_classification_failure(payment_id, lease_id, message)
            raise ClassificationError(message)

        try:
            classification = classify_failure(
                error_code,
                error_reason,
                error_source,
                error_step,
            )
        except Exception as exc:
            error_msg = str(exc)
            _persist_classification_failure(payment_id, lease_id, error_msg)
            if isinstance(exc, ClassificationError):
                raise
            raise ClassificationError(f"Classification failed: {exc}") from exc

        category = classification.category
        confidence = classification.confidence
        reason = classification.reason

        if category not in VALID_CATEGORIES:
            message = f"Classifier returned unsupported category: {category!r}"
            _persist_classification_failure(payment_id, lease_id, message)
            raise ClassificationError(message)

        if not 0.0 <= float(confidence) <= 1.0:
            message = f"Classifier returned invalid confidence: {confidence!r}"
            _persist_classification_failure(payment_id, lease_id, message)
            raise ClassificationError(message)

        if not str(reason or "").strip():
            message = "Classifier returned an empty reason"
            _persist_classification_failure(payment_id, lease_id, message)
            raise ClassificationError(message)

        with get_db_cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                SET failure_category = %s,
                    classification_confidence = %s,
                    classification_reason = %s,
                    classification_status = 'classified',
                    classification_error = NULL,
                    classification_attempts = classification_attempts + 1,
                    classification_lease_id = NULL,
                    classification_lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE payment_id = %s
                  AND status <> 'captured'
                  AND classification_status = 'processing'
                  AND classification_lease_id = %s
                RETURNING failed_at
                """,
                (
                    category,
                    float(confidence),
                    str(reason).strip(),
                    payment_id,
                    lease_id,
                ),
            )
            finalized = cur.fetchone()
            if not finalized:
                state, existing_category, _ = _read_classification_state(payment_id)
                logger.info(
                    "Classification claim lost for %s; state=%s category=%s",
                    payment_id,
                    state,
                    existing_category,
                )
                return state, existing_category

            current_failed_at = finalized[0]
            if current_failed_at is None:
                raise ClassificationError(
                    "Payment cannot be scheduled because failed_at is NULL"
                )

            group = assign_group(payment_id, category)
            cur.execute(
                """
                UPDATE payments
                SET recovery_group = %s,
                    recovery_status = CASE
                        WHEN recovery_status = 'recovered' THEN recovery_status
                        ELSE 'open'
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE payment_id = %s
                """,
                (group, payment_id),
            )

            if group == "treatment":
                for step in get_policy(category):
                    if step["action_type"] == "manual_review":
                        continue
                    run_at = adjust_for_quiet_hours(
                        current_failed_at
                        + timedelta(minutes=int(step["delay_minutes"]))
                    )
                    action_id = generate_action_id(
                        payment_id,
                        step["action_type"],
                        int(step["step"]),
                    )
                    cur.execute(
                        """
                        INSERT INTO scheduled_actions (
                            action_id,
                            payment_id,
                            action_type,
                            step,
                            run_at,
                            status,
                            recheck_count
                        )
                        VALUES (%s, %s, %s, %s, %s, 'pending', 0)
                        ON CONFLICT (payment_id, action_type, step) DO NOTHING
                        """,
                        (
                            action_id,
                            payment_id,
                            step["action_type"],
                            int(step["step"]),
                            run_at,
                        ),
                    )

            if category == "other":
                action_id = generate_action_id(payment_id, "manual_review", 1)
                cur.execute(
                    """
                    INSERT INTO scheduled_actions (
                        action_id,
                        payment_id,
                        action_type,
                        step,
                        run_at,
                        status,
                        recheck_count
                    )
                    VALUES (%s, %s, 'manual_review', 1, CURRENT_TIMESTAMP, 'manual_review', 0)
                    ON CONFLICT (payment_id, action_type, step) DO NOTHING
                    """,
                    (action_id, payment_id),
                )
                logger.warning(
                    "Manual review required for payment %s: code=%s reason=%s source=%s step=%s",
                    payment_id,
                    error_code,
                    error_reason,
                    error_source,
                    error_step,
                )

        return "classified", category

    except ClassificationError:
        raise
    except Exception as exc:
        error_msg = str(exc)
        logger.exception("Unexpected error classifying payment %s", payment_id)
        if claim is not None:
            _persist_classification_failure(payment_id, claim["lease_id"], error_msg)
        raise
    finally:
        latency_ms = (time.monotonic() - started) * 1000
        logger.info(
            "classification_run | payment_id=%s | category=%s | confidence=%s | latency=%.2fms | error=%s",
            payment_id,
            category,
            confidence,
            latency_ms,
            error_msg,
        )
