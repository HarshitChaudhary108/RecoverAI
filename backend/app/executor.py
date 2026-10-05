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
)

logger = logging.getLogger(__name__)
actions_repo = ActionsRepository()
razorpay_client = RazorpayClient()


LIVE_PAYMENT_STATUSES = {"captured"}
UNRESOLVED_TIMEOUT_STATUSES = {"pending", "created", "authorized"}
SUCCESSFUL_RECOVERY_ATTEMPT_STATUSES = {"sent", "recovered"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _delay_used_minutes(run_at: datetime, failed_at: datetime) -> int:
    delta = policy._ensure_utc(run_at) - policy._ensure_utc(failed_at)
    return max(0, int(delta.total_seconds() // 60))


def _effective_action_run_at(action: Dict[str, Any]) -> datetime:
    run_at = action.get("run_at")
    if not isinstance(run_at, datetime):
        return _utc_now()
    return policy._ensure_utc(run_at)


def _get_recovery_attempt(cur, action_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT id, action_id, original_payment_id, link_id, link_url,
               channel, recovery_group, delay_used, sent_at, status,
               recovered_payment_id, recovered_at
        FROM recovery_attempts
        WHERE action_id = %s
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
          AND status IN ('sent', 'recovered')
          AND sent_at IS NOT NULL
        """,
        (payment_id,),
    )
    return int(cur.fetchone()[0] or 0)


def _insert_or_get_attempt(
    cur,
    *,
    action_id: str,
    payment_id: str,
    link_id: str,
    link_url: str,
    recovery_group: Optional[str],
    delay_used: int,
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
        ON CONFLICT (action_id) DO NOTHING
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
        raise RuntimeError(f"Recovery attempt could not be loaded for action {action_id}")
    return attempt


def _mark_attempt_email_failed(cur, action_id: str, error: str) -> None:
    cur.execute(
        """
        UPDATE recovery_attempts
        SET status = 'email_failed'
        WHERE action_id = %s
          AND status = 'link_created'
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
          AND status = 'link_created'
        RETURNING id
        """,
        (sent_at, action_id),
    )
    return cur.fetchone() is not None


def _fresh_payment_state(payment_id: str):
    rp_payment = razorpay_client.fetch_payment(payment_id)
    order_id = getattr(rp_payment, "order_id", None)
    order_paid = razorpay_client.is_order_paid(order_id) if order_id else False
    return rp_payment, order_paid


def _is_successfully_paid(rp_payment, order_paid: bool) -> bool:
    return getattr(rp_payment, "status", None) in LIVE_PAYMENT_STATUSES or order_paid


def _reschedule_with_backoff(
    action_id: str,
    worker_id: str,
    attempt_num: int,
    reason: str,
    *,
    increment_recheck: bool = False,
    minutes: Optional[int] = None,
) -> str:
    delay_seconds = minutes * 60 if minutes is not None else policy.retry_backoff(attempt_num)
    next_run = _utc_now() + timedelta(seconds=delay_seconds)
    actions_repo.reschedule_action(
        action_id,
        worker_id,
        policy.adjust_for_quiet_hours(next_run),
        reason,
        increment_recheck=increment_recheck,
    )
    return "rescheduled"


def run_action(action_id: str, worker_id: str) -> str:
    """Execute one claimed recovery action with durable, idempotent state transitions."""
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

        # Always trust the latest Razorpay state over the original webhook state.
        rp_payment, order_paid = _fresh_payment_state(payment_id)
        if _is_successfully_paid(rp_payment, order_paid):
            actions_repo.cancel_action(action_id, worker_id, "already_paid")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "already_paid")
            return "cancelled_paid"

        now = _utc_now()

        # Timeout actions must not create another payment while the original payment
        # may still be unresolved. Persist the recheck count in scheduled_actions.
        if payment.get("failure_category") == "timeout":
            provider_status = getattr(rp_payment, "status", None)
            if provider_status in UNRESOLVED_TIMEOUT_STATUSES:
                recheck_count = int(action.get("recheck_count") or 0)
                next_run = policy.next_timeout_recheck(now, recheck_count)
                if next_run is None:
                    actions_repo.cancel_action(action_id, worker_id, "status_unresolved")
                    _log_run(
                        action_id,
                        payment_id,
                        action_type,
                        attempt_num,
                        "cancelled",
                        "status_unresolved",
                    )
                    return "cancelled_unresolved"
                actions_repo.reschedule_action(
                    action_id,
                    worker_id,
                    policy.adjust_for_quiet_hours(next_run),
                    "still_unresolved",
                    increment_recheck=True,
                )
                _log_run(
                    action_id,
                    payment_id,
                    action_type,
                    attempt_num,
                    "rescheduled",
                    "timeout_pending",
                )
                return "rescheduled_timeout"

        if policy.is_expired(payment.get("failed_at"), now):
            actions_repo.cancel_action(action_id, worker_id, "expired")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "expired")
            return "cancelled_expired"

        if not payment.get("failed_at"):
            actions_repo.fail_action(action_id, worker_id, "missing_failed_at")
            _log_run(action_id, payment_id, action_type, attempt_num, "failed", "missing_failed_at")
            return "failed_missing_failed_at"

        with get_db_cursor() as cur:
            sent_count = _count_sent_messages(cur, payment_id)
            if not policy.can_send_more(sent_count):
                actions_repo.cancel_action_cur(cur, action_id, worker_id, "max_messages")
                _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "max_messages")
                return "cancelled_max_messages"

            existing_attempt = _get_recovery_attempt(cur, action_id)

        if existing_attempt and existing_attempt["status"] in SUCCESSFUL_RECOVERY_ATTEMPT_STATUSES:
            actions_repo.complete_action(action_id, worker_id)
            _log_run(action_id, payment_id, action_type, attempt_num, "completed", "already_recorded")
            return "already_completed"

        email = payment.get("email")
        if not email:
            actions_repo.fail_action(action_id, worker_id, "no_email")
            _log_run(action_id, payment_id, action_type, attempt_num, "failed", "no_email")
            return "failed_no_email"

        adjusted_now = policy.adjust_for_quiet_hours(now)
        if adjusted_now > now + timedelta(minutes=1):
            actions_repo.reschedule_action(
                action_id,
                worker_id,
                adjusted_now,
                "quiet_hours",
            )
            _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", "quiet_hours")
            return "rescheduled_quiet_hours"

        # Recheck ownership immediately before provider work.
        if not actions_repo.renew_claim(action_id, worker_id):
            return "claim_lost"

        # Reuse a link already associated with this action. If none exists, create one.
        existing_attempt = None
        with get_db_cursor() as cur:
            existing_attempt = _get_recovery_attempt(cur, action_id)

        link_id: Optional[str] = existing_attempt.get("link_id") if existing_attempt else None
        link_url: Optional[str] = existing_attempt.get("link_url") if existing_attempt else None

        if not link_id or not link_url:
            failed_at = policy._ensure_utc(payment["failed_at"])
            expiry_time = min(
                failed_at + timedelta(days=settings.RECOVERY_EXPIRY_DAYS),
                now + timedelta(days=settings.RECOVERY_EXPIRY_DAYS),
            )
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
                if not actions_repo.still_owns_claim(action_id, worker_id):
                    return "claim_lost"
                existing_attempt = _insert_or_get_attempt(
                    cur,
                    action_id=action_id,
                    payment_id=payment_id,
                    link_id=link_id,
                    link_url=link_url,
                    recovery_group=payment.get("recovery_group"),
                    delay_used=_delay_used_minutes(
                        _effective_action_run_at(action),
                        payment["failed_at"],
                    ),
                )
                link_id = existing_attempt["link_id"]
                link_url = existing_attempt["link_url"]
        else:
            if not existing_attempt:
                raise RuntimeError(f"Recovery link exists without attempt for {action_id}")

        # Provider state can change while Payment Link creation is running.
        if not actions_repo.renew_claim(action_id, worker_id):
            return "claim_lost"
        rp_payment, order_paid = _fresh_payment_state(payment_id)
        if _is_successfully_paid(rp_payment, order_paid):
            actions_repo.cancel_action(action_id, worker_id, "already_paid")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "already_paid_after_link")
            return "cancelled_paid"

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
                # Another worker may have recorded the send while this worker was
                # outside the database. Do not create another attempt.
                cur.execute(
                    "SELECT status FROM recovery_attempts WHERE action_id = %s",
                    (action_id,),
                )
                row = cur.fetchone()
                if not row or row[0] not in SUCCESSFUL_RECOVERY_ATTEMPT_STATUSES:
                    raise RuntimeError(
                        f"Recovery email succeeded but attempt state is inconsistent for {action_id}"
                    )

        # Completion is ownership-checked. A stale worker cannot complete a reclaimed action.
        try:
            actions_repo.complete_action(action_id, worker_id)
        except ClaimError:
            # The provider-side idempotency key protects an immediate retry. The action
            # remains recoverable through the lease/scheduler state rather than being lost.
            logger.warning("Claim lost after successful email for action %s", action_id)
            return "claim_lost_after_send"

        _log_run(action_id, payment_id, action_type, attempt_num, "completed", "success")
        return "completed"

    except ClaimError as exc:
        logger.warning("Claim lost while executing action %s: %s", action_id, exc)
        return "claim_lost"
    except TemporaryProviderError as exc:
        try:
            result = _reschedule_with_backoff(
                action_id,
                worker_id,
                attempt_num,
                f"temporary_provider_error: {exc}",
            )
        except ClaimError:
            return "claim_lost"
        _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", str(exc))
        return f"{result}_temporary"
    except PermanentProviderError as exc:
        try:
            with get_db_cursor() as cur:
                _mark_attempt_email_failed(cur, action_id, str(exc))
            actions_repo.fail_action(action_id, worker_id, f"permanent_provider_error: {exc}")
        except ClaimError:
            return "claim_lost"
        _log_run(action_id, payment_id, action_type, attempt_num, "failed", str(exc))
        return "failed_permanent"
    except Exception as exc:
        logger.exception("Unexpected error executing action %s: %s", action_id, exc)
        try:
            _reschedule_with_backoff(
                action_id,
                worker_id,
                attempt_num,
                f"internal_error: {exc}",
                minutes=15,
            )
        except ClaimError:
            return "claim_lost"
        return "rescheduled_internal"
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "recovery_run | action_id=%s | payment_id=%s | action_type=%s | attempt=%s | latency_ms=%.2f",
            action_id,
            payment_id,
            action_type,
            attempt_num,
            elapsed_ms,
        )


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
        result,
    )
