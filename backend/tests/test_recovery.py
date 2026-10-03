import pytest
from datetime import datetime, timedelta, timezone
from backend.app.db import get_db_cursor
from backend.app.events import handle_razorpay_event

@pytest.fixture(autouse=True)
def clean_db(db_cursor):
    """Ensure tables are clean before each test."""
    db_cursor.execute("TRUNCATE payments, scheduled_actions, recovery_attempts CASCADE")
    db_cursor.connection.commit()

def test_captured_cancels_own_actions(db_cursor):
    """captured payment should cancel its own pending/claimed actions."""
    payment_id = "pay_123"
    order_id = "ord_123"
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (payment_id, order_id)
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'pending')",
        ("act_1", payment_id)
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 2, CURRENT_TIMESTAMP, 'claimed')",
        ("act_2", payment_id)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT status FROM scheduled_actions WHERE action_id = 'act_1'")
    assert db_cursor.fetchone()[0] == 'cancelled'
    db_cursor.execute("SELECT status FROM scheduled_actions WHERE action_id = 'act_2'")
    assert db_cursor.fetchone()[0] == 'cancelled'

def test_captured_cancels_earlier_failed_payments_actions(db_cursor):
    """captured payment should cancel pending actions of other failed payments on the same order."""
    pay_a = "pay_a"
    pay_b = "pay_b"
    order_id = "ord_123"
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_a, order_id)
    )
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_b, order_id)
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'pending')",
        ("act_b", pay_b)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": pay_a, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT status FROM scheduled_actions WHERE action_id = 'act_b'")
    assert db_cursor.fetchone()[0] == 'cancelled'

def test_attribution_email_within_48h(db_cursor):
    """email sent 2 hours before capture = recovered."""
    pay_failed = "pay_fail"
    pay_cap = "pay_cap"
    order_id = "ord_123"
    sent_at = datetime.now(timezone.utc) - timedelta(hours=2)

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_failed, order_id)
    )
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_cap, order_id)
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'completed')",
        ("act_1", pay_failed)
    )
    db_cursor.execute(
        "INSERT INTO recovery_attempts (id, action_id, original_payment_id, channel, sent_at) VALUES (%s, %s, %s, 'email', %s)",
        ("att_1", "act_1", pay_failed, sent_at)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": pay_cap, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT recovery_status FROM payments WHERE payment_id = %s", (pay_failed,))
    assert db_cursor.fetchone()[0] == 'recovered'
    db_cursor.execute("SELECT recovered_payment_id FROM recovery_attempts WHERE id = 'att_1'")
    assert db_cursor.fetchone()[0] == pay_cap

def test_attribution_email_after_48h(db_cursor):
    """capture 50 hours after email = not recovered."""
    pay_failed = "pay_fail"
    pay_cap = "pay_cap"
    order_id = "ord_123"
    sent_at = datetime.now(timezone.utc) - timedelta(hours=50)

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_failed, order_id)
    )
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_cap, order_id)
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'completed')",
        ("act_1", pay_failed)
    )
    db_cursor.execute(
        "INSERT INTO recovery_attempts (id, action_id, original_payment_id, channel, sent_at) VALUES (%s, %s, %s, 'email', %s)",
        ("att_1", "act_1", pay_failed, sent_at)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": pay_cap, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT recovery_status FROM payments WHERE payment_id = %s", (pay_failed,))
    assert db_cursor.fetchone()[0] == 'self_recovered'

def test_attribution_no_email(db_cursor):
    """no email = self_recovered."""
    pay_failed = "pay_fail"
    pay_cap = "pay_cap"
    order_id = "ord_123"

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_failed, order_id)
    )
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_cap, order_id)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": pay_cap, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT recovery_status FROM payments WHERE payment_id = %s", (pay_failed,))
    assert db_cursor.fetchone()[0] == 'self_recovered'

def test_paid_link_recovery(db_cursor):
    """paid-link event marks recovered."""
    pay_fail = "pay_fail"
    pay_cap = "pay_cap"
    order_id = "ord_123"
    link_id = "plink_123"

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_fail, order_id)
    )
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_cap, order_id)
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'pending')",
        ("act_1", pay_fail)
    )
    db_cursor.execute(
        "INSERT INTO recovery_attempts (id, action_id, original_payment_id, link_id, channel, sent_at) VALUES (%s, %s, %s, %s, 'email', CURRENT_TIMESTAMP)",
        ("att_1", "act_1", pay_fail, link_id)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment": {"entity": {"id": pay_cap}},
            "payment_link": {"entity": {"id": link_id}}
        }
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT recovery_status FROM payments WHERE payment_id = %s", (pay_fail,))
    assert db_cursor.fetchone()[0] == 'recovered'
    db_cursor.execute("SELECT recovered_payment_id FROM recovery_attempts WHERE id = 'att_1'")
    assert db_cursor.fetchone()[0] == pay_cap
    db_cursor.execute("SELECT status FROM scheduled_actions WHERE action_id = 'act_1'")
    assert db_cursor.fetchone()[0] == 'cancelled'

def test_unmatched_paid_link_ignored(db_cursor):
    """unmatched paid-link event is ignored."""
    pay_cap = "pay_cap"
    link_id = "plink_missing"

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment": {"entity": {"id": pay_cap}},
            "payment_link": {"entity": {"id": link_id}}
        }
    }
    handle_razorpay_event("evt_1", payload)
    assert True

def test_idempotency_capture(db_cursor):
    """the same capture event twice changes nothing."""
    pay_id = "pay_123"
    order_id = "ord_123"
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_id, order_id)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": pay_id, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT status FROM payments WHERE payment_id = %s", (pay_id,))
    assert db_cursor.fetchone()[0] == 'captured'

def test_holdout_captured_self_recovered(db_cursor):
    """holdout then captured = self_recovered."""
    pay_id = "pay_hold"
    order_id = "ord_123"
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (pay_id, order_id)
    )
    db_cursor.execute(
        "UPDATE payments SET recovery_group = 'holdout' WHERE payment_id = %s",
        (pay_id,)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": pay_id, "order_id": order_id}}}
    }
    handle_razorpay_event("evt_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT recovery_status FROM payments WHERE payment_id = %s", (pay_id,))
    assert db_cursor.fetchone()[0] == 'self_recovered'
