"""Celery task for asynchronous prompt generation."""

from typing import Any, Dict

from celery import Task
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.prompt_generation_service import PromptGenerationService
from app.tasks.retry_policy import TRANSIENT_ERRORS
from app.utils.logger import logger


class PromptTask(Task):
    """Base task class for prompt generation tasks."""

    # Same policy as the analysis tasks: retry only failures that can clear up.
    autoretry_for = TRANSIENT_ERRORS
    max_retries = 2
    default_retry_delay = 10
    acks_late = True
    reject_on_worker_lost = True
    track_started = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log task failures."""
        image_id = args[0] if args else "unknown"
        logger.bind(category="prompts").error(
            f"Celery prompt task failed for image {image_id}: {exc}"
        )


@celery_app.task(
    bind=True,
    base=PromptTask,
    name="run_prompt_generation_task",
    queue="analysis",
)
def run_prompt_generation_task(
    self: PromptTask,
    image_id: str,
    image_path: str,
    request_id: str = "unknown",
    description: str = "",
) -> Dict[str, Any]:
    """Run the prompt generation pipeline as a Celery task.

    Args:
        image_id: UUID of the Image record (for traceability).
        image_path: Absolute path to the uploaded image file.
        request_id: Request ID for traceability.
        description: Optional consumer-provided description.

    Returns:
        Dict with prompt generation results.
    """
    logger.bind(category="prompts").info(
        f"Prompt generation task started: image={image_id} "
        f"request_id={request_id}"
    )

    import asyncio
    service = PromptGenerationService()
    result = asyncio.run(service.generate_prompts(
        image_path=image_path,
        description=description,
        request_id=request_id,
    ))

    if result.get("success"):
        logger.bind(category="prompts").info(
            f"Prompt generation task completed: image={image_id} "
            f"request_id={request_id}"
        )
    else:
        logger.bind(category="prompts").error(
            f"Prompt generation task failed: {result.get('error')} "
            f"image={image_id} request_id={request_id}"
        )

    return result
