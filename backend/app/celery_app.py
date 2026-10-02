"""
Celery application instance for MORAA GemVision.

Usage (development — eager mode, no broker needed):
    Tasks run synchronously within the FastAPI process.

Usage (production — real broker):
    CELERY_TASK_ALWAYS_EAGER=false
    CELERY_BROKER_URL=redis://redis-host:6379/0

    Start worker:
        celery -A app.celery_app worker -l info -Q analysis

    Start beat scheduler (if periodic tasks are added later):
        celery -A app.celery_app beat -l info
"""

from celery import Celery
from celery.signals import worker_ready, worker_shutdown

from app.config import settings
from app.utils.logger import logger

celery_app = Celery(
    "moraa_gemvision",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Task routing — send analysis tasks to the dedicated queue
    task_routes={
        "app.tasks.analysis_tasks.run_analysis_task": {
            "queue": settings.CELERY_ANALYSIS_QUEUE,
        },
    },
    # Eager mode — tasks run synchronously when no broker is available
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    # Store task results
    task_track_started=True,
    task_store_errors_even_if_ignored=True,
    # Worker settings
    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,
    worker_prefetch_multiplier=1,
    # Retry policy for broker connection
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
    # Time limits (Q-4): a hung AI call can never keep a worker forever. The soft limit raises inside the task
    # first (so it can record the failure); the hard limit then kills it.
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT_SECONDS,
)


@worker_ready.connect
def on_worker_ready(**kwargs):
    """Log when the Celery worker starts."""
    logger.bind(category="system").info(f"Celery worker ready — queue: {settings.CELERY_ANALYSIS_QUEUE}, "
        f"concurrency: {settings.CELERY_WORKER_CONCURRENCY}, "
        f"broker: {settings.CELERY_BROKER_URL}")


@worker_shutdown.connect
def on_worker_shutdown(**kwargs):
    """Log when the Celery worker shuts down."""
    logger.bind(category="system").info("Celery worker shutting down")


# Import tasks so they are registered with the Celery app
import app.tasks.analysis_tasks  # noqa: E402, F401
import app.tasks.prompt_tasks  # noqa: E402, F401
