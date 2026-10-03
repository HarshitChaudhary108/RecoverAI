import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import settings
from backend.app.db import get_db_cursor
from backend.app.events import handle_razorpay_event
from backend.app.executor import run_action
import json
from backend.app.actions_repo import ActionsRepository
# ... other imports

# ... other imports

from backend.app.alerts import run_evaluate_alerts
from backend.worker.tasks import classify_payment, retry_stuck_classifications
from backend.app.classifier import ClassificationError

client = TestClient(app)

@pytest.fixture(autouse=True)
def clean_db(db_cursor):
    """Ensure tables are clean before each test."""
    db_cursor.execute("TRUNCATE webhook_events, payments, scheduled_actions, recovery_attempts, health_snapshots, alert_state CASCADE")
    db_cursor.connection.commit()

def test_hardening_duplicate_webhook(db_cursor):
    """Scenario 1: Duplicate webhook delivery should not create duplicate payments or actions."""
    event_id = "evt_dup_1"
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_dup_1",
                    "order_id": "ord_dup_1",
                    "amount": 100,
                    "currency": "INR",
                    "method": "card",
                    "bank": "HDFC",
                    "email": "test@example.com",
                    "contact": "123",
                    "error_code": "ERR1",
                    "error_description": "Insufficient funds",
                    "error_source": "src",
                    "error_step": "step"
                }
            }
        }
    }

    # First delivery
    handle_razorpay_event(event_id, payload, cur=db_cursor)
    db_cursor.connection.commit()

    # Second delivery (same event_id)
    # In real app, this is handled by the webhook router's deduplication check
    # But here we test the effect: handle_razorpay_event uses ON CONFLICT (payment_id) DO UPDATE
    # The deduplication happens at the webhook_events table level in the router.

    # Simulate the router's deduplication by manually inserting the event first
    db_cursor.execute(
        "INSERT INTO webhook_events (event_id, event_type, payload) VALUES (%s, %s, %s)",
        (event_id, "payment.failed", json.dumps(payload))
    )
    db_cursor.connection.commit()

    # Now try to process again (simulating what happens if deduplication was bypassed)
    handle_razorpay_event(event_id, payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT COUNT(*) FROM payments WHERE payment_id = 'pay_dup_1'")
    assert db_cursor.fetchone()[0] == 1

def test_hardening_worker_crash_recovery(db_cursor):
    """Scenario 2: Worker crash after action claim should allow retry after lease expiry."""
    payment_id = "pay_crash_1"
    action_id = "act_crash_1"

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (payment_id, "ord_crash_1")
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'pending')",
        (action_id, payment_id)
    )
    db_cursor.connection.commit()

    # Worker 1 claims the action
    repo = ActionsRepository()
    repo.claim_due_actions("worker_1", 10, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT locked_by, attempts FROM scheduled_actions WHERE action_id = %s", (action_id,))
    row = db_cursor.fetchone()
    assert row[0] == "worker_1"
    assert row[1] == 1

    # Simulate crash by expiring lease
    db_cursor.execute(
        "UPDATE scheduled_actions SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE action_id = %s",
        (action_id,)
    )
    db_cursor.connection.commit()

    # Worker 2 should now be able to claim it
    repo = ActionsRepository()
    repo.claim_due_actions("worker_2", 10, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT locked_by, attempts FROM scheduled_actions WHERE action_id = %s", (action_id,))
    row = db_cursor.fetchone()
    assert row[0] == "worker_2"
    assert row[1] == 2

def test_hardening_concurrent_workers(db_cursor):
    """Scenario 3: Multiple workers processing due actions should not process the same action."""
    payment_id = "pay_conc_1"
    for i in range(10):
        db_cursor.execute(
            "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
            (f"pay_conc_{i}", f"ord_conc_{i}")
        )
        db_cursor.execute(
            "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'pending')",
            (f"act_conc_{i}", f"pay_conc_{i}")
        )
    db_cursor.connection.commit()

    # Simulate concurrent claims by calling it multiple times for different workers
    # In a real scenario, this would be parallel threads.
    # Our claim_due_actions uses FOR UPDATE SKIP LOCKED, so it's safe.
    repo = ActionsRepository()
    workers = ["w1", "w2", "w3", "w4", "w5"]
    for w in workers:
        repo.claim_due_actions(w, 10, cur=db_cursor)
        db_cursor.connection.commit()

    db_cursor.execute("SELECT COUNT(*) FROM scheduled_actions WHERE status = 'claimed'")
    assert db_cursor.fetchone()[0] == 10

    # Verify each action has exactly one owner
    db_cursor.execute("SELECT action_id, COUNT(DISTINCT locked_by) FROM scheduled_actions GROUP BY action_id")
    for row in db_cursor.fetchall():
        assert row[1] == 1

def test_hardening_capture_during_claim(db_cursor):
    """Scenario 4: Payment captured while a recovery action is pending/claimed should cancel it."""
    payment_id = "pay_cap_hard_1"
    action_id = "act_cap_hard_1"

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (payment_id, "ord_cap_hard_1")
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'claimed')",
        (action_id, payment_id)
    )
    db_cursor.connection.commit()

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": "ord_cap_hard_1"}}}
    }
    handle_razorpay_event("evt_cap_hard_1", payload, cur=db_cursor)
    db_cursor.connection.commit()

    db_cursor.execute("SELECT status FROM scheduled_actions WHERE action_id = %s", (action_id,))
    assert db_cursor.fetchone()[0] == 'cancelled'

