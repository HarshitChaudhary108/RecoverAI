import pytest
import threading
from datetime import datetime, timedelta
from backend.app.actions_repo import ActionsRepository, ClaimError
from backend.app.db import get_db_cursor

@pytest.fixture
def repo():
    return ActionsRepository()

@pytest.fixture
def clean_actions():
    """Clear scheduled_actions before each test."""
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM recovery_attempts")
        cur.execute("DELETE FROM scheduled_actions")
        cur.execute("DELETE FROM payments")

    # Create a dummy payment for foreign key constraints
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO payments (payment_id, order_id, amount, currency, status, classification_status, recovery_status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("pay_123", "ord_123", 1000, "INR", "failed", "classified", "open")
        )

def test_claim_due_actions_basic(repo, clean_actions):
    # Setup: one due, one future
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s), (%s, %s, %s, %s, %s, %s)",
            (
                "act_due", "pay_123", "email", 1, datetime.utcnow() - timedelta(minutes=1), "pending",
                "act_future", "pay_123", "email", 2, datetime.utcnow() + timedelta(minutes=1), "pending"
            )
        )

    claimed = repo.claim_due_actions("worker_1", limit=10)
    assert len(claimed) == 1
    assert claimed[0]["action_id"] == "act_due"
    assert claimed[0]["status"] == "claimed"
    assert claimed[0]["locked_by"] == "worker_1"
    assert claimed[0]["attempts"] == 1

def test_claim_cancelled_not_claimed(repo, clean_actions):
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("act_cancelled", "pay_123", "email", 1, datetime.utcnow() - timedelta(minutes=1), "cancelled")
        )

    claimed = repo.claim_due_actions("worker_1", limit=10)
    assert len(claimed) == 0

def test_concurrent_claims(repo, clean_actions):
    # Setup: 10 due actions
    with get_db_cursor() as cur:
        for i in range(10):
            cur.execute(
                "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (f"act_{i}", "pay_123", "email", i, datetime.utcnow() - timedelta(minutes=1), "pending")
            )

    results = []
    def worker_task(worker_id):
        claimed = repo.claim_due_actions(worker_id, limit=5)
        results.append((worker_id, [r["action_id"] for r in claimed]))

    t1 = threading.Thread(target=worker_task, args=("worker_1",))
    t2 = threading.Thread(target=worker_task, args=("worker_2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    all_claimed_ids = []
    for worker_id, ids in results:
        all_claimed_ids.extend(ids)

    assert len(all_claimed_ids) == 10
    assert len(set(all_claimed_ids)) == 10  # No duplicates

def test_lease_expiry_retry(repo, clean_actions):
    # 1. Claim an action
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("act_crash", "pay_123", "email", 1, datetime.utcnow() - timedelta(minutes=1), "pending")
        )

    repo.claim_due_actions("worker_1", limit=1)

    # 2. Simulate crash by expiring lease
    with get_db_cursor() as cur:
        cur.execute(
            "UPDATE scheduled_actions SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE action_id = %s",
            ("act_crash",)
        )

    # 3. Claim again
    claimed = repo.claim_due_actions("worker_2", limit=1)
    assert len(claimed) == 1
    assert claimed[0]["action_id"] == "act_crash"
    assert claimed[0]["locked_by"] == "worker_2"
    assert claimed[0]["attempts"] == 2

def test_ownership_guard(repo, clean_actions):
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_actions (action_id, payment_id, action_type, step, run_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("act_own", "pay_123", "email", 1, datetime.utcnow() - timedelta(minutes=1), "pending")
        )

    repo.claim_due_actions("worker_1", limit=1)

    # Worker 2 tries to complete it
    with pytest.raises(ClaimError):
        repo.complete_action("act_own", "worker_2")

    # Worker 1 should be able to complete it
    repo.complete_action("act_own", "worker_1")

    with get_db_cursor() as cur:
        cur.execute("SELECT status FROM scheduled_actions WHERE action_id = %s", ("act_own",))
        assert cur.fetchone()[0] == "completed"
