import resend
from backend.app.config import settings

class TemporaryProviderError(Exception):
    """Network problem, rate limit, 5xx"""
    pass

class PermanentProviderError(Exception):
    """Bad request, 4xx"""
    pass

def send_recovery_email(
    to: str,
    amount: int,
    currency: str,
    link_url: str,
    kind: str,
    suggest_other_method: bool,
    idempotency_key: str
):
    """
    Sends a recovery email using Resend.
    Amount is in smallest currency unit (paise for INR).
    """
    resend.api_key = settings.EMAIL_API_KEY

    # Convert paise to rupees for display
    amount_rupees = amount / 100.0 if currency == "INR" else amount

    if kind == "recovery":
        subject = "Payment Action Required"
        body_text = (
            f"Hello,\n\nWe noticed your payment of ₹{amount_rupees:.2f} could not be completed. "
            f"You can complete it now using this secure link: {link_url}\n\n"
        )
        body_html = (
            f"<p>Hello,</p><p>We noticed your payment of <strong>₹{amount_rupees:.2f}</strong> "
            f"could not be completed. You can complete it now using this secure link: "
            f"<a href='{link_url}'>{link_url}</a></p>"
        )
    elif kind == "soft_reminder":
        subject = "Payment Reminder"
        body_text = (
            f"Hello,\n\nThis is a friendly reminder regarding your payment of ₹{amount_rupees:.2f}. "
            f"If you haven't already, you can complete it here: {link_url}\n\n"
        )
        body_html = (
            f"<p>Hello,</p><p>This is a friendly reminder regarding your payment of "
            f"<strong>₹{amount_rupees:.2f}</strong>. If you haven't already, you can "
            f"complete it here: <a href='{link_url}'>{link_url}</a></p>"
        )
    else:
        raise PermanentProviderError(f"Unsupported email kind: {kind}")

    if suggest_other_method:
        suggestion = "\nIf you prefer, you can also use a different payment method."
        suggestion_html = "<p>If you prefer, you can also use a different payment method.</p>"
        body_text += suggestion
        body_html += suggestion_html

    try:
        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": [to],
            "subject": subject,
            "html": body_html,
            "text": body_text,
            "headers": {
                "Idempotency-Key": idempotency_key
            }
        })
    except Exception as e:
        # Resend SDK doesn't always provide granular error codes in the base Exception
        # We check common patterns or just treat as temporary if it's a connection issue
        err_msg = str(e).lower()
        if "400" in err_msg or "invalid" in err_msg:
            raise PermanentProviderError(f"Resend bad request: {e}")
        if "429" in err_msg or "500" in err_msg or "timeout" in err_msg:
            raise TemporaryProviderError(f"Resend temporary failure: {e}")
        raise TemporaryProviderError(f"Unexpected Resend error: {e}")
