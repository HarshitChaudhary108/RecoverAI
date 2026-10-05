import logging
from datetime import datetime, timezone
from typing import Optional

from backend.app.config import settings

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cancel_payment_actions(cur, payment_id: str, reason: str) -> None:
    """Cancel all outstanding actions for one payment idempotently."""
    cur.execute(
        """
        UPDATE scheduled_actions
        SET status = 'cancelled',
            result = %s,
            locked_by = NULL,
            lease_expires_at = NULL,
            completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
        WHERE payment_id = %s
          AND status IN ('pending', 'claimed')
        """,
        (reason, payment_id),
    )


def cancel_order_actions(cur, order_id: str, current_payment_id: str, reason: str) -> None:
    """Cancel outstanding actions for earlier attempts of the same order."""
    cur.execute(
        """
        UPDATE scheduled_actions AS a
        SET status = 'cancelled',
            result = %s,
            locked_by = NULL,
            lease_expires_at = NULL,
            completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
        FROM payments AS p
        WHERE a.payment_id = p.payment_id
          AND p.order_id = %s
          AND p.payment_id <> %s
          AND p.status = 'failed'
          AND a.status IN ('pending', 'claimed')
        """,
        (reason, order_id, current_payment_id),
    )


def _latest_sent_attempt_for_order(cur, order_id: str, captured_payment_id: str):
    cur.execute(
        """
        SELECT ra.id,
               ra.original_payment_id,
               ra.recovery_group,
               ra.sent_at
        FROM recovery_attempts AS ra
        JOIN payments AS p ON p.payment_id = ra.original_payment_id
        WHERE p.order_id = %s
          AND p.payment_id <> %s
          AND p.status = 'failed'
          AND ra.channel = 'email'
          AND ra.status IN ('sent', 'recovered')
          AND ra.sent_at IS NOT NULL
          AND ra.sent_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
        ORDER BY ra.sent_at DESC
        LIMIT 1
        """,
        (order_id, captured_payment_id, settings.RECOVERY_ATTRIBUTION_WINDOW_HOURS),
    )
    return cur.fetchone()


def attribute_recovery(cur, order_id: str, captured_payment_id: str) -> None:
    """Attribute an order-level capture to the most recent qualifying recovery action."""
    latest_attempt = _latest_sent_attempt_for_order(cur, order_id, captured_payment_id)

    if latest_attempt:
        attempt_id, original_payment_id, _, sent_at = latest_attempt
        recovered_at = _utc_now()
        cur.execute(
            """
            UPDATE recovery_attempts
            SET status = 'recovered',
                recovered_payment_id = COALESCE(recovered_payment_id, %s),
                recovered_at = COALESCE(recovered_at, %s)
            WHERE id = %s
            """,
            (captured_payment_id, recovered_at, attempt_id),
        )
        cur.execute(
            """
            UPDATE payments
            SET recovery_status = 'recovered', updated_at = CURRENT_TIMESTAMP
            WHERE payment_id = %s
              AND status = 'failed'
            """,
            (original_payment_id,),
        )
        logger.info(
            "Attributed captured payment %s to recovery attempt %s for failed payment %s",
            captured_payment_id,
            attempt_id,
            original_payment_id,
        )

    # Mark remaining failed payments that never received a recovery email as
    # self-recovered. Do not relabel payments that actually had a recovery message.
    cur.execute(
        """
        UPDATE payments AS p
        SET recovery_status = 'self_recovered',
            updated_at = CURRENT_TIMESTAMP
        WHERE p.order_id = %s
          AND p.payment_id <> %s
          AND p.status = 'failed'
          AND p.recovery_status = 'open'
          AND NOT EXISTS (
              SELECT 1
              FROM recovery_attempts AS ra
              WHERE ra.original_payment_id = p.payment_id
                AND ra.channel = 'email'
                AND ra.status IN ('sent', 'recovered')
                AND ra.sent_at IS NOT NULL
          )
        """,
        (order_id, captured_payment_id),
    )


def handle_paid_link_recovery(cur, link_id: str, payment_id: str) -> bool:
    """Attribute a paid Razorpay Payment Link to the recovery attempt that created it."""
    cur.execute(
        """
        SELECT id, original_payment_id, status
        FROM recovery_attempts
        WHERE link_id = %s
        FOR UPDATE
        """,
        (link_id,),
    )
    row = cur.fetchone()
    if not row:
        logger.warning("Paid link event received for unmatched link_id=%s", link_id)
        return False

    attempt_id, original_payment_id, status = row
    recovered_at = _utc_now()
    cur.execute(
        """
        UPDATE recovery_attempts
        SET status = 'recovered',
            recovered_payment_id = COALESCE(recovered_payment_id, %s),
            recovered_at = COALESCE(recovered_at, %s)
        WHERE id = %s
        """,
        (payment_id, recovered_at, attempt_id),
    )
    cur.execute(
        """
        UPDATE payments
        SET recovery_status = 'recovered',
            updated_at = CURRENT_TIMESTAMP
        WHERE payment_id = %s
          AND status = 'failed'
        """,
        (original_payment_id,),
    )
    cancel_payment_actions(cur, original_payment_id, "payment_link_paid")

    logger.info(
        "Recovery attempt %s marked recovered from Payment Link %s / payment %s (previous_status=%s)",
        attempt_id,
        link_id,
        payment_id,
        status,
    )
    return True
