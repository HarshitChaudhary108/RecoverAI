import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

import razorpay

from backend.app.config import settings


@dataclass(frozen=True)
class RazorpayPayment:
    payment_id: str
    order_id: Optional[str]
    amount: int
    currency: str
    status: str
    email: Optional[str]
    contact: Optional[str]


class TemporaryProviderError(Exception):
    """Provider/network condition that should be retried."""


class PermanentProviderError(Exception):
    """Provider request that should not be retried unchanged."""


def payment_link_reference_id(action_id: str) -> str:
    """Create a deterministic Razorpay reference_id <= 40 characters."""
    digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:36]
    return f"rai_{digest}"


class RazorpayClient:
    def __init__(self) -> None:
        self.client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )

    @staticmethod
    def _provider_error(exc: Exception, operation: str) -> Exception:
        status = getattr(exc, "status_code", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None

        if status == 429 or (status is not None and status >= 500):
            return TemporaryProviderError(f"Razorpay {operation} failed: {exc}")
        if status is not None and 400 <= status < 500:
            return PermanentProviderError(f"Razorpay {operation} failed: {exc}")

        if isinstance(exc, (razorpay.errors.ServerError, razorpay.errors.GatewayError)):
            return TemporaryProviderError(f"Razorpay {operation} failed: {exc}")
        if isinstance(exc, razorpay.errors.BadRequestError):
            return PermanentProviderError(f"Razorpay {operation} failed: {exc}")
        return TemporaryProviderError(f"Unexpected Razorpay error during {operation}: {exc}")

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        try:
            payment = self.client.payment.fetch(payment_id)
            return RazorpayPayment(
                payment_id=payment["id"],
                order_id=payment.get("order_id"),
                amount=payment["amount"],
                currency=payment["currency"],
                status=payment["status"],
                email=payment.get("email"),
                contact=payment.get("contact"),
            )
        except Exception as exc:
            raise self._provider_error(exc, "fetch payment") from exc

    def is_order_paid(self, order_id: str) -> bool:
        try:
            order = self.client.order.fetch(order_id)
            return order.get("status") == "paid"
        except Exception as exc:
            raise self._provider_error(exc, "fetch order") from exc

    def find_payment_link_by_reference_id(
        self,
        reference_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Recover a previously-created Payment Link after a worker crash/network timeout."""
        try:
            response = self.client.payment_link.all(
                {"reference_id": reference_id, "count": 10}
            )
            items = response.get("items", []) if isinstance(response, dict) else []
            if not items:
                return None
            return items[0]
        except Exception as exc:
            raise self._provider_error(exc, "find payment link") from exc

    def create_payment_link(
        self,
        amount: int,
        currency: str,
        customer_email: str,
        description: str,
        action_id: str,
        original_payment_id: str,
        expiry_unix: int,
    ) -> tuple[str, str]:
        """Create or recover a deterministic Payment Link for an action."""
        reference_id = payment_link_reference_id(action_id)

        existing = self.find_payment_link_by_reference_id(reference_id)
        if existing:
            link_id = existing.get("id")
            link_url = existing.get("short_url")
            if link_id and link_url:
                return str(link_id), str(link_url)
            raise PermanentProviderError(
                f"Payment Link with reference_id={reference_id} has incomplete provider data"
            )

        data = {
            "amount": int(amount),
            "currency": currency,
            "accept_notes": False,
            "customer": {"email": customer_email},
            "description": description,
            "reference_id": reference_id,
            "notes": {
                "original_payment_id": original_payment_id,
                "action_id": action_id,
            },
            "notify": {"sms": False, "email": False},
            "expire_by": int(expiry_unix),
        }

        try:
            link = self.client.payment_link.create(data=data)
        except razorpay.errors.BadRequestError as exc:
            # A concurrent worker/provider retry may already have created the same
            # unique reference_id. Resolve the provider-side resource before failing.
            existing = self.find_payment_link_by_reference_id(reference_id)
            if existing:
                link_id = existing.get("id")
                link_url = existing.get("short_url")
                if link_id and link_url:
                    return str(link_id), str(link_url)
            raise PermanentProviderError(
                f"Invalid request to create Payment Link: {exc}"
            ) from exc
        except Exception as exc:
            raise self._provider_error(exc, "create payment link") from exc

        link_id = link.get("id")
        link_url = link.get("short_url")
        if not link_id or not link_url:
            raise TemporaryProviderError(
                "Razorpay created a Payment Link without id/short_url"
            )
        return str(link_id), str(link_url)
