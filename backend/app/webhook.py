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
    """Verify, durably persist, and asynchronously process Razorpay events."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    event_id = request.headers.get("x-razorpay-event-id")

    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event ID")

    expected_signature = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_type = payload.get("event")
    if not event_type:
        raise HTTPException(status_code=400, detail="Missing event type")

    try:
        # Event persistence and deterministic business-state changes are committed
        # together. If that transaction fails, the event is not acknowledged as
        # successfully processed and Razorpay may retry it.
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
                logger.info("Duplicate webhook event_id=%s", event_id)
                return JSONResponse({"status": "ok"}, status_code=200)

            payment_id, success = events.handle_razorpay_event(
                event_id,
                payload,
                cur=cur,
            )
            if not success:
                raise RuntimeError(f"Invalid/failed processing for event={event_type}")

        # Classification is intentionally outside the HTTP request and is backed by
        # PostgreSQL safety-net discovery if the broker enqueue temporarily fails.
        if payment_id and event_type == "payment.failed":
            try:
                classify_payment.delay(payment_id)
            except Exception:
                logger.exception(
                    "Classification enqueue failed for %s; PostgreSQL safety-net will retry",
                    payment_id,
                )

        return JSONResponse({"status": "ok"}, status_code=200)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Razorpay webhook processing failed for %s: %s", event_id, exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed") from exc
