from urllib.parse import parse_qs, urlparse

from celery import Celery

from backend.app.config import settings


def _validate_redis_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "rediss":
        return

    query = parse_qs(parsed.query)
    ssl_cert_reqs = query.get("ssl_cert_reqs", [None])[0]
    if ssl_cert_reqs is None:
        raise ValueError(
            "REDIS_URL uses rediss:// but does not declare ssl_cert_reqs. "
            "Configure Redis TLS explicitly, preferably ssl_cert_reqs=required."
        )


_validate_redis_url(settings.REDIS_URL)

app = Celery(
    "worker",
    broker=settings.REDIS_URL,
    include=["backend.worker.tasks"],
)

app.conf.update(
    # PostgreSQL is the business-state source of truth. Celery result persistence
    # is not used by the application and would only add Redis traffic/storage.
    task_ignore_result=True,
    broker_pool_limit=5,
    broker_connection_retry_on_startup=True,

    # Recovery actions are bounded but involve external I/O. Prefetching several
    # messages per worker process can hide due work behind a slow task.
    worker_prefetch_multiplier=1,

    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=300,
    task_soft_time_limit=240,
)

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
        "schedule": 600,
    },
}


if __name__ == "__main__":
    app.start()
