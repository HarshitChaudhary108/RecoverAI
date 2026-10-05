import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.app.config import settings

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cancel_payment_actions(cur, payment_id: str, reason: str) -> None:
    """Cancel all pending/claimed recovery actions for a payment."""
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


def cancel_order_actions(
    cur,
    order_id: str,
    current_payment_id: str,
    reason: str,
) -> None:
    """Cancel actions belonging to earlier failed attempts of the same order."""
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


def _latest_qualifying_attempt_for_order(
    cur,
    order_id: str,
    captured_payment_id: str,
    captured_at: datetime,
):
    captured_at = _as_utc(captured_at) or _utc_now()
    cur.execute(
        """
        SELECT
            ra.id,
            ra.original_payment_id,
            ra.recovery_group,
            ra.sent_at
        FROM recovery_attempts AS ra
        JOIN payments AS p
          ON p.payment_id = ra.original_payment_id
        WHERE p.order_id = %s
          AND p.payment_id <> %s
          AND p.status = 'failed'
          AND ra.channel = 'email'
          AND ra.status IN ('sent', 'recovered')
          AND ra.sent_at IS NOT NULL
          AND ra.sent_at <= %s
          AND %s <= ra.sent_at + (%s * INTERVAL '1 hour')
        ORDER BY ra.sent_at DESC, ra.id DESC
        LIMIT 1
        """,
        (
            order_id,
            captured_payment_id,
            captured_at,
            captured_at,
            settings.RECOVERY_ATTRIBUTION_WINDOW_HOURS,
        ),
    )
    return cur.fetchone()


def attribute_recovery(
    cur,
    order_id: str,
    captured_payment_id: str,
    captured_at: Optional[datetime] = None,
) -> None:
    """Apply 48-hour recovery attribution and conservative self-recovery rules."""
    captured_at = _as_utc(captured_at) or _utc_now()
    latest_attempt = _latest_qualifying_attempt_for_order(
        cur,
        order_id,
        captured_payment_id,
        captured_at,
    )

    if latest_attempt:
        attempt_id, original_payment_id, recovery_group, sent_at = latest_attempt
        cur.execute(
            """
            UPDATE recovery_attempts
            SET status = 'recovered',
                recovered_payment_id = COALESCE(recovered_payment_id, %s),
                recovered_at = COALESCE(recovered_at, %s)
            WHERE id = %s
            """,
            (captured_payment_id, captured_at, attempt_id),
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
        logger.info(
            "Recovery attributed: captured=%s original=%s attempt=%s group=%s sent_at=%s",
            captured_payment_id,
            original_payment_id,
            attempt_id,
            recovery_group,
            sent_at,
        )

    # A failed payment is self-recovered only when no recovery email has EVER
    # successfully been sent for that payment. A message sent outside the 48-hour
    # attribution window therefore remains unattributed rather than being relabeled.
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


def handle_paid_link_recovery(
    cur,
    link_id: str,
    payment_id: str,
    event_at: Optional[datetime] = None,
) -> bool:
    """Attribute a paid RecoveryAI Payment Link to its original failed payment."""
    event_at = _as_utc(event_at) or _utc_now()
    cur.execute(
        """
        SELECT id,
               original_payment_id,
               status,
               sent_at
        FROM recovery_attempts
        WHERE link_id = %s
        ORDER BY id
        LIMIT 1
        FOR UPDATE
        """,
        (link_id,),
    )
    row = cur.fetchone()
    if not row:
        logger.warning("Unmatched recovery Payment Link paid event: %s", link_id)
        return False

    attempt_id, original_payment_id, status, sent_at = row

    if sent_at is not None:
        sent_at = _as_utc(sent_at)
        within_window = sent_at <= event_at <= sent_at + timedelta(
            hours=settings.RECOVERY_ATTRIBUTION_WINDOW_HOURS
        )
    else:
        within_window = False

    if not within_window:
        logger.warning(
            "Recovery Payment Link %s was paid outside attribution window or before send state "
            "(attempt=%s status=%s sent_at=%s event_at=%s)",
            link_id,
            attempt_id,
            status,
            sent_at,
            event_at,
        )
        return False

    cur.execute(
        """
        UPDATE recovery_attempts
        SET status = 'recovered',
            recovered_payment_id = COALESCE(recovered_payment_id, %s),
            recovered_at = COALESCE(recovered_at, %s)
        WHERE id = %s
        """,
        (payment_id, event_at, attempt_id),
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
        "Recovery attempt %s attributed through Payment Link %s to payment %s",
        attempt_id,
        link_id,
        payment_id,
    )
    return True
