import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from backend.app.main import app
from backend.app.db import get_db_cursor
from backend.worker.tasks import classify_payment
from backend.app.classifier import ClassificationError
from backend.app.config import settings
import hmac
import hashlib
import json

def generate_signature(payload: str):
    """Generates a valid Razorpay webhook signature for tests."""
    return hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture(autouse=True)
def mock_celery_eager():
    """Force Celery to run tasks synchronously for integration tests."""
    from celery import current_app
    current_app.conf.task_always_eager = True
    yield
    current_app.conf.task_always_eager = False

@pytest.fixture
def mock_classification():
    """Mocks the LLM classification to return a predictable result."""
    with patch("backend.app.classification_service.classify_failure") as mock:
        mock_result = MagicMock()
        mock_result.category = "insufficient_funds"
        mock_result.confidence = 0.95
        mock_result.reason = "Customer has insufficient funds in account."
        mock.return_value = mock_result
        yield mock

@pytest.fixture
def mock_email():
    """Mocks the email sending provider."""
    with patch("backend.app.messaging.send_recovery_email") as mock:
        yield mock

def test_end_to_end_recovery_flow(client, db_cursor, mock_classification, mock_email):
    """
    Tests the flow: Webhook -> DB -> Classification -> Scheduled Action.
    """
    payment_id = "pay_test_123"
    order_id = "order_test_123"

    payload = {
        "event": "payment.failed",
        "created_at": datetime.utcnow().isoformat(),
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 50000,
                    "currency": "INR",
                    "method": "card",
                    "bank": "HDFC",
                    "email": "test@example.com",
                    "contact": "+919876543210",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Insufficient funds",
                    "error_source": "bank",
                    "error_step": "authorization",
                    "created_at": datetime.utcnow().isoformat()
                }
            }
        }
    }

    body = json.dumps(payload)
    headers = {
        "X-Razorpay-Signature": generate_signature(body),
        "x-razorpay-event-id": "evt_test_123"
    }

    # 1. POST the webhook
    response = client.post("/webhook/razorpay", content=body, headers=headers)
    assert response.status_code == 200

    # 2. Verify payment is created and pending classification
    db_cursor.execute("SELECT status, classification_status, failed_at FROM payments WHERE payment_id = %s", (payment_id,))
    row = db_cursor.fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] in ["pending", "classified"]
    assert row[2] is not None

    # 3. Trigger classification (normally happens via Celery, here eager)
    classify_payment(payment_id)

    # 4. Verify classification and scheduled actions
    db_cursor.execute("SELECT failure_category, classification_status FROM payments WHERE payment_id = %s", (payment_id,))
    row = db_cursor.fetchone()
    assert row[0] == "insufficient_funds"
    assert row[1] == "classified"

    db_cursor.execute("SELECT count(*) FROM scheduled_actions WHERE payment_id = %s", (payment_id,))
    count = db_cursor.fetchone()[0]
    assert count > 0

def test_webhook_deduplication(client, db_cursor, mock_classification):
    """Tests that duplicate webhooks do not create duplicate payments or actions."""
    payment_id = "pay_dup_123"
    event_id = "evt_dup_123"

    payload = {
        "event": "payment.failed",
        "id": event_id,
        "created_at": datetime.utcnow().isoformat(),
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": "order_dup_123",
                    "amount": 1000,
                    "currency": "INR",
                    "method": "upi",
                    "bank": "ICICI",
                    "email": "dup@example.com",
                    "contact": "123",
                    "error_code": "ERR",
                    "error_description": "ERR",
                    "error_source": "S",
                    "error_step": "ST",
                    "created_at": datetime.utcnow().isoformat()
                }
            }
        }
    }

    body = json.dumps(payload)
    headers = {
        "X-Razorpay-Signature": generate_signature(body),
        "x-razorpay-event-id": "evt_dup_123"
    }

    # First request
    client.post("/webhook/razorpay", content=body, headers=headers)
    # Second request (duplicate)
    client.post("/webhook/razorpay", content=body, headers=headers)

    # Verify only one payment record
    db_cursor.execute("SELECT count(*) FROM payments WHERE payment_id = %s", (payment_id,))
    assert db_cursor.fetchone()[0] == 1

    # Verify only one set of actions (after classification)
    classify_payment(payment_id)
    db_cursor.execute("SELECT count(*) FROM scheduled_actions WHERE payment_id = %s", (payment_id,))
    count = db_cursor.fetchone()[0]
    classify_payment(payment_id)
    db_cursor.execute("SELECT count(*) FROM scheduled_actions WHERE payment_id = %s", (payment_id,))
    assert db_cursor.fetchone()[0] == count

