import pytest
from psycopg import errors

def test_duplicate_event_id_rejected(db_cursor):
    """Verify that duplicate webhook event IDs are rejected."""
    sql = "INSERT INTO webhook_events (event_id, event_type, payload) VALUES (%s, %s, %s)"
    params = ("evt_123", "payment.failed", '{"foo": "bar"}')

    db_cursor.execute(sql, params)

    with pytest.raises(errors.UniqueViolation):
        db_cursor.execute(sql, params)

def test_duplicate_scheduled_action_rejected(db_cursor):
    """Verify that duplicate (payment_id, action_type, step) is rejected."""
    # Setup payment first
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("pay_1", "ord_1", 100, "INR", "failed", "pending", "open")
    )

    sql = "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, %s, %s, %s, %s)"
    params = ("act_1", "pay_1", "send_email", 1, "2026-01-01 00:00:00+00", "pending")

    db_cursor.execute(sql, params)

    with pytest.raises(errors.UniqueViolation):
        db_cursor.execute(sql, ("act_2", "pay_1", "send_email", 1, "2026-01-01 00:00:00+00", "pending"))

def test_duplicate_recovery_attempt_action_id_rejected(db_cursor):
    """Verify that duplicate recovery_attempts.action_id is rejected."""
    # Setup payment and action
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("pay_2", "ord_2", 100, "INR", "failed", "pending", "open")
    )
    db_cursor.execute(
        "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, %s, %s, %s, %s)",
        ("act_2", "pay_2", "send_email", 1, "2026-01-01 00:00:00+00", "pending")
    )

    sql = "INSERT INTO recovery_attempts (id, action_id, original_payment_id, status) VALUES (%s, %s, %s, %s)"
    params = ("att_1", "act_2", "pay_2", "sent")

    db_cursor.execute(sql, params)

    with pytest.raises(errors.UniqueViolation):
        db_cursor.execute(sql, ("att_2", "act_2", "pay_2", "sent"))

def test_invalid_status_values_rejected(db_cursor):
    """Verify that invalid status values are rejected via CHECK constraints."""
    # Test payments.classification_status
    with pytest.raises(errors.CheckViolation):
        db_cursor.execute(
            "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("pay_3", "ord_3", 100, "INR", "failed", "invalid_status", "open")
        )
    db_cursor.execute("ROLLBACK")

    # Test scheduled_actions.status
    db_cursor.execute(
        "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("pay_4", "ord_4", 100, "INR", "failed", "pending", "open")
    )
    with pytest.raises(errors.CheckViolation):
        db_cursor.execute(
            "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES (%s, %s, %s, %s, %s, %s)",
            ("act_3", "pay_4", "send_email", 1, "2026-01-01 00:00:00+00", "invalid_status")
        )
    db_cursor.execute("ROLLBACK")
