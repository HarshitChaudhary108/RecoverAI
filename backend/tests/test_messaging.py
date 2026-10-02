import pytest
from unittest.mock import patch
from backend.app.messaging import send_recovery_email, TemporaryProviderError, PermanentProviderError

@patch('resend.Emails.send')
def test_send_recovery_email_success(mock_send):
    send_recovery_email(
        to="test@example.com",
        amount=50000,
        currency="INR",
        link_url="https://rzp.io/i/link",
        kind="recovery",
        suggest_other_method=True,
        idempotency_key="idemp_123"
    )

    # Verify call arguments
    args, kwargs = mock_send.call_args
    params = args[0] if args else kwargs.get('params', {}) # resend.Emails.send takes one dict
    # The SDK call is resend.Emails.send({...})

    # Since mock_send is called as resend.Emails.send({...})
    call_data = mock_send.call_args[0][0]

    assert call_data['to'] == ["test@example.com"]
    assert "₹500.00" in call_data['text']
    assert "₹500.00" in call_data['html']
    assert "idempotency_key" in call_data['headers'] or "Idempotency-Key" in call_data['headers']
    assert "different payment method" in call_data['text']

@patch('resend.Emails.send')
def test_send_soft_reminder_success(mock_send):
    send_recovery_email(
        to="test@example.com",
        amount=10000,
        currency="INR",
        link_url="https://rzp.io/i/link",
        kind="soft_reminder",
        suggest_other_method=False,
        idempotency_key="idemp_456"
    )

    call_data = mock_send.call_args[0][0]
    assert "Payment Reminder" in call_data['subject']
    assert "friendly reminder" in call_data['text']
    assert "different payment method" not in call_data['text']

@patch('resend.Emails.send')
def test_send_email_permanent_error(mock_send):
    mock_send.side_effect = Exception("400 Bad Request")

    with pytest.raises(PermanentProviderError):
        send_recovery_email(
            to="bad@email", amount=100, currency="INR", link_url="url",
            kind="recovery", suggest_other_method=False, idempotency_key="k"
        )

@patch('resend.Emails.send')
def test_send_email_temporary_error(mock_send):
    mock_send.side_effect = Exception("429 Too Many Requests")

    with pytest.raises(TemporaryProviderError):
        send_recovery_email(
            to="test@email", amount=100, currency="INR", link_url="url",
            kind="recovery", suggest_other_method=False, idempotency_key="k"
        )
