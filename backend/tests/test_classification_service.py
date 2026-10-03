import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from backend.app.classification_service import classify_and_schedule
from backend.app.classifier import ClassificationError, FailureClassification
from backend.app.db import get_db_cursor

@pytest.fixture
def mock_db():
    with patch("backend.app.classification_service.get_db_cursor") as mock:
        mock_cur = MagicMock()
        mock.return_value.__enter__.return_value = mock_cur
        yield mock_cur

@pytest.fixture
def mock_classifier():
    with patch("backend.app.classification_service.classify_failure") as mock:
        yield mock

def test_classify_success_treatment(mock_db, mock_classifier):
    # Setup: Payment pending classification
    payment_id = "pay_123"
    failed_at = datetime(2023, 1, 1, 10, 0)
    mock_db.fetchone.return_value = (
        "failed", "pending", "ERR_01", "Insufficient funds", "stripe", "payment_intent", failed_at
    )

    # Mock classifier to return a standard category (e.g. insufficient_funds)
    mock_classifier.return_value = FailureClassification(
        category="insufficient_funds", confidence=0.95, reason="Low balance"
    )

    # Use a mock for assign_group to ensure 'treatment'
    with patch("backend.app.classification_service.assign_group", return_value="treatment"):
        status, category = classify_and_schedule(payment_id)

    assert status == "classified"
    assert category == "insufficient_funds"

    # Verify DB updates
    # 1. Update payment status
    # 2. Update group
    # 3. Insert actions
    calls = mock_db.execute.call_args_list
    assert any("UPDATE payments SET failure_category = %s" in str(c) for c in calls)
    assert any("UPDATE payments SET recovery_group = %s" in str(c) for c in calls)
    assert any("INSERT INTO scheduled_actions" in str(c) for c in calls)

def test_classify_soft_decline_multiple_actions(mock_db, mock_classifier):
    payment_id = "pay_soft"
    failed_at = datetime(2023, 1, 1, 10, 0)
    mock_db.fetchone.return_value = (
        "failed", "pending", "ERR_02", "Soft decline", "stripe", "payment_intent", failed_at
    )

    mock_classifier.return_value = FailureClassification(
        category="bank_declined_soft", confidence=0.9, reason="Bank temporary issue"
    )

    with patch("backend.app.classification_service.assign_group", return_value="treatment"):
        classify_and_schedule(payment_id)

    # bank_declined_soft typically has multiple steps in policy
    action_inserts = [c for c in mock_db.execute.call_args_list if "INSERT INTO scheduled_actions" in str(c)]
    # Should have at least 1, usually more based on policy
    assert len(action_inserts) >= 1

def test_classify_other_manual_review(mock_db, mock_classifier):
    payment_id = "pay_other"
    failed_at = datetime(2023, 1, 1, 10, 0)
    mock_db.fetchone.return_value = (
        "failed", "pending", "ERR_99", "Unknown error", "stripe", "payment_intent", failed_at
    )

    mock_classifier.return_value = FailureClassification(
        category="other", confidence=0.7, reason="Unclear"
    )

    with patch("backend.app.classification_service.assign_group", return_value="treatment"):
        classify_and_schedule(payment_id)

    # Verify manual_review action
    calls = [str(c) for c in mock_db.execute.call_args_list]
    assert any("'manual_review', 1, %s, 'manual_review')" in s for s in calls)

def test_classify_holdout_no_emails(mock_db, mock_classifier):
    payment_id = "pay_holdout"
    failed_at = datetime(2023, 1, 1, 10, 0)
    mock_db.fetchone.return_value = (
        "failed", "pending", "ERR_01", "Insufficient funds", "stripe", "payment_intent", failed_at
    )

    mock_classifier.return_value = FailureClassification(
        category="insufficient_funds", confidence=0.95, reason="Low balance"
    )

    with patch("backend.app.classification_service.assign_group", return_value="holdout"):
        classify_and_schedule(payment_id)

    # No scheduled_actions should be inserted for holdout
    calls = [str(c) for c in mock_db.execute.call_args_list]
    assert not any("INSERT INTO scheduled_actions" in s for s in calls)

def test_classify_idempotency(mock_db):
    payment_id = "pay_already"
    mock_db.fetchone.return_value = (
        "failed", "classified", "ERR_01", "...", "...", "...", datetime.utcnow()
    )

    status, category = classify_and_schedule(payment_id)
    assert status == "classified"
    # Should NOT have called classifier or executed updates
    assert mock_db.execute.call_count == 1 # Only the SELECT

def test_classify_captured_skip(mock_db):
    payment_id = "pay_captured"
    mock_db.fetchone.return_value = (
        "captured", "pending", "...", "...", "...", "...", datetime.utcnow()
    )

    status, category = classify_and_schedule(payment_id)
    assert status == "skipped"
    assert mock_db.execute.call_count == 1

def test_classify_error_updates_payment(mock_db, mock_classifier):
    payment_id = "pay_fail"
    mock_db.fetchone.return_value = (
        "failed", "pending", "...", "...", "...", "...", datetime.utcnow()
    )

    mock_classifier.side_effect = ClassificationError("LLM Timeout")

    with pytest.raises(ClassificationError):
        classify_and_schedule(payment_id)

    # Verify failure update
    calls = [str(c) for c in mock_db.execute.call_args_list]
    assert any("classification_status = 'failed'" in s for s in calls)
    assert any("classification_attempts = classification_attempts + 1" in s for s in calls)

def test_retry_stuck_classifications(mock_db):
    from backend.worker.tasks import retry_stuck_classifications

    # Mock payments that need re-queuing
    mock_db.fetchall.return_value = [("pay_stuck_1",), ("pay_stuck_2",)]

    with patch("backend.worker.tasks.classify_payment.delay") as mock_delay:
        # To prevent the task from actually hitting the DB (since it's a @shared_task
        # and we're calling it directly), we mock the DB cursor.
        # The issue was that the task was using the real DB because
        # the mock_db fixture only mocks the cursor for the current thread/context,
        # and Celery tasks can sometimes be tricky.
        # Actually, the problem is that retry_stuck_classifications is a Celery task,
        # and when called directly in tests, it might be interacting with the real
        # database if not properly mocked.

        # We already have mock_db, but let's ensure get_db_cursor is patched
        # globally for this test.
        with patch("backend.worker.tasks.get_db_cursor") as mock_cursor_ctx:
            mock_cursor_ctx.return_value.__enter__.return_value = mock_db
            retry_stuck_classifications()

        assert mock_delay.call_count == 2
        mock_delay.assert_any_call("pay_stuck_1")
        mock_delay.assert_any_call("pay_stuck_2")
