import logging
import time
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


def _persist_classification_failure(payment_id: str, error_detail: str) -> None:
    with get_db_cursor() as cur:
        cur.execute(
            """
            UPDATE payments
            SET classification_status = 'failed',
                classification_error = %s,
                classification_attempts = classification_attempts + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE payment_id = %s
              AND status <> 'captured'
              AND classification_status <> 'classified'
            """,
            (error_detail, payment_id),
        )


def classify_and_schedule(payment_id: str) -> Tuple[str, Optional[str]]:
    """Classify one failed payment and durably create its deterministic actions."""
    started = time.monotonic()
    category: Optional[str] = None
    confidence: Optional[float] = None
    error_msg: Optional[str] = None

    try:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT status,
                       classification_status,
                       failure_category,
                       error_code,
                       error_reason,
                       error_source,
                       error_step,
                       failed_at
                FROM payments
                WHERE payment_id = %s
                """,
                (payment_id,),
            )
            row = cur.fetchone()

        if not row:
            logger.warning("Payment %s not found for classification", payment_id)
            return "not_found", None

        (
            status,
            classification_status,
            existing_category,
            error_code,
            error_reason,
            error_source,
            error_step,
            failed_at,
        ) = row

        if status == "captured":
            return "skipped", None

        if classification_status == "classified":
            category = existing_category
            return "classified", existing_category

        if failed_at is None:
            message = "Payment cannot be scheduled because failed_at is NULL"
            _persist_classification_failure(payment_id, message)
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
            _persist_classification_failure(payment_id, error_msg)
            if isinstance(exc, ClassificationError):
                raise
            raise ClassificationError(f"Classification failed: {exc}") from exc

        category = classification.category
        confidence = classification.confidence
        reason = classification.reason

        if category not in VALID_CATEGORIES:
            message = f"Classifier returned unsupported category: {category!r}"
            _persist_classification_failure(payment_id, message)
            raise ClassificationError(message)

        with get_db_cursor() as cur:
            # Re-read while locked. A capture webhook may have arrived while the LLM ran.
            cur.execute(
                """
                SELECT status, classification_status, failure_category, failed_at
                FROM payments
                WHERE payment_id = %s
                FOR UPDATE
                """,
                (payment_id,),
            )
            current = cur.fetchone()
            if not current:
                return "not_found", None

            current_status, current_classification_status, current_category, current_failed_at = current
            if current_status == "captured":
                return "skipped", None
            if current_classification_status == "classified":
                return "classified", current_category
            if current_failed_at is None:
                message = "Payment cannot be scheduled because failed_at is NULL"
                cur.execute(
                    """
                    UPDATE payments
                    SET classification_status = 'failed',
                        classification_error = %s,
                        classification_attempts = classification_attempts + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE payment_id = %s
                    """,
                    (message, payment_id),
                )
                raise ClassificationError(message)

            cur.execute(
                """
                UPDATE payments
                SET failure_category = %s,
                    classification_confidence = %s,
                    classification_reason = %s,
                    classification_status = 'classified',
                    classification_error = NULL,
                    classification_attempts = classification_attempts + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE payment_id = %s
                """,
                (category, confidence, reason, payment_id),
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
                    run_at = current_failed_at + timedelta(
                        minutes=step.get("delay_minutes", 0)
                    )
                    run_at = adjust_for_quiet_hours(run_at)
                    action_id = generate_action_id(
                        payment_id,
                        step["action_type"],
                        step["step"],
                    )
                    cur.execute(
                        """
                        INSERT INTO scheduled_actions (
                            action_id, payment_id, action_type, step, run_at, status, recheck_count
                        )
                        VALUES (%s, %s, %s, %s, %s, 'pending', 0)
                        ON CONFLICT (payment_id, action_type, step) DO NOTHING
                        """,
                        (
                            action_id,
                            payment_id,
                            step["action_type"],
                            step["step"],
                            run_at,
                        ),
                    )

            if category == "other":
                action_id = generate_action_id(payment_id, "manual_review", 1)
                cur.execute(
                    """
                    INSERT INTO scheduled_actions (
                        action_id, payment_id, action_type, step, run_at, status, recheck_count
                    )
                    VALUES (%s, %s, 'manual_review', 1, CURRENT_TIMESTAMP, 'manual_review', 0)
                    ON CONFLICT (payment_id, action_type, step) DO NOTHING
                    """,
                    (action_id, payment_id),
                )
                logger.info(
                    "Manual review required for %s. Razorpay errors: code=%s reason=%s source=%s step=%s",
                    payment_id,
                    error_code,
                    error_reason,
                    error_source,
                    error_step,
                )

        return "classified", category

    except ClassificationError as exc:
        error_msg = str(exc)
        raise
    except Exception:
        logger.exception("Unexpected error classifying payment %s", payment_id)
        raise
    finally:
        latency_ms = (time.monotonic() - started) * 1000
        logger.info(
            "classification_run | payment_id=%s | status=%s | category=%s | confidence=%s | latency=%.2fms | error=%s",
            payment_id,
            "classified" if category else "failed",
            category,
            confidence,
            latency_ms,
            error_msg,
        )
