from dataclasses import dataclass
from typing import Optional
import razorpay
from backend.app.config import settings
from backend.app.db import get_db_cursor

@dataclass
class RazorpayPayment:
    payment_id: str
    order_id: Optional[str]
    amount: int
    currency: str
    status: str
    email: Optional[str]
    contact: Optional[str]

class TemporaryProviderError(Exception):
    """Network problem, rate limit, 5xx"""
    pass

class PermanentProviderError(Exception):
    """Bad request, 4xx"""
    pass

class RazorpayClient:
    def __init__(self):
        self.client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        try:
            payment = self.client.payment.fetch(payment_id)
            return RazorpayPayment(
                payment_id=payment['id'],
                order_id=payment.get('order_id'),
                amount=payment['amount'],
                currency=payment['currency'],
                status=payment['status'],
                email=payment.get('email'),
                contact=payment.get('contact')
            )
        except razorpay.errors.BadRequestError as e:
            raise PermanentProviderError(f"Invalid payment ID: {e}")
        except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as e:
            raise TemporaryProviderError(f"Razorpay server error: {e}")
        except Exception as e:
            raise TemporaryProviderError(f"Unexpected error fetching payment: {e}")

    def is_order_paid(self, order_id: str) -> bool:
        try:
            order = self.client.order.fetch(order_id)
            return order.get('status') == 'paid'
        except razorpay.errors.BadRequestError as e:
            raise PermanentProviderError(f"Invalid order ID: {e}")
        except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as e:
            raise TemporaryProviderError(f"Razorpay server error: {e}")
        except Exception as e:
            raise TemporaryProviderError(f"Unexpected error checking order: {e}")

    def create_payment_link(
        self,
        amount: int,
        currency: str,
        customer_email: str,
        description: str,
        action_id: str,
        original_payment_id: str,
        expiry_unix: int
    ) -> tuple[str, str]:
        """
        Creates a payment link.
        Amount is expected in smallest currency unit (paise for INR).
        """
        try:
            data = {
                "amount": amount,
                "currency": currency,
                "accept_notes": False,
                "customer": {"email": customer_email},
                "description": description,
                "reference_id": action_id,
                "notes": {
                    "original_payment_id": original_payment_id,
                    "action_id": action_id
                },
                "notify": {"sms": False, "email": False},
                "expire_by": expiry_unix
            }
            link = self.client.payment_link.create(data=data)
            return link['id'], link['short_url']
        except razorpay.errors.BadRequestError as e:
            raise PermanentProviderError(f"Invalid request to create link: {e}")
        except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as e:
            raise TemporaryProviderError(f"Razorpay server error: {e}")
        except Exception as e:
            raise TemporaryProviderError(f"Unexpected error creating link: {e}")
