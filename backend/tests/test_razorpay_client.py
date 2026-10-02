import pytest
from unittest.mock import MagicMock, patch
import razorpay
from backend.app.razorpay_client import (
    RazorpayClient,
    TemporaryProviderError,
    PermanentProviderError,
    RazorpayPayment
)

@patch('razorpay.Client')
def test_fetch_payment_success(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_client.payment.fetch.return_value = {
        'id': 'pay_123',
        'order_id': 'ord_123',
        'amount': 1000,
        'currency': 'INR',
        'status': 'failed',
        'email': 'test@example.com',
        'contact': '1234567890'
    }

    client = RazorpayClient()
    payment = client.fetch_payment('pay_123')

    assert isinstance(payment, RazorpayPayment)
    assert payment.payment_id == 'pay_123'
    assert payment.amount == 1000

@patch('razorpay.Client')
def test_fetch_payment_permanent_error(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_client.payment.fetch.side_effect = razorpay.errors.BadRequestError("Invalid ID")

    client = RazorpayClient()
    with pytest.raises(PermanentProviderError):
        client.fetch_payment('invalid_id')

@patch('razorpay.Client')
def test_fetch_payment_temporary_error(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_client.payment.fetch.side_effect = razorpay.errors.ServerError("Server Error")

    client = RazorpayClient()
    with pytest.raises(TemporaryProviderError):
        client.fetch_payment('pay_123')

@patch('razorpay.Client')
def test_is_order_paid_success(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_client.order.fetch.return_value = {'status': 'paid'}

    client = RazorpayClient()
    assert client.is_order_paid('ord_123') is True

@patch('razorpay.Client')
def test_create_payment_link_success(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_client.payment_link.create.return_value = {
        'id': 'link_123',
        'short_url': 'https://rzp.io/i/link_123'
    }

    client = RazorpayClient()
    link_id, link_url = client.create_payment_link(
        amount=10000, currency="INR", customer_email="test@test.com",
        description="desc", action_id="act1", original_payment_id="p1",
        expiry_unix=12345678
    )

    assert link_id == 'link_123'
    assert link_url == 'https://rzp.io/i/link_123'

    # Verify notifications are OFF
    args, kwargs = mock_client.payment_link.create.call_args
    data = kwargs['data']
    assert data['notify'] == {"sms": False, "email": False}
    assert data['reference_id'] == "act1"
    assert data['notes'] == {"original_payment_id": "p1", "action_id": "act1"}
