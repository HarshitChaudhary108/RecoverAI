import argparse
import hashlib
import hmac
import json
import httpx
import logging
import sys
from backend.app.config import settings

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def generate_signature(secret, payload_bytes):
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

def send_webhook(event, event_id, payment_id, bad_signature=False, error_code=None, error_reason=None):
    url = "http://localhost:8000/webhook/razorpay"

    # Base payload structure based on provider_notes.md
    payload = {
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": f"order_{payment_id}",
                    "amount": 50000,
                    "currency": "INR",
                    "method": "card",
                    "bank": "HDFC",
                    "status": "failed" if event == "payment.failed" else "captured",
                    "email": "test@example.com",
                    "contact": "+919876543210",
                    "error_code": error_code or "BAD_REQUEST",
                    "error_description": error_reason or "Payment failed for some reason",
                    "error_source": "razorpay",
                    "error_step": "authorization"
                }
            }
        }
    }

    if event == "payment_link.paid":
        # Adjust payload for payment_link.paid
        payload["payload"]["payment_link"] = {"entity": {"id": f"pl_{payment_id}"}}
        payload["payload"]["payment"] = {"entity": {"id": payment_id, "order_id": f"order_{payment_id}"}}

    # Use separators to ensure a compact JSON representation identical to what a server would produce
    body_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    signature = generate_signature(settings.RAZORPAY_WEBHOOK_SECRET, body_bytes)

    if bad_signature:
        signature = "invalid_signature_12345"

    headers = {
        "X-Razorpay-Signature": signature,
        "x-razorpay-event-id": event_id,
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client() as client:
            response = client.post(url, content=body_bytes, headers=headers)
            logger.info(f"Sent {event} (ID: {event_id}) -> Status: {response.status_code}, Body: {response.text}")
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send test Razorpay webhooks")
    parser.add_argument("--event", choices=["failed", "captured", "link_paid"], required=True)
    parser.add_argument("--event-id", default="evt_test_123")
    parser.add_argument("--payment-id", default="pay_test_123")
    parser.add_argument("--bad-signature", action="store_true")
    parser.add_argument("--error-code", default=None)
    parser.add_argument("--error-reason", default=None)

    args = parser.parse_args()

    event_map = {
        "failed": "payment.failed",
        "captured": "payment.captured",
        "link_paid": "payment_link.paid"
    }

    send_webhook(
        event=event_map[args.event],
        event_id=args.event_id,
        payment_id=args.payment_id,
        bad_signature=args.bad_signature,
        error_code=args.error_code,
        error_reason=args.error_reason
    )
