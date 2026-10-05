import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from backend.app import policy
from backend.app.actions_repo import ActionsRepository, ClaimError
from backend.app.config import settings
from backend.app.db import get_db_cursor
from backend.app.messaging import send_recovery_email
from backend.app.razorpay_client import (
    PermanentProviderError,
    RazorpayClient,
    TemporaryProviderError,
    payment_link_reference_id,
)

logger = logging.getLogger(__name__)
actions_repo = ActionsRepository()
razorpay_client = RazorpayClient()

SUCCESSFUL_RECOVERY_ATTEMPT_STATUSES = {"sent", "recovered"}
UNRESOLVED_TIMEOUT_STATUSES = {"pending", "created", "authorized"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_recovery_attempt(cur, action_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT id, action_id, original_payment_id, link_id, link_url,
               channel, recovery_group, delay_used, sent_at, status,
               recovered_payment_id, recovered_at
        FROM recovery_attempts
        WHERE action_id = %s
        LIMIT 1
        """,
        (action_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def _count_sent_messages(cur, payment_id: str) -> int:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM recovery_attempts
        WHERE original_payment_id = %s
          AND channel = 'email'
          AND sent_at IS NOT NULL
          AND status IN ('sent', 'recovered')
        """,
        (payment_id,),
    )
    return int(cur.fetchone()[0] or 0)


def _delay_used_minutes(category: Optional[str], action_type: str, step: int) -> int:
    return policy.delay_for_step(category, action_type, step)


def _upsert_link_attempt(
    cur,
    *,
    action_id: str,
    payment_id: str,
    link_id: str,
    link_url: str,
    recovery_group: Optional[str],
    delay_used: timedelta,
) -> Dict[str, Any]:
    attempt_id = f"rat_{uuid.uuid4().hex}"
    cur.execute(
        """
        INSERT INTO recovery_attempts (
            id,
            action_id,
            original_payment_id,
            link_id,
            link_url,
            channel,
            recovery_group,
            delay_used,
            sent_at,
            status
        )
        VALUES (%s, %s, %s, %s, %s, 'email', %s, %s, NULL, 'link_created')
        ON CONFLICT (action_id) DO UPDATE SET
            link_id = COALESCE(recovery_attempts.link_id, EXCLUDED.link_id),
            link_url = COALESCE(recovery_attempts.link_url, EXCLUDED.link_url),
            channel = COALESCE(recovery_attempts.channel, EXCLUDED.channel),
            recovery_group = COALESCE(
                recovery_attempts.recovery_group,
                EXCLUDED.recovery_group
            ),
            delay_used = COALESCE(
                recovery_attempts.delay_used,
                EXCLUDED.delay_used
            )
        """,
        (
            attempt_id,
            action_id,
            payment_id,
            link_id,
            link_url,
            recovery_group,
            delay_used,
        ),
    )
    attempt = _get_recovery_attempt(cur, action_id)
    if not attempt:
        raise RuntimeError(f"Recovery attempt missing after persistence: {action_id}")
    return attempt


def _mark_attempt_email_failed(action_id: str, error: str) -> None:
    with get_db_cursor() as cur:
        cur.execute(
            """
            UPDATE recovery_attempts
            SET status = CASE
                    WHEN status IN ('sent', 'recovered') THEN status
                    ELSE 'email_failed'
                END
            WHERE action_id = %s
              AND status NOT IN ('sent', 'recovered')
            """,
            (action_id,),
        )


def _mark_attempt_sent(cur, action_id: str, sent_at: datetime) -> bool:
    cur.execute(
        """
        UPDATE recovery_attempts
        SET status = 'sent',
            channel = 'email',
            sent_at = COALESCE(sent_at, %s)
        WHERE action_id = %s
          AND status IN ('link_created', 'email_failed')
          AND sent_at IS NULL
        RETURNING id
        """,
        (sent_at, action_id),
    )
    if cur.fetchone():
        return True

    cur.execute(
        """
        SELECT 1
        FROM recovery_attempts
        WHERE action_id = %s
          AND status IN ('sent', 'recovered')
          AND sent_at IS NOT NULL
        """,
        (action_id,),
    )
    return cur.fetchone() is not None


def _fresh_payment_state(payment_id: str):
    payment = razorpay_client.fetch_payment(payment_id)
    order_paid = (
        razorpay_client.is_order_paid(payment.order_id)
        if payment.order_id
        else False
    )
    return payment, order_paid


def _is_successfully_paid(rp_payment, order_paid: bool) -> bool:
    return getattr(rp_payment, "status", None) == "captured" or order_paid


def _safe_reschedule(
    action_id: str,
    worker_id: str,
    new_run_at: datetime,
    reason: str,
    *,
    increment_recheck: bool = False,
) -> bool:
    try:
        return actions_repo.reschedule_action(
            action_id,
            worker_id,
            policy.adjust_for_quiet_hours(new_run_at),
            reason,
            increment_recheck=increment_recheck,
        )
    except ClaimError:
        logger.warning("Lost claim while rescheduling action %s", action_id)
        return False


def _safe_fail(action_id: str, worker_id: str, reason: str) -> bool:
    try:
        return actions_repo.fail_action(action_id, worker_id, reason)
    except ClaimError:
        logger.warning("Lost claim while failing action %s", action_id)
        return False


def _safe_cancel(action_id: str, worker_id: str, reason: str) -> bool:
    try:
        return actions_repo.cancel_action(action_id, worker_id, reason)
    except ClaimError:
        logger.info("Action %s was already transitioned by another owner", action_id)
        return False


def run_action(action_id: str, worker_id: str) -> str:
    """Execute one claimed recovery action with durable state and provider idempotency."""
    started = time.monotonic()
    payment_id: Optional[str] = None
    action_type = "unknown"
    attempt_num = 0

    try:
        if not actions_repo.still_owns_claim(action_id, worker_id):
            return "claim_lost"

        action, payment = actions_repo.get_action_and_payment(action_id)
        payment_id = payment["payment_id"]
        action_type = action["action_type"]
        attempt_num = int(action.get("attempts") or 0)

        if not actions_repo.renew_claim(action_id, worker_id):
            return "claim_lost"

        rp_payment, order_paid = _fresh_payment_state(payment_id)
        if _is_successfully_paid(rp_payment, order_paid):
            _safe_cancel(action_id, worker_id, "already_paid")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "already_paid")
            return "cancelled_paid"

        now = _utc_now()

        if payment.get("failure_category") == "timeout":
            provider_status = getattr(rp_payment, "status", None)
            if provider_status in UNRESOLVED_TIMEOUT_STATUSES:
                recheck_count = int(action.get("recheck_count") or 0)
                next_run = policy.next_timeout_recheck(now, recheck_count)
                if next_run is None:
                    _safe_cancel(action_id, worker_id, "status_unresolved")
                    _log_run(
                        action_id,
                        payment_id,
                        action_type,
                        attempt_num,
                        "cancelled",
                        "status_unresolved",
                    )
                    return "cancelled_unresolved"

                if _safe_reschedule(
                    action_id,
                    worker_id,
                    next_run,
                    "still_unresolved",
                    increment_recheck=True,
                ):
                    _log_run(
                        action_id,
                        payment_id,
                        action_type,
                        attempt_num,
                        "rescheduled",
                        "timeout_pending",
                    )
                    return "rescheduled_timeout"
                return "claim_lost"

        failed_at = payment.get("failed_at")
        if not failed_at:
            _safe_fail(action_id, worker_id, "missing_failed_at")
            _log_run(action_id, payment_id, action_type, attempt_num, "failed", "missing_failed_at")
            return "failed_missing_failed_at"

        if policy.is_expired(failed_at, now):
            _safe_cancel(action_id, worker_id, "expired")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "expired")
            return "cancelled_expired"

        email = payment.get("email")
        if not email:
            _safe_fail(action_id, worker_id, "no_email")
            _log_run(action_id, payment_id, action_type, attempt_num, "failed", "no_email")
            return "failed_no_email"

        if payment.get("recovery_group") != "treatment":
            # A recovery action should only exist for treatment traffic. A mismatched
            # group indicates corrupted state; fail closed rather than emailing holdout.
            _safe_cancel(action_id, worker_id, "invalid_recovery_group")
            _log_run(
                action_id,
                payment_id,
                action_type,
                attempt_num,
                "cancelled",
                "invalid_recovery_group",
            )
            return "cancelled_invalid_group"

        adjusted_now = policy.adjust_for_quiet_hours(now)
        if adjusted_now > now + timedelta(minutes=1):
            if _safe_reschedule(action_id, worker_id, adjusted_now, "quiet_hours"):
                _log_run(
                    action_id,
                    payment_id,
                    action_type,
                    attempt_num,
                    "rescheduled",
                    "quiet_hours",
                )
                return "rescheduled_quiet_hours"
            return "claim_lost"

        with get_db_cursor() as cur:
            sent_count = _count_sent_messages(cur, payment_id)
            if not policy.can_send_more(sent_count):
                if not actions_repo.cancel_action_cur(cur, action_id, worker_id, "max_messages"):
                    raise ClaimError(f"Worker {worker_id} lost action {action_id}")
                _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "max_messages")
                return "cancelled_max_messages"

            attempt = _get_recovery_attempt(cur, action_id)

        if attempt and attempt["status"] in SUCCESSFUL_RECOVERY_ATTEMPT_STATUSES:
            try:
                actions_repo.complete_action(action_id, worker_id)
            except ClaimError:
                pass
            _log_run(action_id, payment_id, action_type, attempt_num, "completed", "already_recorded")
            return "already_completed"

        if not actions_repo.renew_claim(action_id, worker_id):
            return "claim_lost"

        # Reuse a durable link first. If the DB row was lost after provider creation,
        # resolve the deterministic provider reference_id before creating anything new.
        with get_db_cursor() as cur:
            attempt = _get_recovery_attempt(cur, action_id)

        link_id = attempt.get("link_id") if attempt else None
        link_url = attempt.get("link_url") if attempt else None

        if not link_id or not link_url:
            reference_id = payment_link_reference_id(action_id)
            existing_provider_link = razorpay_client.find_payment_link_by_reference_id(
                reference_id
            )
            if existing_provider_link:
                provider_status = existing_provider_link.get("status")
                if provider_status == "paid":
                    _safe_cancel(action_id, worker_id, "payment_link_already_paid")
                    _log_run(
                        action_id,
                        payment_id,
                        action_type,
                        attempt_num,
                        "cancelled",
                        "payment_link_already_paid",
                    )
                    return "cancelled_link_paid"
                link_id = existing_provider_link.get("id")
                link_url = existing_provider_link.get("short_url")
                if not link_id or not link_url:
                    raise PermanentProviderError(
                        f"Existing Payment Link {reference_id} has incomplete data"
                    )
            else:
                if not actions_repo.renew_claim(action_id, worker_id):
                    return "claim_lost"

                failed_at_utc = policy._ensure_utc(failed_at)
                expiry_time = failed_at_utc + timedelta(days=settings.RECOVERY_EXPIRY_DAYS)
                expiry_unix = int(expiry_time.timestamp())

                link_id, link_url = razorpay_client.create_payment_link(
                    amount=payment["amount"],
                    currency=payment["currency"],
                    customer_email=email,
                    description=f"Recovery payment for {payment_id}",
                    action_id=action_id,
                    original_payment_id=payment_id,
                    expiry_unix=expiry_unix,
                )

            with get_db_cursor() as cur:
                attempt = _upsert_link_attempt(
                    cur,
                    action_id=action_id,
                    payment_id=payment_id,
                    link_id=str(link_id),
                    link_url=str(link_url),
                    recovery_group=payment.get("recovery_group"),
                    delay_used=timedelta(
                        minutes=_delay_used_minutes(
                            payment.get("failure_category"),
                            action_type,
                            int(action.get("step") or 1),
                        )
                    ),
                )
                link_id = attempt["link_id"]
                link_url = attempt["link_url"]
        else:
            link_id = str(link_id)
            link_url = str(link_url)

        # Recheck provider state immediately before customer-facing email.
        if not actions_repo.renew_claim(action_id, worker_id):
            return "claim_lost"

        rp_payment, order_paid = _fresh_payment_state(payment_id)
        if _is_successfully_paid(rp_payment, order_paid):
            _safe_cancel(action_id, worker_id, "already_paid_after_link")
            _log_run(
                action_id,
                payment_id,
                action_type,
                attempt_num,
                "cancelled",
                "already_paid_after_link",
            )
            return "cancelled_paid"

        # The claim can expire while provider calls execute. Renewal immediately before
        # sending minimizes that race; the DB still treats provider idempotency as the
        # final protection against duplicate sends during retries.
        if not actions_repo.renew_claim(action_id, worker_id):
            return "claim_lost"

        email_kind = "soft_reminder" if action_type == "send_email_reminder" else "recovery"
        send_recovery_email(
            to=email,
            amount=payment["amount"],
            currency=payment["currency"],
            link_url=link_url,
            kind=email_kind,
            suggest_other_method=policy.suggests_other_method(
                payment.get("failure_category"),
                action_type,
            ),
            idempotency_key=f"recoverai:{action_id}:email",
        )

        sent_at = _utc_now()
        with get_db_cursor() as cur:
            marked = _mark_attempt_sent(cur, action_id, sent_at)
            if not marked:
                raise RuntimeError(
                    f"Recovery email succeeded but no durable sent state exists for {action_id}"
                )

            # If the payment-captured webhook cancelled the action while the email was
            # in flight, preserve the sent attempt but do not resurrect the action.
            actions_repo.complete_action_cur(cur, action_id, worker_id)

        elapsed_ms = (time.monotonic() - started) * 1000
        _log_run(
            action_id,
            payment_id,
            action_type,
            attempt_num,
            "completed",
            f"email_sent elapsed_ms={elapsed_ms:.1f}",
        )
        return "completed"

    except ClaimError as exc:
        logger.info("Recovery action %s lost its lease: %s", action_id, exc)
        return "claim_lost"
    except TemporaryProviderError as exc:
        if payment_id:
            try:
                _mark_attempt_email_failed(action_id, str(exc))
            except Exception:
                logger.exception("Failed to record temporary email/provider failure for %s", action_id)
        try:
            _safe_reschedule(
                action_id,
                worker_id,
                _utc_now() + timedelta(seconds=policy.retry_backoff(attempt_num)),
                str(exc),
            )
        except Exception:
            logger.exception("Failed to reschedule temporary error for %s", action_id)
        _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", f"temporary: {exc}")
        return "rescheduled_temporary"
    except PermanentProviderError as exc:
        if payment_id:
            try:
                _mark_attempt_email_failed(action_id, str(exc))
            except Exception:
                logger.exception("Failed to record permanent provider failure for %s", action_id)
        _safe_fail(action_id, worker_id, str(exc))
        _log_run(action_id, payment_id, action_type, attempt_num, "failed", f"permanent: {exc}")
        return "failed_permanent"
    except Exception as exc:
        logger.exception("Unexpected error executing recovery action %s", action_id)
        if payment_id:
            try:
                _mark_attempt_email_failed(action_id, str(exc))
            except Exception:
                logger.exception("Failed to record unexpected recovery failure for %s", action_id)
        try:
            _safe_reschedule(
                action_id,
                worker_id,
                _utc_now() + timedelta(seconds=policy.retry_backoff(attempt_num)),
                f"unexpected: {exc}",
            )
        except Exception:
            logger.exception("Failed to reschedule unexpected error for %s", action_id)
        _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", f"unexpected: {exc}")
        return "rescheduled_generic"


def _log_run(
    action_id: str,
    payment_id: Optional[str],
    action_type: str,
    attempt: int,
    status: str,
    result: str,
) -> None:
    logger.info(
        "RECOVERY_RUN | %s | %s | %s | %s | %s | %s",
        action_id,
        payment_id,
        action_type,
        attempt,
        status,
        result[:1000],
    )