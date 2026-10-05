import html
import logging

import resend

from backend.app.config import settings
from backend.app.razorpay_client import PermanentProviderError, TemporaryProviderError

logger = logging.getLogger(__name__)


def _classify_resend_error(exc: Exception) -> Exception:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None

    message = str(exc).lower()
    if status_code == 429 or (status_code is not None and status_code >= 500):
        return TemporaryProviderError(f"Resend temporary failure: {exc}")
    if status_code is not None and 400 <= status_code < 500:
        return PermanentProviderError(f"Resend permanent failure: {exc}")
    if any(token in message for token in ("timeout", "timed out", "connection", "dns", "rate limit")):
        return TemporaryProviderError(f"Resend temporary failure: {exc}")
    if any(token in message for token in ("invalid", "unauthorized", "forbidden", "unprocessable")):
        return PermanentProviderError(f"Resend permanent failure: {exc}")
    return TemporaryProviderError(f"Unknown Resend failure: {exc}")


def send_recovery_email(
    to: str,
    amount: int,
    currency: str,
    link_url: str,
    kind: str,
    suggest_other_method: bool,
    idempotency_key: str,
):
    """Send one customer-facing recovery email through Resend."""
    if not to:
        raise PermanentProviderError("Customer email is missing")
    if not link_url:
        raise PermanentProviderError("Recovery Payment Link URL is missing")
    resend.api_key = settings.EMAIL_API_KEY
    display_amount = amount / 100.0 if currency.upper() == "INR" else amount

    safe_link = html.escape(link_url, quote=True)

    if kind == "recovery":
        subject = "Payment Action Required"
        body_text = (
            f"Hello,\n\nWe noticed your payment of {currency} {display_amount:.2f} "
            f"could not be completed. You can complete it using this secure link: {link_url}\n\n"
        )
        body_html = (
            f"<p>Hello,</p><p>We noticed your payment of "
            f"<strong>{currency} {display_amount:.2f}</strong> could not be completed. "
            f"You can complete it using this secure link: "
            f'<a href="{safe_link}">Complete payment</a>.</p>'
        )
    elif kind == "soft_reminder":
        subject = "Payment Reminder"
        body_text = (
            f"Hello,\n\nThis is a friendly reminder regarding your payment of "
            f"{currency} {display_amount:.2f}. If you have not already, you can complete it here: "
            f"{link_url}\n\n"
        )
        body_html = (
            f"<p>Hello,</p><p>This is a friendly reminder regarding your payment of "
            f"<strong>{currency} {display_amount:.2f}</strong>. If you have not already, "
            f'you can complete it here: <a href="{safe_link}">Complete payment</a>.</p>'
        )
    else:
        raise PermanentProviderError(f"Unsupported email kind: {kind}")

    if suggest_other_method:
        body_text += "If you prefer, you can also use a different payment method."
        body_html += "<p>If you prefer, you can also use a different payment method.</p>"

    try:
        params = {
                "from": settings.EMAIL_FROM,
                "to": [to],
                "subject": subject,
                "html": body_html,
                "text": body_text,
        }
        try:
            return resend.Emails.send(params, idempotency_key=idempotency_key)
        except TypeError as exc:
            # Compatibility fallback for older Resend SDK builds that expose the
            # idempotency key as the second options mapping.
            if "idempotency" not in str(exc).lower():
                raise
            return resend.Emails.send(params, {"idempotencyKey": idempotency_key})
    except Exception as exc:
        raise _classify_resend_error(exc) from exc
