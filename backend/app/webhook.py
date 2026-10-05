import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.app import events
from backend.app.config import settings
from backend.app.db import get_db_cursor
from backend.worker.tasks import classify_payment

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    """Verify, durably persist, and acknowledge a Razorpay webhook quickly."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")

    expected_signature = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_id = request.headers.get("x-razorpay-event-id")
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event ID")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("event")
    payment_id = None

    try:
        # Event persistence and business-state persistence share one DB transaction.
        # If processing fails, the transaction rolls back and Razorpay receives 5xx,
        # allowing delivery to be retried rather than permanently losing the event.
        with get_db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhook_events (event_id, event_type, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, event_type, json.dumps(payload)),
            )
            if cur.rowcount == 0:
                logger.info("Duplicate Razorpay webhook event_id=%s", event_id)
                return JSONResponse({"status": "ok"}, status_code=200)

            payment_id, success = events.handle_razorpay_event(
                event_id,
                payload,
                cur=cur,
            )
            if not success:
                raise RuntimeError(f"Razorpay event processing failed: {event_type}")

        # Only failed payments require LLM classification. Captured/payment-link events
        # are already fully handled by their deterministic event processors.
        if payment_id and event_type == "payment.failed":
            try:
                classify_payment.delay(payment_id)
            except Exception:
                # PostgreSQL is still the source of truth. retry_stuck_classifications
                # can rediscover pending/failed classification rows if broker enqueue fails.
                logger.exception(
                    "Could not enqueue classification for payment %s; DB safety-net will rediscover it",
                    payment_id,
                )

        return JSONResponse({"status": "ok"}, status_code=200)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Razorpay webhook processing failed for event %s: %s", event_id, exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed") from exc
