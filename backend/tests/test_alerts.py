import pytest
from datetime import datetime, timedelta
from backend.app.db import get_db_cursor
from backend.app.alerts import run_evaluate_alerts, next_state
from backend.app.config import settings

def test_next_state_hysteresis():
    """
    Test the pure state transition function.
    Hysteresis: 2 healthy checks to clear.
    """
    # 1. ok -> alerting (Condition True)
    state, streak = next_state('ok', 0, True)
    assert state == 'alerting'
    assert streak == 0

    # 2. alerting -> alerting (Condition True)
    state, streak = next_state('alerting', 0, True)
    assert state == 'alerting'
    assert streak == 0

    # 3. alerting -> alerting (Condition False, streak 1)
    state, streak = next_state('alerting', 0, False)
    assert state == 'alerting'
    assert streak == 1

    # 4. alerting -> ok (Condition False, streak 2)
    state, streak = next_state('alerting', 1, False)
    assert state == 'ok'
    assert streak == 0

def test_alert_trigger_floor():
    """Trigger alert when success rate is below absolute floor."""
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM health_snapshots")
        cur.execute("DELETE FROM alert_state")

        now = datetime.utcnow().replace(second=0, microsecond=0)
        # Below floor (e.g., 50% < 70%)
        cur.execute("""
            INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (now, 'global', 'all', 100, 50, 50, 0.5))

    run_evaluate_alerts()

    with get_db_cursor() as cur:
        cur.execute("SELECT state FROM alert_state WHERE scope = 'global' AND scope_value = 'all'")
        state = cur.fetchone()[0]
        assert state == 'alerting'

def test_alert_trigger_baseline():
    """Trigger alert when success rate drops significantly from baseline."""
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM health_snapshots")
        cur.execute("DELETE FROM alert_state")

        now = datetime.utcnow().replace(second=0, microsecond=0)

        # 1. Seed historical baseline: 95% success rate for the same hour
        # We need a few days of data
        for i in range(1, 8):
            ts = now - timedelta(days=i)
            cur.execute("""
                INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (ts, 'global', 'all', 100, 95, 5, 0.95))

        # 2. Current: 75% (Above floor of 70%, but 20% drop from 95% baseline)
        cur.execute("""
            INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (now, 'global', 'all', 100, 75, 25, 0.75))

    run_evaluate_alerts()

    with get_db_cursor() as cur:
        cur.execute("SELECT state FROM alert_state WHERE scope = 'global' AND scope_value = 'all'")
        state = cur.fetchone()[0]
        assert state == 'alerting'

def test_alert_min_attempts():
    """Verify that low volume does not trigger alerts."""
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM health_snapshots")
        cur.execute("DELETE FROM alert_state")

        now = datetime.utcnow().replace(second=0, microsecond=0)
        # Success rate 0%, but only 5 attempts (< ALERT_MIN_ATTEMPTS)
        cur.execute("""
            INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (now, 'global', 'all', 5, 0, 5, 0.0))

    run_evaluate_alerts()

    with get_db_cursor() as cur:
        cur.execute("SELECT state FROM alert_state WHERE scope = 'global' AND scope_value = 'all'")
        row = cur.fetchone()
        # Might not exist if no transition happened and it defaults to 'ok' in logic,
        # but our logic inserts 'ok' or updates.
        state = row[0] if row else 'ok'
        assert state == 'ok'
