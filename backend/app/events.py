import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from backend.app import recovery
from backend.app.db import get_db_cursor

logger = logging.getLogger(__name__)


def _event_timestamp(payload: Dict[str, Any]) -> datetime:
    value = payload.get("created_at")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _entity_timestamp(entity: Dict[str, Any], fallback: datetime) -> datetime:
    value = entity.get("created_at")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return fallback


def _customer_fields(entity: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    email = entity.get("email")
    contact = entity.get("contact")
    customer = entity.get("customer") or {}
    if isinstance(customer, dict):
        email = email or customer.get("email")
        contact = contact or customer.get("contact") or customer.get("phone")
    return email, contact


def _persist_captured_payment(
    cur,
    entity: Dict[str, Any],
    event_ts: datetime,
) -> Tuple[Optional[str], Optional[str], bool]:
    payment_id = entity.get("id")
    order_id = entity.get("order_id")
    amount = entity.get("amount")
    currency = entity.get("currency")
    method = entity.get("method")
    bank = entity.get("bank")
    email, contact = _customer_fields(entity)

    if not payment_id or not order_id or amount is None or not currency:
        logger.error("Captured event missing required payment fields")
        return payment_id, order_id, False

    payment_created_at = _entity_timestamp(entity, event_ts)
    cur.execute(
        """
        INSERT INTO payments (
            payment_id, order_id, amount, currency, method, bank,
            status, customer_email, customer_contact,
            classification_status, recovery_status,
            payment_created_at, captured_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            'captured', %s, %s,
            'classified', 'open',
            %s, %s
        )
        ON CONFLICT (payment_id) DO UPDATE SET
            order_id = COALESCE(payments.order_id, EXCLUDED.order_id),
            amount = COALESCE(payments.amount, EXCLUDED.amount),
            currency = COALESCE(payments.currency, EXCLUDED.currency),
            method = COALESCE(payments.method, EXCLUDED.method),
            bank = COALESCE(payments.bank, EXCLUDED.bank),
            status = 'captured',
            customer_email = COALESCE(payments.customer_email, EXCLUDED.customer_email),
            customer_contact = COALESCE(payments.customer_contact, EXCLUDED.customer_contact),
            payment_created_at = COALESCE(payments.payment_created_at, EXCLUDED.payment_created_at),
            captured_at = COALESCE(payments.captured_at, EXCLUDED.captured_at),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            payment_id,
            order_id,
            amount,
            currency,
            method,
            bank,
            email,
            contact,
            payment_created_at,
            event_ts,
        ),
    )
    return payment_id, order_id, True


def _persist_failed_payment(
    cur,
    entity: Dict[str, Any],
    event_ts: datetime,
) -> Tuple[Optional[str], bool]:
    payment_id = entity.get("id")
    order_id = entity.get("order_id")
    amount = entity.get("amount")
    currency = entity.get("currency")
    method = entity.get("method")
    bank = entity.get("bank")
    email, contact = _customer_fields(entity)
    error_code = entity.get("error_code")
    error_reason = entity.get("error_description") or entity.get("error_reason")
    error_source = entity.get("error_source")
    error_step = entity.get("error_step")
    payment_created_at = _entity_timestamp(entity, event_ts)

    if not payment_id or not order_id or amount is None or not currency:
        logger.error("Missing required fields in payment.failed event")
        return payment_id, False

    cur.execute(
        """
        INSERT INTO payments (
            payment_id, order_id, amount, currency, method, bank,
            status, error_code, error_reason, error_source, error_step,
            customer_email, customer_contact,
            classification_status, recovery_status,
            failed_at, payment_created_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            'failed', %s, %s, %s, %s,
            %s, %s,
            'pending', 'open',
            %s, %s
        )
        ON CONFLICT (payment_id) DO UPDATE SET
            order_id = COALESCE(payments.order_id, EXCLUDED.order_id),
            amount = COALESCE(payments.amount, EXCLUDED.amount),
            currency = COALESCE(payments.currency, EXCLUDED.currency),
            method = COALESCE(payments.method, EXCLUDED.method),
            bank = COALESCE(payments.bank, EXCLUDED.bank),
            status = CASE
                WHEN payments.status = 'captured' THEN 'captured'
                ELSE 'failed'
            END,
            error_code = COALESCE(EXCLUDED.error_code, payments.error_code),
            error_reason = COALESCE(EXCLUDED.error_reason, payments.error_reason),
            error_source = COALESCE(EXCLUDED.error_source, payments.error_source),
            error_step = COALESCE(EXCLUDED.error_step, payments.error_step),
            customer_email = COALESCE(payments.customer_email, EXCLUDED.customer_email),
            customer_contact = COALESCE(payments.customer_contact, EXCLUDED.customer_contact),
            failed_at = CASE
                WHEN payments.failed_at IS NULL AND payments.status <> 'captured'
                THEN EXCLUDED.failed_at
                ELSE payments.failed_at
            END,
            payment_created_at = COALESCE(payments.payment_created_at, EXCLUDED.payment_created_at),
            updated_at = CURRENT_TIMESTAMP
        WHERE payments.status <> 'captured'
        """,
        (
            payment_id,
            order_id,
            amount,
            currency,
            method,
            bank,
            error_code,
            error_reason,
            error_source,
            error_step,
            email,
            contact,
            event_ts,
            payment_created_at,
        ),
    )
    return payment_id, True


def handle_razorpay_event(event_id: str, payload: dict, cur=None):
    """Process one verified webhook event in the caller's transaction."""
    if cur is None:
        with get_db_cursor() as transaction_cur:
            return _process_event(event_id, payload, transaction_cur)
    return _process_event(event_id, payload, cur)


def _process_event(event_id: str, payload: dict, cur):
    event_type = payload.get("event")
    event_ts = _event_timestamp(payload)
    data = payload.get("payload") or {}

    if event_type == "payment.failed":
        entity = ((data.get("payment") or {}).get("entity") or {})
        payment_id, success = _persist_failed_payment(cur, entity, event_ts)
        if success:
            logger.info("Persisted payment.failed event %s for %s", event_id, payment_id)
        return payment_id, success

    if event_type == "payment.captured":
        entity = ((data.get("payment") or {}).get("entity") or {})
        payment_id, order_id, success = _persist_captured_payment(cur, entity, event_ts)
        if not success:
            return payment_id, False

        recovery.cancel_payment_actions(cur, payment_id, "payment_captured")
        recovery.cancel_order_actions(cur, order_id, payment_id, "order_paid")
        recovery.attribute_recovery(cur, order_id, payment_id, captured_at=event_ts)
        return payment_id, True

    if event_type == "payment_link.paid":
        pl_entity = ((data.get("payment_link") or {}).get("entity") or {})
        p_entity = ((data.get("payment") or {}).get("entity") or {})
        link_id = pl_entity.get("id")
        payment_id = p_entity.get("id")

        if not link_id or not payment_id:
            logger.error("payment_link.paid event %s missing link/payment entity IDs", event_id)
            return payment_id, False

        link_status = pl_entity.get("status")
        payment_status = p_entity.get("status")
        if link_status and link_status != "paid":
            logger.warning("payment_link.paid %s has unexpected link status=%s", event_id, link_status)
            return payment_id, False
        if payment_status and payment_status != "captured":
            logger.warning("payment_link.paid %s has unexpected payment status=%s", event_id, payment_status)
            return payment_id, False

        persisted_payment_id, order_id, success = _persist_captured_payment(
            cur,
            p_entity,
            event_ts,
        )
        if not success:
            return persisted_payment_id, False

        if not recovery.handle_paid_link_recovery(
            cur,
            link_id,
            payment_id,
            event_at=event_ts,
        ):
            # The event is valid Razorpay traffic, but the Payment Link is not one of
            # our recorded recovery links. Acknowledge it without creating recovery state.
            logger.info("payment_link.paid %s does not match a RecoverAI attempt", event_id)

        recovery.cancel_payment_actions(cur, payment_id, "payment_link_paid")
        if order_id:
            recovery.cancel_order_actions(cur, order_id, payment_id, "order_paid")
        return payment_id, True

    # Unsupported event types are safely persisted but have no automated action.
    logger.info("Persisted unsupported/non-recovery Razorpay event type=%s id=%s", event_type, event_id)
    return None, True
