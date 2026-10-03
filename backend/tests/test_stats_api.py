import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.db import pool

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_test_db():
    # Clear and seed a tiny known dataset for precise counting
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE payments, scheduled_actions, recovery_attempts, health_snapshots, alert_state CASCADE;")

            # Case 1: Captured
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failed_at, updated_at) VALUES ('p1', 'o1', 100, 'INR', 'captured', 'recovered', NULL, NOW())")

            # Case 2: Failed, Eligible, Recovered
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category, recovery_group, failed_at, updated_at) VALUES ('p2', 'o2', 200, 'INR', 'failed', 'recovered', 'insufficient_funds', 'treatment', NOW(), NOW())")

            # Case 3: Failed, Eligible, Open
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category, recovery_group, failed_at, updated_at) VALUES ('p3', 'o3', 300, 'INR', 'failed', 'open', 'bank_declined_soft', 'treatment', NOW(), NOW())")

            # Case 4: Failed, Not Eligible, Open
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category, recovery_group, failed_at, updated_at) VALUES ('p4', 'o4', 400, 'INR', 'failed', 'open', 'other', 'not_eligible', NOW(), NOW())")

            # Case 5: Failed, User Cancelled (excluded from success rate)
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category, recovery_group, failed_at, updated_at) VALUES ('p5', 'o5', 500, 'INR', 'failed', 'open', 'user_cancelled', NULL, NOW(), NOW())")

            # Case 6: Failed, Holdout, Open
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category, recovery_group, failed_at, updated_at) VALUES ('p6', 'o6', 600, 'INR', 'failed', 'open', 'timeout', 'holdout', NOW(), NOW())")

            # Recovery Attempt for p2
            cur.execute("INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) VALUES ('act1', 'p2', 'send_recovery_email', 1, NOW(), 'completed')")
            cur.execute("INSERT INTO recovery_attempts (id, action_id, original_payment_id, channel, recovery_group, delay_used, sent_at, status) VALUES ('a1', 'act1', 'p2', 'email', 'treatment', '2m', NOW(), 'sent')")

            # Health snapshot
            cur.execute("INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate) VALUES (NOW(), 'global', 'all', 10, 7, 3, 70.0)")

            # Alert
            cur.execute("INSERT INTO alert_state (scope, scope_value, state, healthy_streak, last_success_rate, last_baseline, last_attempts, state_changed_at, updated_at) VALUES ('bank', 'Chase', 'alerting', 0, 60.0, 70.0, 20, NOW(), NOW())")

            conn.commit()

def test_summary_stats():
    # Captured: 1
    # Failed (excl user_cancelled): p2, p3, p4, p6 = 4
    # Total effective: 5
    # Success Rate: 1/5 * 100 = 20.0
    # Failed Count: p2, p3, p4, p5, p6 = 5
    # Recovered Count: p1, p2 = 2
    # Revenue Recovered: p1 + p2 = 100 + 200 = 300
    # Revenue at Risk: p3, p4, p6 = 300 + 400 + 600 = 1300

    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data['success_rate'] == 20.0
    assert data['failed_count'] == 5
    assert data['recovered_count'] == 2
    assert data['revenue_recovered'] == 300
    assert data['revenue_at_risk'] == 1300

def test_recovery_funnel():
    # Failed: p2, p3, p4, p5, p6 = 5
    # Eligible: p2, p3, p6 = 3
    # Emails Sent: a1 = 1
    # Recovered: p1, p2 = 2
    # Treatment: p2, p3 -> 2. Recovered: p2 -> 1. Rate: 50%
    # Holdout: p6 -> 1. Recovered: 0. Rate: 0%
    # Self Recovered: 0

    response = client.get("/api/recovery/funnel")
    assert response.status_code == 200
    data = response.json()
    assert data['failed'] == 5
    assert data['eligible'] == 3
    assert data['emails_sent'] == 1
    assert data['recovered'] == 2
    assert data['treatment_count'] == 2
    assert data['treatment_rate'] == 50.0
    assert data['holdout_count'] == 1
    assert data['holdout_rate'] == 0.0
    assert data['self_recovered'] == 0

def test_invalid_hours():
    response = client.get("/api/stats/summary?hours=0")
    assert response.status_code == 422
    response = client.get("/api/stats/summary?hours=169")
    assert response.status_code == 422

def test_wrong_method():
    response = client.post("/api/stats/summary")
    assert response.status_code == 405

def test_alerts():
    response = client.get("/api/alerts")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['scope'] == 'bank'
    assert data[0]['scope_value'] == 'Chase'
    assert data[0]['state'] == 'alerting'

def test_zero_state():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE payments, recovery_attempts CASCADE;")
            conn.commit()

    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data['success_rate'] == 0.0
    assert data['failed_count'] == 0
    assert data['recovered_count'] == 0
    assert data['revenue_recovered'] == 0
    assert data['revenue_at_risk'] == 0

    response = client.get("/api/recovery/funnel")
    assert response.status_code == 200
    data = response.json()
    assert data['failed'] == 0
    assert data['eligible'] == 0
    assert data['emails_sent'] == 0
    assert data['recovered'] == 0

def test_full_success_rate():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE payments CASCADE;")
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status) VALUES ('p1', 'o1', 100, 'INR', 'captured', 'recovered')")
            conn.commit()

    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data['success_rate'] == 100.0

def test_zero_success_rate():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE payments CASCADE;")
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category) VALUES ('p1', 'o1', 100, 'INR', 'failed', 'open', 'insufficient_funds')")
            conn.commit()

    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data['success_rate'] == 0.0

def test_exclusion_only_rate():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE payments CASCADE;")
            cur.execute("INSERT INTO payments (payment_id, order_id, amount, currency, status, recovery_status, failure_category) VALUES ('p1', 'o1', 100, 'INR', 'failed', 'open', 'user_cancelled')")
            conn.commit()

    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    data = response.json()
    # Denominator should be 0, success_rate should be 0.0 (via Service default)
    assert data['success_rate'] == 0.0