def test_recovery_cancellation_on_capture(client, db_cursor, mock_classification):
    """Tests that payment.captured cancels pending recovery actions."""
    payment_id = "pay_cap_123"
    order_id = "order_cap_123"

    # 1. Setup failed payment
    payload_fail = {
        "event": "payment.failed",
        "id": "evt_fail_1",
        "created_at": datetime.utcnow().isoformat(),
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 1000,
                    "currency": "INR",
                    "method": "card",
                    "bank": "HDFC",
                    "email": "cap@example.com",
                    "contact": "123",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Insufficient funds",
                    "error_source": "bank",
                    "error_step": "authorization",
                    "created_at": datetime.utcnow().isoformat()
                }
            }
        }
    }
    body_fail = json.dumps(payload_fail)
    headers_fail = {
        "X-Razorpay-Signature": generate_signature(body_fail),
        "x-razorpay-event-id": "evt_fail_1"
    }
    client.post("/webhook/razorpay", content=body_fail, headers=headers_fail)
    classify_payment(payment_id)

    # Verify actions exist
    db_cursor.execute("SELECT count(*) FROM scheduled_actions WHERE payment_id = %s AND status = 'pending'", (payment_id,))
    assert db_cursor.fetchone()[0] > 0

    # 2. Send payment.captured
    payload_cap = {
        "event": "payment.captured",
        "id": "evt_cap_1",
        "created_at": datetime.utcnow().isoformat(),
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id
                }
            }
        }
    }
    body_cap = json.dumps(payload_cap)
    headers_cap = {
        "X-Razorpay-Signature": generate_signature(body_cap),
        "x-razorpay-event-id": "evt_cap_1"
    }
    client.post("/webhook/razorpay", content=body_cap, headers=headers_cap)

    # 3. Verify status and cancellation
    db_cursor.execute("SELECT status, captured_at FROM payments WHERE payment_id = %s", (payment_id,))
    row = db_cursor.fetchone()
    assert row[0] == "captured"
    assert row[1] is not None

    db_cursor.execute("SELECT count(*) FROM scheduled_actions WHERE payment_id = %s AND status = 'pending'", (payment_id,))
    assert db_cursor.fetchone()[0] == 0
    db_cursor.execute("SELECT count(*) FROM scheduled_actions WHERE payment_id = %s AND status = 'cancelled'", (payment_id,))
    assert db_cursor.fetchone()[0] > 0

def test_classification_retry_resiliency(client, db_cursor):
    """Tests that processing exceptions do not permanently dedupe the event and can be retried."""
    payment_id = "pay_retry_123"

    payload = {
        "event": "payment.failed",
        "id": "evt_retry_123",
        "created_at": datetime.utcnow().isoformat(),
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": "order_retry_123",
                    "amount": 1000,
                    "currency": "INR",
                    "method": "upi",
                    "bank": "ICICI",
                    "email": "retry@example.com",
                    "contact": "123",
                    "error_code": "ERR",
                    "error_description": "ERR",
                    "error_source": "S",
                    "error_step": "ST",
                    "created_at": datetime.utcnow().isoformat()
                }
            }
        }
    }
    body = json.dumps(payload)
    headers = {
        "X-Razorpay-Signature": generate_signature(body),
        "x-razorpay-event-id": "evt_retry_123"
    }

    # 1. Webhook creates payment
    with patch("backend.app.classification_service.classify_failure", side_effect=ClassificationError("LLM Timeout")):
        client.post("/webhook/razorpay", content=body, headers=headers)

    # 2. Verify status is 'failed'
    db_cursor.execute("SELECT classification_status FROM payments WHERE payment_id = %s", (payment_id,))
    assert db_cursor.fetchone()[0] == "failed"

    # 3. Fix failure and retry
    with patch("backend.app.classifier.classify_failure") as mock:
        mock_result = MagicMock()
        mock_result.category = "other"
        mock_result.confidence = 0.8
        mock_result.reason = "Fixed"
        mock.return_value = mock_result

        classify_payment(payment_id)

    # Verify eventual success
    db_cursor.execute("SELECT classification_status FROM payments WHERE payment_id = %s", (payment_id,))
    assert db_cursor.fetchone()[0] == "classified"
