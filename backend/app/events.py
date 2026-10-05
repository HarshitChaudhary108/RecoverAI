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
    if not email and isinstance(customer, dict):
        email = customer.get("email")
    if not contact and isinstance(customer, dict):
        contact = customer.get("contact") or customer.get("phone")
    return email, contact


def _persist_captured_payment(cur, entity: Dict[str, Any], event_ts: datetime) -> Tuple[Optional[str], Optional[str], bool]:
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
            'pending', 'open',
            %s, %s
        )
        ON CONFLICT (payment_id) DO UPDATE SET
            order_id = EXCLUDED.order_id,
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


def _persist_failed_payment(cur, entity: Dict[str, Any], event_ts: datetime) -> Tuple[Optional[str], bool]:
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
            order_id = EXCLUDED.order_id,
            amount = EXCLUDED.amount,
            currency = EXCLUDED.currency,
            method = COALESCE(payments.method, EXCLUDED.method),
            bank = COALESCE(payments.bank, EXCLUDED.bank),
            status = CASE WHEN payments.status = 'captured' THEN payments.status ELSE 'failed' END,
            error_code = EXCLUDED.error_code,
            error_reason = EXCLUDED.error_reason,
            error_source = EXCLUDED.error_source,
            error_step = EXCLUDED.error_step,
            customer_email = COALESCE(payments.customer_email, EXCLUDED.customer_email),
            customer_contact = COALESCE(payments.customer_contact, EXCLUDED.customer_contact),
            failed_at = COALESCE(payments.failed_at, EXCLUDED.failed_at),
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
    """Process a verified Razorpay webhook event inside the caller's transaction."""
    if cur is None:
        with get_db_cursor() as transaction_cur:
            return _process_event(event_id, payload, transaction_cur)
    return _process_event(event_id, payload, cur)


def _process_event(event_id: str, payload: dict, cur):
    event_type = payload.get("event")
    event_ts = _event_timestamp(payload)

    if event_type == "payment.failed":
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        payment_id, success = _persist_failed_payment(cur, entity, event_ts)
        if success:
            logger.info("Persisted payment.failed event %s for %s", event_id, payment_id)
        return payment_id, success

    if event_type == "payment.captured":
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        payment_id, order_id, success = _persist_captured_payment(cur, entity, event_ts)
        if not success:
            return payment_id, False

        recovery.cancel_payment_actions(cur, payment_id, "payment_captured")
        if order_id:
            recovery.cancel_order_actions(cur, order_id, payment_id, "order_paid")
            recovery.attribute_recovery(cur, order_id, payment_id)
        return payment_id, True

    if event_type == "payment_link.paid":
        pl_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        p_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        link_id = pl_entity.get("id")
        payment_id = p_entity.get("id")
        link_status = pl_entity.get("status")
        payment_status = p_entity.get("status")

        if link_status and link_status != "paid":
            logger.warning("payment_link.paid event %s contains non-paid link status %s", event_id, link_status)
            return payment_id, False
        if payment_status and payment_status != "captured":
            logger.warning("payment_link.paid event %s contains payment status %s", event_id, payment_status)
            return payment_id, False
        if not link_id or not payment_id:
            logger.error("Missing link_id or payment_id in payment_link.paid event %s", event_id)
            return payment_id, False

        persisted_payment_id, order_id, success = _persist_captured_payment(cur, p_entity, event_ts)
        if not success:
            return persisted_payment_id, False

        # The link event is only meaningful for this recovery system when the link
        # corresponds to a persisted recovery_attempt.
        recovery.handle_paid_link_recovery(cur, link_id, payment_id)
        if order_id:
            recovery.cancel_payment_actions(cur, payment_id, "payment_link_paid")
            recovery.cancel_order_actions(cur, order_id, payment_id, "order_paid")
        return payment_id, True

    # Unsupported webhook types are persisted by webhook.py but deliberately do not
    # trigger classification or recovery behavior.
    return None, True
