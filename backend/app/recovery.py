import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

def cancel_payment_actions(cur, payment_id: str, reason: str):
    """
    Cancels all pending or claimed actions for a specific payment.
    Safe to run multiple times.
    """
    cur.execute(
        """
        UPDATE scheduled_actions
        SET status = 'cancelled',
            result = %s,
            completed_at = CURRENT_TIMESTAMP
        WHERE payment_id = %s
          AND status IN ('pending', 'claimed')
        """,
        (reason, payment_id)
    )

def cancel_order_actions(cur, order_id: str, current_payment_id: str, reason: str):
    """
    Cancels pending actions of EARLIER failed payments with the same order_id.
    """
    cur.execute(
        """
        UPDATE scheduled_actions
        SET status = 'cancelled',
            result = %s,
            completed_at = CURRENT_TIMESTAMP
        WHERE payment_id IN (
            SELECT payment_id FROM payments
            WHERE order_id = %s AND payment_id != %s
        )
        AND status = 'pending'
        """,
        (reason, order_id, current_payment_id)
    )

def attribute_recovery(cur, order_id: str, captured_payment_id: str):
    """
    Implements attribution rules for captured payments.
    """
    # 0. Handle the captured payment itself: If it was a holdout, mark as self_recovered
    cur.execute(
        "SELECT recovery_group FROM payments WHERE payment_id = %s",
        (captured_payment_id,)
    )
    res = cur.fetchone()
    if res and res[0] == 'holdout':
        cur.execute(
            "UPDATE payments SET recovery_status = 'self_recovered' WHERE payment_id = %s",
            (captured_payment_id,)
        )

    # 1. Find failed payments on the same order (excluding the captured one)
    cur.execute(
        "SELECT payment_id, recovery_group FROM payments WHERE order_id = %s AND payment_id != %s AND status = 'failed'",
        (order_id, captured_payment_id)
    )
    failed_payments = cur.fetchall()

    for payment_id, recovery_group in failed_payments:
        # Check if an email was sent in the last 48 hours
        cur.execute(
            """
            SELECT id FROM recovery_attempts
            WHERE original_payment_id = %s
              AND channel = 'email'
              AND sent_at >= CURRENT_TIMESTAMP - INTERVAL '48 hours'
            ORDER BY sent_at DESC LIMIT 1
            """,
            (payment_id,)
        )
        attempt = cur.fetchone()

        if attempt:
            # Case: Recovered via email
            attempt_id = attempt[0]
            cur.execute(
                """
                UPDATE recovery_attempts
                SET recovered_payment_id = %s, recovered_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (captured_payment_id, attempt_id)
            )
            cur.execute(
                "UPDATE payments SET recovery_status = 'recovered' WHERE payment_id = %s",
                (payment_id,)
            )
        else:
            # Case: No email sent within 48h (or never sent)
            # Holdout payments are never emailed, so they fall here
            cur.execute(
                "UPDATE payments SET recovery_status = 'self_recovered' WHERE payment_id = %s",
                (payment_id,)
            )

def handle_paid_link_recovery(cur, link_id: str, payment_id: str):
    """
    Handles payment_link.paid event attribution.
    """
    # Find the attempt matching the link_id
    cur.execute(
        """
        SELECT id, original_payment_id FROM recovery_attempts
        WHERE link_id = %s
        """,
        (link_id,)
    )
    row = cur.fetchone()

    if not row:
        logger.warning(f"Paid link event received for unmatched link_id: {link_id}")
        return False

    attempt_id, original_payment_id = row

    # Mark attempt as recovered
    cur.execute(
        """
        UPDATE recovery_attempts
        SET recovered_payment_id = %s, recovered_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (payment_id, attempt_id)
    )

    # Update original payment status
    cur.execute(
        "UPDATE payments SET recovery_status = 'recovered' WHERE payment_id = %s",
        (original_payment_id,)
    )

    # Cancel remaining actions for original payment
    cancel_payment_actions(cur, original_payment_id, "payment_link_paid")

    return True
