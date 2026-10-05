import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from celery import shared_task

from backend.app.actions_repo import ActionsRepository
from backend.app.alerts import run_evaluate_alerts
from backend.app.classification_service import classify_and_schedule
from backend.app.classifier import ClassificationError
from backend.app.config import settings
from backend.app.db import get_db_cursor
from backend.app.executor import run_action
from backend.app.health import run_snapshot_health

logger = logging.getLogger(__name__)
actions_repo = ActionsRepository()


@shared_task
def ping():
    return "pong"


@shared_task(
    autoretry_for=(ClassificationError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=max(0, settings.MAX_CLASSIFICATION_RETRIES - 1),
)
def classify_payment(payment_id: str):
    logger.info("Classifying payment %s", payment_id)
    status, category = classify_and_schedule(payment_id)
    return f"Payment {payment_id} {status} as {category}"


@shared_task
def retry_stuck_classifications():
    """Rediscover pending/failed classifications from PostgreSQL."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.STUCK_CLASSIFICATION_MINUTES
    )
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT payment_id
            FROM payments
            WHERE status = 'failed'
              AND classification_status IN ('pending', 'failed')
              AND classification_attempts < %s
              AND updated_at < %s
            ORDER BY updated_at ASC
            LIMIT 100
            """,
            (settings.MAX_CLASSIFICATION_RETRIES, cutoff),
        )
        stuck = [row[0] for row in cur.fetchall()]

    for payment_id in stuck:
        logger.info("Re-queuing stuck classification for %s", payment_id)
        classify_payment.delay(payment_id)

    return f"Re-queued {len(stuck)} payments"


@shared_task
def pick_due_actions():
    """Claim due actions from PostgreSQL and enqueue one task per claim."""
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    claimed = actions_repo.claim_due_actions(worker_id, limit=10)
    queued = 0
    for action in claimed:
        try:
            execute_action.delay(action["action_id"], worker_id)
            queued += 1
        except Exception:
            # Leave the claimed row untouched. Its lease expiry makes it recoverable
            # by a future scheduler run.
            logger.exception("Failed to enqueue action %s", action["action_id"])

    logger.info(
        "Worker %s claimed=%s queued=%s",
        worker_id,
        len(claimed),
        queued,
    )
    return f"Queued {queued} actions"


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def execute_action(self, action_id: str, worker_id: str):
    logger.info("Executing action %s as worker %s", action_id, worker_id)
    return run_action(action_id, worker_id)


@shared_task
def snapshot_health():
    run_snapshot_health()
    return "health snapshot generated"


@shared_task
def evaluate_alerts():
    run_evaluate_alerts()
    return "alerts evaluated"
