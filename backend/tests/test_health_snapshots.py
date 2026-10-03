import pytest
from datetime import datetime, timedelta
from backend.app.db import get_db_cursor
from backend.app.health import run_snapshot_health
from backend.app.config import settings

def test_snapshot_health_math():
    """
    Test success rate calculation:
    - 10 captured
    - 5 failed (category 'other')
    - 2 failed (category 'user_cancelled')
    - 3 pending (status 'pending')
    Result: attempts=15, captured=10, failed=5, rate=10/15=0.666...
    """
    with get_db_cursor() as cur:
        # Clear any existing simulation data and associated actions to avoid FK violations
        cur.execute("DELETE FROM scheduled_actions")
        cur.execute("DELETE FROM recovery_attempts")
        cur.execute("DELETE FROM payments")
        cur.execute("DELETE FROM health_snapshots")

        # Captured
        for i in range(10):
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status, payment_created_at) VALUES (%s, %s, 100, 'INR', 'captured', 'classified', 'recovered', %s)",
                        (f"p_cap_{i}", f"o_{i}", datetime.utcnow()))

        # Failed - Other
        for i in range(5):
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, failure_category, classification_status, recovery_status, payment_created_at) VALUES (%s, %s, 100, 'INR', 'failed', 'other', 'classified', 'open', %s)",
                        (f"p_fail_oth_{i}", f"o_{i}", datetime.utcnow()))

        # Failed - User Cancelled (Should be excluded)
        for i in range(2):
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, failure_category, classification_status, recovery_status, payment_created_at) VALUES (%s, %s, 100, 'INR', 'failed', 'user_cancelled', 'classified', 'open', %s)",
                        (f"p_fail_can_{i}", f"o_{i}", datetime.utcnow()))

        # Pending (Should be excluded)
        for i in range(3):
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status, payment_created_at) VALUES (%s, %s, 100, 'INR', 'pending', 'pending', 'open', %s)",
                        (f"p_pend_{i}", f"o_{i}", datetime.utcnow()))

    run_snapshot_health()

    with get_db_cursor() as cur:
        cur.execute("SELECT attempts, captured, failed, success_rate FROM health_snapshots WHERE scope = 'global'")
        row = cur.fetchone()
        assert row is not None
        attempts, captured, failed, rate = row
        assert attempts == 15
        assert captured == 10
        assert failed == 5
        assert rate == pytest.approx(10/15)

def test_snapshot_health_empty_attempts():
    """Verify success_rate is NULL when attempts=0."""
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM payments")

    run_snapshot_health()

    with get_db_cursor() as cur:
        cur.execute("SELECT success_rate FROM health_snapshots WHERE scope = 'global'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None

def test_snapshot_health_idempotency():
    """Running snapshot twice in one minute should not create duplicate rows."""
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM payments")
        cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status, payment_created_at) VALUES (%s, %s, 100, 'INR', 'captured', 'classified', 'recovered', %s)",
                    ("p1", "o1", datetime.utcnow()))

    run_snapshot_health()
    run_snapshot_health()

    with get_db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM health_snapshots")
        count = cur.fetchone()[0]
        # global, bank(null), method(null) - only global should be there
        assert count == 1
