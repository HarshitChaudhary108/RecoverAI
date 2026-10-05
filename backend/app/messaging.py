import resend

from backend.app.config import settings
from backend.app.razorpay_client import PermanentProviderError, TemporaryProviderError


def _classify_resend_error(exc: Exception) -> Exception:
    text = str(exc).lower()
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)

    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None

    if status_code == 429 or status_code is not None and status_code >= 500:
        return TemporaryProviderError(str(exc))
    if status_code is not None and 400 <= status_code < 500:
        return PermanentProviderError(str(exc))
    if any(token in text for token in ("timeout", "timed out", "connection", "dns", "temporarily", "rate limit", "429")):
        return TemporaryProviderError(str(exc))
    if any(token in text for token in ("invalid", "unauthorized", "forbidden", "unprocessable", "400", "401", "403", "404")):
        return PermanentProviderError(str(exc))
    return TemporaryProviderError(str(exc))


def send_recovery_email(
    to: str,
    amount: int,
    currency: str,
    link_url: str,
    kind: str,
    suggest_other_method: bool,
    idempotency_key: str,
):
    """Send one customer recovery email through Resend with provider idempotency."""
    if not to:
        raise PermanentProviderError("Customer email is missing")
    if not link_url:
        raise PermanentProviderError("Recovery Payment Link URL is missing")

    resend.api_key = settings.EMAIL_API_KEY

    display_amount = amount / 100.0 if currency.upper() == "INR" else amount

    if kind == "recovery":
        subject = "Payment Action Required"
        body_text = (
            f"Hello,\n\nWe noticed your payment of {currency} {display_amount:.2f} "
            f"could not be completed. You can complete it using this secure link: {link_url}\n\n"
        )
        body_html = (
            f"<p>Hello,</p><p>We noticed your payment of <strong>{currency} {display_amount:.2f}</strong> "
            f"could not be completed. You can complete it using this secure link: "
            f"<a href=\"{link_url}\">Complete payment</a>.</p>"
        )
    elif kind == "soft_reminder":
        subject = "Payment Reminder"
        body_text = (
            f"Hello,\n\nThis is a friendly reminder regarding your payment of "
            f"{currency} {display_amount:.2f}. If you have not already, you can complete it here: {link_url}\n\n"
        )
        body_html = (
            f"<p>Hello,</p><p>This is a friendly reminder regarding your payment of "
            f"<strong>{currency} {display_amount:.2f}</strong>. If you have not already, "
            f"you can complete it here: <a href=\"{link_url}\">Complete payment</a>.</p>"
        )
    else:
        raise PermanentProviderError(f"Unsupported email kind: {kind}")

    if suggest_other_method:
        body_text += "If you prefer, you can also use a different payment method."
        body_html += "<p>If you prefer, you can also use a different payment method.</p>"

    try:
        # Current Resend Python SDK supports idempotency as the second parameter.
        return resend.Emails.send(
            {
                "from": "onboarding@resend.dev",
                "to": [to],
                "subject": subject,
                "html": body_html,
                "text": body_text,
            },
            {"idempotencyKey": idempotency_key},
        )
    except Exception as exc:
        raise _classify_resend_error(exc) from exc
