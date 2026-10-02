import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from backend.app.executor import run_action
from backend.app.razorpay_client import RazorpayPayment, TemporaryProviderError, PermanentProviderError
from backend.app.actions_repo import ClaimError

# Constants for testing
WORKER_ID = "test-worker-1"
ACTION_ID = "act_123"
PAYMENT_ID = "pay_456"

@pytest.fixture
def mock_repo():
    with patch("backend.app.executor.actions_repo") as mock:
        yield mock

@pytest.fixture
def mock_razorpay():
    with patch("backend.app.executor.razorpay_client") as mock:
        yield mock

@pytest.fixture
def mock_messaging():
    with patch("backend.app.executor.send_recovery_email") as mock:
        yield mock

@pytest.fixture
def mock_db():
    with patch("backend.app.executor.get_db_cursor") as mock:
        # Mock the context manager: get_db_cursor() -> context_manager -> cursor
        mock_cursor = MagicMock()
        mock.return_value.__enter__.return_value = mock_cursor
        yield mock_cursor

@pytest.fixture
def mock_now():
    with patch("backend.app.executor.datetime") as mock_datetime:
        # We need to mock datetime.now() and potentially other datetime methods
        # Set to a time that is NOT in quiet hours (e.g., 12:00 PM UTC)
        fixed_now = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))
        mock_datetime.now.return_value = fixed_now
        # Since we patched the whole datetime module, we must provide the class
        # for the executor to instantiate objects like timedelta or for logic
        # that doesn't use the mocked .now(). Actually, better to patch only .now.
        yield fixed_now

# Redefining mocks to avoid datetime patching issues in executor.py
@pytest.fixture
def fixed_now():
    return datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

def test_run_action_happy_path(mock_repo, mock_razorpay, mock_messaging, mock_db):
    with patch("backend.app.executor.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

        # Setup
        mock_repo.still_owns_claim.return_value = True
        mock_repo.get_action_and_payment.return_value = (
            {"action_id": ACTION_ID, "action_type": "send_recovery_email", "attempts": 1},
            {"payment_id": PAYMENT_ID, "amount": 1000, "currency": "INR", "email": "user@example.com", "failure_category": "insufficient_funds", "failed_at": datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))}
        )

        mock_razorpay.fetch_payment.return_value = RazorpayPayment(
            payment_id=PAYMENT_ID, order_id="ord_1", amount=1000, currency="INR", status="failed", email="user@example.com", contact=None
        )
        mock_razorpay.is_order_paid.return_value = False
        mock_razorpay.create_payment_link.return_value = ("link_abc", "https://rzp.io/i/abc")

        # Mock DB responses
        mock_db.fetchone.side_effect = [[0], None]

        result = run_action(ACTION_ID, WORKER_ID)

        assert result == "completed"
        mock_messaging.assert_called_once()
        mock_repo.complete_action_cur.assert_called_once()

def test_run_action_already_paid(mock_repo, mock_razorpay, mock_messaging, mock_db):
    with patch("backend.app.executor.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

        mock_repo.still_owns_claim.return_value = True
        mock_repo.get_action_and_payment.return_value = (
            {"action_id": ACTION_ID, "action_type": "send_recovery_email", "attempts": 1},
            {"payment_id": PAYMENT_ID, "amount": 1000, "currency": "INR", "email": "user@example.com", "failure_category": "insufficient_funds", "failed_at": datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))}
        )

        mock_razorpay.fetch_payment.return_value = RazorpayPayment(
            payment_id=PAYMENT_ID, order_id="ord_1", amount=1000, currency="INR", status="captured", email="user@example.com", contact=None
        )
        mock_razorpay.is_order_paid.return_value = True

        result = run_action(ACTION_ID, WORKER_ID)

        assert result == "cancelled_paid"
        mock_repo.cancel_action.assert_called_with(ACTION_ID, WORKER_ID, "already_paid")
        mock_messaging.assert_not_called()

