import logging
import json
from backend.app.db import get_db_cursor
from backend.worker.tasks import classify_payment

logger = logging.getLogger(__name__)

def handle_razorpay_event(event_id: str, payload: dict, cur=None):
    """
    Processes a verified Razorpay webhook event.
    Returns (payment_id, success_boolean).

    If cur is provided, it is used for the transaction.
    Otherwise, a new transaction is opened.
    """
    event_type = payload.get("event")
    payment_id = None
    success = False

    # Use provided cursor or open a new one
    if cur is None:
        with get_db_cursor() as transaction_cur:
            payment_id, success = _process_event(event_id, payload, transaction_cur)
            return payment_id, success
    else:
        return _process_event(event_id, payload, cur)

def _process_event(event_id: str, payload: dict, cur):
    """
    Internal processing logic that uses the provided cursor.
    Does NOT commit or close the cursor.
    """
    event_type = payload.get("event")
    payment_id = None
    success = False

    if event_type == "payment.failed":
        # payload.payment.entity mapping from provider_notes.md
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        if not entity:
            logger.error(f"Missing payment entity in payment.failed event {event_id}")
            return None, False

        payment_id = entity.get("id")
        order_id = entity.get("order_id")
        amount = entity.get("amount")
        currency = entity.get("currency")
        method = entity.get("method")
        bank = entity.get("bank")
        email = entity.get("email")
        contact = entity.get("contact")

        # Mapping error fields
        error_code = entity.get("error_code")
        error_reason = entity.get("error_description") or entity.get("error_reason")
        error_source = entity.get("error_source")
        error_step = entity.get("error_step")

        if not all([payment_id, order_id, amount]):
            logger.error(f"Missing required fields in payment.failed event {event_id}")
            return None, False

        cur.execute(
            """
            INSERT INTO payments (
                payment_id, order_id, amount, currency, method, bank,
                status, error_code, error_reason, error_source, error_step,
                customer_email, customer_contact, classification_status, recovery_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (payment_id) DO UPDATE SET
                status = EXCLUDED.status,
                error_code = EXCLUDED.error_code,
                error_reason = EXCLUDED.error_reason,
                error_source = EXCLUDED.error_source,
                error_step = EXCLUDED.error_step,
                updated_at = CURRENT_TIMESTAMP
            WHERE payments.status != 'captured'
            """,
            (payment_id, order_id, amount, currency, method, bank,
             'failed', error_code, error_reason, error_source, error_step,
             email, contact, 'pending', 'open')
        )
        success = True

    elif event_type == "payment.captured":
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        payment_id = entity.get("id")
        order_id = entity.get("order_id")
        if payment_id:
            cur.execute(
                "UPDATE payments SET status = 'captured', updated_at = CURRENT_TIMESTAMP WHERE payment_id = %s AND status != 'captured'",
                (payment_id,)
            )

            if order_id:
                from backend.app import recovery
                recovery.cancel_payment_actions(cur, payment_id, "payment_captured")
                recovery.cancel_order_actions(cur, order_id, payment_id, "order_paid")
                recovery.attribute_recovery(cur, order_id, payment_id)

            success = True

    elif event_type == "payment_link.paid":
        # payload.payment_link.entity from provider_notes.md
        pl_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        p_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

        link_id = pl_entity.get("id")
        payment_id = p_entity.get("id")

        if link_id and payment_id:
            from backend.app import recovery
            if recovery.handle_paid_link_recovery(cur, link_id, payment_id):
                success = True
            else:
                # Logged inside handle_paid_link_recovery, just return success=True to acknowledge webhook
                success = True
        else:
            logger.error(f"Missing link_id or payment_id in payment_link.paid event {event_id}")
            success = False

    else:
        # Any other event: save event only (handled by webhook route), return 200
        success = True

    return payment_id, success
