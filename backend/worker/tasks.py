import logging
import os
import socket
from datetime import datetime, timedelta
from celery import shared_task
from backend.app.classification_service import classify_and_schedule
from backend.app.classifier import ClassificationError
from backend.app.db import get_db_cursor
from backend.app.config import settings
from backend.app.actions_repo import ActionsRepository

logger = logging.getLogger(__name__)
actions_repo = ActionsRepository()

@shared_task
def ping():
    logger.info("pong")
    return "pong"

@shared_task(
    autoretry_for=(ClassificationError,),
    retry_backoff=True,
    max_retries=settings.MAX_CLASSIFICATION_RETRIES
)
def classify_payment(payment_id):
    logger.info(f"Classifying payment {payment_id}...")
    status, category = classify_and_schedule(payment_id)
    return f"Payment {payment_id} {status} as {category}"

@shared_task
def retry_stuck_classifications():
    """Safety net to re-queue payments stuck in pending or failed state."""
    logger.info("Checking for stuck classifications...")
    with get_db_cursor() as cur:
        # Find payments that are pending or failed (and under max attempts)
        # and haven't been updated in 10 minutes.
        cur.execute(
            "SELECT payment_id FROM payments "
            "WHERE (classification_status = 'pending' OR classification_status = 'failed') "
            "AND classification_attempts < %s "
            "AND updated_at < %s",
            (settings.MAX_CLASSIFICATION_RETRIES, datetime.utcnow() - timedelta(minutes=10))
        )
        stuck_payments = cur.fetchall()

        for (payment_id,) in stuck_payments:
            logger.info(f"Re-queuing stuck payment {payment_id}")
            classify_payment.delay(payment_id)

    return f"Re-queued {len(stuck_payments)} payments"

@shared_task
def pick_due_actions():
    """Claims due actions from DB and queues them for execution."""
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    logger.info(f"Worker {worker_id} polling for due actions...")

    try:
        claimed = actions_repo.claim_due_actions(worker_id, limit=10)
        logger.info(f"Claimed {len(claimed)} actions")

        for action in claimed:
            execute_action.delay(action["action_id"], worker_id)

    except Exception as e:
        logger.error(f"Error picking due actions: {e}")
        raise

    return f"Queued {len(claimed)} actions"

@shared_task
def execute_action(action_id, worker_id):
    """Executes a recovery action."""
    from backend.app.executor import run_action
    logger.info(f"Worker {worker_id} starting execution of action {action_id}...")

    try:
        result = run_action(action_id, worker_id)
        logger.info(f"Action {action_id} finished with result: {result}")
        return result
    except Exception as e:
        logger.exception(f"Unexpected failure in Celery task for action {action_id}: {e}")
        raise

@shared_task
def snapshot_health():
    from backend.app.health import run_snapshot_health
    run_snapshot_health()

@shared_task
def evaluate_alerts():
    from backend.app.alerts import run_evaluate_alerts
    run_evaluate_alerts()