def test_run_action_timeout_pending(mock_repo, mock_razorpay, mock_messaging, mock_db):
    with patch("backend.app.executor.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

        mock_repo.still_owns_claim.return_value = True
        mock_repo.get_action_and_payment.return_value = (
            {"action_id": ACTION_ID, "action_type": "send_recovery_email", "attempts": 1, "recheck_count": 0},
            {"payment_id": PAYMENT_ID, "amount": 1000, "currency": "INR", "email": "user@example.com", "failure_category": "timeout", "failed_at": datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))}
        )

        mock_razorpay.fetch_payment.return_value = RazorpayPayment(
            payment_id=PAYMENT_ID, order_id="ord_1", amount=1000, currency="INR", status="authorized", email="user@example.com", contact=None
        )
        mock_razorpay.is_order_paid.return_value = False

        result = run_action(ACTION_ID, WORKER_ID)

        assert result == "rescheduled_timeout"
        mock_repo.reschedule_action.assert_called()
        mock_messaging.assert_not_called()

def test_run_action_max_messages(mock_repo, mock_razorpay, mock_messaging, mock_db):
    with patch("backend.app.executor.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

        mock_repo.still_owns_claim.return_value = True
        mock_repo.get_action_and_payment.return_value = (
            {"action_id": ACTION_ID, "action_type": "send_recovery_email", "attempts": 1},
            {"payment_id": PAYMENT_ID, "amount": 1000, "currency": "INR", "email": "user@example.com", "failure_category": "insufficient_funds", "failed_at": datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))}
        )

        mock_razorpay.fetch_payment.return_value = RazorpayPayment(
            payment_id=PAYMENT_ID, order_id="ord_1", amount=1000, currency="INR", status="failed", email="user@example.com", contact=None
        )
        mock_razorpay.is_order_paid.return_value = False

        # 3 messages already sent
        mock_db.fetchone.return_value = [3]

        result = run_action(ACTION_ID, WORKER_ID)

        assert result == "cancelled_max_messages"
        mock_repo.cancel_action.assert_called_with(ACTION_ID, WORKER_ID, "max_messages")
        mock_messaging.assert_not_called()

def test_run_action_temporary_error(mock_repo, mock_razorpay, mock_messaging, mock_db):
    with patch("backend.app.executor.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

        mock_repo.still_owns_claim.return_value = True
        mock_repo.get_action_and_payment.return_value = (
            {"action_id": ACTION_ID, "action_type": "send_recovery_email", "attempts": 1},
            {"payment_id": PAYMENT_ID, "amount": 1000, "currency": "INR", "email": "user@example.com", "failure_category": "insufficient_funds", "failed_at": datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))}
        )

        # Razorpay throws temp error
        mock_razorpay.fetch_payment.side_effect = TemporaryProviderError("Network timeout")

        result = run_action(ACTION_ID, WORKER_ID)

        assert result == "rescheduled_temporary"
        mock_repo.reschedule_action.assert_called()

def test_run_action_permanent_error(mock_repo, mock_razorpay, mock_messaging, mock_db):
    with patch("backend.app.executor.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))

        mock_repo.still_owns_claim.return_value = True
        mock_repo.get_action_and_payment.return_value = (
            {"action_id": ACTION_ID, "action_type": "send_recovery_email", "attempts": 1},
            {"payment_id": PAYMENT_ID, "amount": 1000, "currency": "INR", "email": "user@example.com", "failure_category": "insufficient_funds", "failed_at": datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))}
        )

        mock_razorpay.fetch_payment.return_value = RazorpayPayment(
            payment_id=PAYMENT_ID, order_id="ord_1", amount=1000, currency="INR", status="failed", email="user@example.com", contact=None
        )
        mock_razorpay.is_order_paid.return_value = False

        # Fix: mock_db.fetchone is called multiple times.
        # 1st call: count(*) for sent_count
        # 2nd call: check existing link
        mock_db.fetchone.side_effect = [[0], None]
        mock_razorpay.create_payment_link.return_value = ("link_abc", "url")

        # Resend throws permanent error
        mock_messaging.side_effect = PermanentProviderError("Invalid email format")

        result = run_action(ACTION_ID, WORKER_ID)

        assert result == "failed_permanent"
        mock_repo.fail_action.assert_called_with(ACTION_ID, WORKER_ID, "Invalid email format")
