import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from backend.app.actions_repo import ActionsRepository
from backend.app.razorpay_client import RazorpayClient, TemporaryProviderError, PermanentProviderError
from backend.app.messaging import send_recovery_email
from backend.app import policy
from backend.app.db import get_db_cursor

logger = logging.getLogger(__name__)
actions_repo = ActionsRepository()
razorpay_client = RazorpayClient()

def run_action(action_id: str, worker_id: str) -> str:
    """
    Executes a recovery action following the 12-step flow.
    Returns a status string indicating the outcome.
    """
    now = datetime.now(ZoneInfo("UTC"))
    payment_id = None
    action_type = "unknown"
    attempt_num = 0

    try:
        # 1. Check the claim is still ours
        if not actions_repo.still_owns_claim(action_id, worker_id):
            return "claim_lost"

        # 2. Load the action and its payment
        action, payment = actions_repo.get_action_and_payment(action_id)
        payment_id = payment["payment_id"]
        action_type = action["action_type"]
        attempt_num = action["attempts"]

        # 3. Fetch the LATEST payment status from Razorpay now
        # We trust the live API, not the database record.
        rp_payment = razorpay_client.fetch_payment(payment_id)
        is_paid = razorpay_client.is_order_paid(rp_payment.order_id) if rp_payment.order_id else False

        # 4. If the payment is captured, or the order is already paid: cancel
        if rp_payment.status == "captured" or is_paid:
            actions_repo.cancel_action(action_id, worker_id, "already_paid")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "already_paid")
            return "cancelled_paid"

        # 5. Timeout handling
        if payment["failure_category"] == "timeout":
            # Status meanings from provider_notes.md: pending, created, authorized
            if rp_payment.status in ["pending", "created", "authorized"]:
                next_run = policy.next_timeout_recheck(now, action.get("recheck_count", 0))
                if next_run:
                    actions_repo.reschedule_action(action_id, worker_id, next_run, "still_unresolved")
                    _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", "timeout_pending")
                    return "rescheduled_timeout"
                else:
                    actions_repo.cancel_action(action_id, worker_id, "status_unresolved")
                    _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "status_unresolved")
                    return "cancelled_unresolved"

        # 6. If expired (7 days after failed_at)
        if policy.is_expired(payment["failed_at"], now):
            actions_repo.cancel_action(action_id, worker_id, "expired")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "expired")
            return "cancelled_expired"

        # 7. If messages already sent for this payment is 3 or more
        # Note: We count attempts from recovery_attempts table
        with get_db_cursor() as cur:
            cur.execute("SELECT count(*) FROM recovery_attempts WHERE payment_id = %s", (payment_id,))
            sent_count = cur.fetchone()[0]

        if not policy.can_send_more(sent_count):
            actions_repo.cancel_action(action_id, worker_id, "max_messages")
            _log_run(action_id, payment_id, action_type, attempt_num, "cancelled", "max_messages")
            return "cancelled_max_messages"

        # 8. If the payment has no customer email
        if not payment["email"]:
            actions_repo.fail_action(action_id, worker_id, "no_email")
            _log_run(action_id, payment_id, action_type, attempt_num, "failed", "no_email")
            return "failed_no_email"

        # 9. If now is inside quiet hours
        adjusted_now = policy.adjust_for_quiet_hours(now)
        if adjusted_now > now + timedelta(minutes=1): # Allow 1m drift
            actions_repo.reschedule_action(action_id, worker_id, adjusted_now, "quiet_hours")
            _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", "quiet_hours")
            return "rescheduled_quiet_hours"

        # 10. Make the Payment Link
        # Reuse existing link if it exists for this action_id
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT link_id, link_url FROM recovery_attempts WHERE action_id = %s LIMIT 1",
                (action_id,)
            )
            row = cur.fetchone()
            if row:
                link_id, link_url = row
            else:
                # Calculate expiry (e.g., 3 days from now)
                expiry_unix = int((now + timedelta(days=3)).timestamp())
                link_id, link_url = razorpay_client.create_payment_link(
                    amount=payment["amount"],
                    currency=payment["currency"],
                    customer_email=payment["email"],
                    description=f"Recovery payment for {payment_id}",
                    action_id=action_id,
                    original_payment_id=payment_id,
                    expiry_unix=expiry_unix
                )
                # Save initial recovery attempt
                cur.execute(
                    """
                    INSERT INTO recovery_attempts (action_id, payment_id, link_id, link_url, status, created_at)
                    VALUES (%s, %s, %s, %s, 'link_created', %s)
                    """,
                    (action_id, payment_id, link_id, link_url, now)
                )

        # 11. Send the email with Resend
        email_kind = "soft_reminder" if action_type == "send_email_reminder" else "recovery"
        send_recovery_email(
            to=payment["email"],
            amount=payment["amount"],
            currency=payment["currency"],
            link_url=link_url,
            kind=email_kind,
            suggest_other_method=policy.suggests_other_method(payment["failure_category"], action_type),
            idempotency_key=action_id
        )

        # 12. In ONE transaction: update recovery_attempts and mark action completed
        with get_db_cursor() as cur:
            cur.execute(
                """
                UPDATE recovery_attempts
                SET status = 'sent', sent_at = %s, channel = 'email'
                WHERE action_id = %s
                """,
                (now, action_id)
            )
            actions_repo.complete_action_cur(cur, action_id, now)

        _log_run(action_id, payment_id, action_type, attempt_num, "completed", "success")
        return "completed"

    except TemporaryProviderError as e:
        delay = policy.retry_backoff(attempt_num)
        actions_repo.reschedule_action(action_id, worker_id, now + timedelta(seconds=delay), str(e))
        _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", f"temp_error: {e}")
        return "rescheduled_temporary"
    except PermanentProviderError as e:
        actions_repo.fail_action(action_id, worker_id, str(e))
        _log_run(action_id, payment_id, action_type, attempt_num, "failed", f"perm_error: {e}")
        return "failed_permanent"
    except Exception as e:
        logger.exception(f"Unexpected error executing action {action_id}: {e}")
        # Generic error treated as temporary with a standard 15m delay
        actions_repo.reschedule_action(action_id, worker_id, now + timedelta(minutes=15), str(e))
        _log_run(action_id, payment_id, action_type, attempt_num, "rescheduled", f"generic_error: {e}")
        return "rescheduled_generic"

def _log_run(action_id, payment_id, action_type, attempt, status, result):
    """Logs a structured line per run."""
    # Format: action_id, payment_id, action_type, attempt, status, result, provider_error
    # Note: provider_error is embedded in result here for simplicity
    logger.info(f"RECOVERY_RUN | {action_id} | {payment_id} | {action_type} | {attempt} | {status} | {result}")
