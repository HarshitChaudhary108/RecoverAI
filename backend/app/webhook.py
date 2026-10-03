import hmac
import hashlib
import json
import logging
from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import JSONResponse
from backend.app.config import settings
from backend.app.db import get_db_cursor
from backend.app import events

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    # 1. Read raw body bytes
    body = await request.body()

    # 2. Signature Verification
    signature = request.headers.get("X-Razorpay-Signature")
    if not signature:
        logger.warning("Webhook received without X-Razorpay-Signature header")
        raise HTTPException(status_code=401, detail="Missing signature")

    expected_signature = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        logger.warning("Invalid Razorpay webhook signature")
        # We log failure but return 401 as per spec
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 3. Event ID extraction
    event_id = request.headers.get("x-razorpay-event-id")
    if not event_id:
        logger.warning("Webhook received without x-razorpay-event-id header")
        raise HTTPException(status_code=400, detail="Missing event ID")

    # 4. Deduplication and Execution
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.error("Malformed JSON payload received")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("event")

    # Deduplication check using the DB
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO webhook_events (event_id, event_type, payload) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
            (event_id, event_type, json.dumps(payload))
        )
        if cur.rowcount == 0:
            # Duplicate event
            logger.info(
                "Razorpay Webhook: Duplicate event",
                extra={
                    "event_id": event_id,
                    "event_type": event_type,
                    "signature_valid": True,
                    "duplicate": True,
                    "processing_result": "skipped"
                }
            )
            return JSONResponse(content={"status": "ok"}, status_code=200)

    # Process the event
    try:
        payment_id, result = events.handle_razorpay_event(event_id, payload)
        if payment_id and result:
            from backend.worker.tasks import classify_payment
            classify_payment.delay(payment_id)
        processing_result = "success" if result else "ignored"
    except Exception as e:
        logger.exception(f"Error processing razorpay webhook event {event_id}")
        # Return 200 anyway to avoid Razorpay retries for permanent failures,
        # or 500 if we want retries. Spec says return 200 for new events if processed.
        # But for exceptions during handle_razorpay_event, let's follow the rule:
        # "Return 200" is the general target for handled events.
        processing_result = f"error: {str(e)}"
        # If it's a validation error we might want 400, but handle_razorpay_event
        # should be robust.
        return JSONResponse(content={"status": "error", "message": "Internal processing error"}, status_code=200)

    # 5. Structured Logging
    logger.info(
        "Razorpay Webhook Processed",
        extra={
            "event_id": event_id,
            "event_type": event_type,
            "payment_id": payment_id,
            "signature_valid": True,
            "duplicate": False,
            "processing_result": processing_result
        }
    )

    return JSONResponse(content={"status": "ok"}, status_code=200)