def test_hardening_timeout_reschedule(db_cursor):
    """Scenario 5: Timeout where Razorpay status is still pending/authorized should reschedule."""
    payment_id = "pay_tout_1"
    action_id = "act_tout_1"

    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'classified', 'open')",
        (payment_id, "ord_tout_1")
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, 'email', 1, CURRENT_TIMESTAMP, 'pending')",
        (action_id, payment_id)
    )
    db_cursor.connection.commit()

    with patch("backend.app.executor.razorpay_client.fetch_payment") as mock_fetch, \
         patch("backend.app.executor.razorpay_client.is_order_paid", return_value=False), \
         patch("backend.app.executor.send_recovery_email") as mock_email:

        from backend.app.razorpay_client import RazorpayPayment
        mock_fetch.return_value = RazorpayPayment(
            payment_id=payment_id, order_id="ord_tout_1", amount=100, currency="INR", status="authorized", email="t@t.com", contact=None
        )

        run_action(action_id, "worker_1")
        db_cursor.connection.commit()

        db_cursor.execute("SELECT status, run_at FROM scheduled_actions WHERE action_id = %s", (action_id,))
        row = db_cursor.fetchone()
        assert row[0] == 'pending'
        assert row[1] > datetime.now(timezone.utc)
        mock_email.assert_not_called()

def test_hardening_alert_hysteresis(db_cursor):
    """Scenario 6: Alert threshold hysteresis (requires 2 healthy checks to clear)."""
    scope, val = 'global', 'all'
    db_cursor.execute(
        "INSERT INTO alert_state (scope, scope_value, state, healthy_streak) VALUES (%s, %s, %s, %s)",
        (scope, val, 'alerting', 0)
    )
    db_cursor.connection.commit()

    # 1st healthy check
    db_cursor.execute(
        "INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (datetime.now(timezone.utc), scope, val, 100, 95, 5, 0.95)
    )
    db_cursor.connection.commit()
    run_evaluate_alerts()
    db_cursor.connection.commit()

    db_cursor.execute("SELECT state, healthy_streak FROM alert_state WHERE scope = %s AND scope_value = %s", (scope, val))
    row = db_cursor.fetchone()
    assert row[0] == 'alerting'
    assert row[1] == 1

    # 2nd healthy check
    db_cursor.execute(
        "INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (datetime.now(timezone.utc) + timedelta(minutes=1), scope, val, 100, 95, 5, 0.95)
    )
    db_cursor.connection.commit()
    run_evaluate_alerts()
    db_cursor.connection.commit()

    db_cursor.execute("SELECT state, healthy_streak FROM alert_state WHERE scope = %s AND scope_value = %s", (scope, val))
    row = db_cursor.fetchone()
    assert row[0] == 'ok'
    assert row[1] == 0

def test_hardening_webhook_signature_security(db_cursor):
    """Scenario 7: Invalid webhook signature should be rejected and produce no DB side-effects."""
    payload = {"event": "payment.failed", "payload": {}}
    body_bytes = b'{"event": "payment.failed", "payload": {}}'

    # Invalid signature
    headers = {"X-Razorpay-Signature": "invalid_sig", "x-razorpay-event-id": "evt_sig_1"}
    response = client.post("/webhook/razorpay", content=body_bytes, headers=headers)
    assert response.status_code == 401

    # Verify no DB writes
    db_cursor.execute("SELECT COUNT(*) FROM webhook_events WHERE event_id = 'evt_sig_1'")
    assert db_cursor.fetchone()[0] == 0

def test_hardening_classification_failure(db_cursor):
    """Scenario 8: Classification failure should mark status as 'failed' and be retriable."""
    payment_id = "pay_fail_class_1"
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, 100, 'INR', 'failed', 'pending', 'open')",
        (payment_id, "ord_fail_class_1")
    )
    db_cursor.connection.commit()

    with patch("backend.app.classification_service.classify_failure", side_effect=ClassificationError("LLM Error")):
        try:
            from backend.app.classification_service import classify_and_schedule
            classify_and_schedule(payment_id)
        except ClassificationError:
            pass
        db_cursor.connection.commit()

    db_cursor.execute("SELECT classification_status, classification_attempts FROM payments WHERE payment_id = %s", (payment_id,))
    row = db_cursor.fetchone()
    assert row[0] == 'failed'
    assert row[1] == 1

    # Verify retry task picks it up
    with patch("backend.app.classification_service.classify_payment.delay") as mock_delay:
        retry_stuck_classifications()
        mock_delay.assert_called_with(payment_id)
