import pytest
from backend.worker.celery_app import app
from backend.app.config import settings

def test_celery_broker_url():
    """Verify that the Celery broker URL equals the configured REDIS_URL."""
    assert app.conf.broker_url == settings.REDIS_URL

def test_placeholder_tasks_registered():
    """Verify that the placeholder tasks are registered in the Celery app."""
    # Get list of registered tasks
    registered_tasks = app.tasks

    expected_tasks = [
        "backend.worker.tasks.pick_due_actions",
        "backend.worker.tasks.snapshot_health",
        "backend.worker.tasks.evaluate_alerts",
        "backend.worker.tasks.ping"
    ]

    for task in expected_tasks:
        assert task in registered_tasks, f"Task {task} is not registered"
