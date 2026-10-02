from backend.app.config import settings
from celery import Celery
from celery.schedules import crontab

# Broker is rediss:// (SSL)
broker_url = settings.REDIS_URL
result_backend = settings.REDIS_URL

app = Celery(
    "worker",
    broker=broker_url,
    backend=result_backend,
    include=["backend.worker.tasks"]
)

# Reliability settings
app.conf.update(
    # Acknowledge tasks late (after execution)
    task_acks_late=True,
    # Requeue task if worker crashes
    task_reject_on_worker_lost=True,
    # Use SSL for Redis (as per provider_notes.md)
    # When using rediss://, Celery and redis-py handle the SSL handshake.
    # We only add these if explicit CA certs or specific requirements are needed.
    # Removing the explicit broker_use_ssl dictionary that was forcing a mismatch
    # if the URL scheme was dynamically changed or misinterpreted.

    # Task execution limits from config.py
    task_time_limit=300,
    task_soft_time_limit=240,
)

# Fixed code schedule for placeholder tasks
app.conf.beat_schedule = {
    "pick-due-actions-every-60s": {
        "task": "backend.worker.tasks.pick_due_actions",
        "schedule": settings.SCHEDULER_INTERVAL_SECONDS,
    },
    "snapshot-health-every-60s": {
        "task": "backend.worker.tasks.snapshot_health",
        "schedule": settings.SCHEDULER_INTERVAL_SECONDS,
    },
    "evaluate-alerts-every-60s": {
        "task": "backend.worker.tasks.evaluate_alerts",
        "schedule": settings.SCHEDULER_INTERVAL_SECONDS,
    },
    "retry-stuck-classifications-every-10m": {
        "task": "backend.worker.tasks.retry_stuck_classifications",
        "schedule": 600, # Every 10 minutes
    },
}

if __name__ == "__main__":
    app.start()
