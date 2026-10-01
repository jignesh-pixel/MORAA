"""
Celery task for running the AI analysis pipeline asynchronously.

This task runs in a separate worker process (or synchronously in eager mode).
It creates its own database session because the worker is independent of
the FastAPI request cycle.

Extended with full processing lifecycle logging, tool execution tracking,
retry recording, version history, and audit events (Parts 5-14).
"""

import asyncio
import concurrent.futures
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from celery import Task
from sqlalchemy.orm import Session

from app.ai.base import AnalysisPipeline
from app.ai.engine_factory import create_engine
from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.analysis import Analysis
from app.models.history import HistoryEntry
from app.models.image import Image
from app.models.retry_history import RetryHistory
from app.services.processing_service import ProcessingService
from app.utils.logger import logger

# Global pipeline instance (lazily initialised per worker process)
_pipeline: Optional[AnalysisPipeline] = None


def get_analysis_pipeline() -> AnalysisPipeline:
    """Get or create the analysis pipeline (singleton per worker).

    Engine type is selected via ``settings.AI_ENGINE_TYPE`` (``mock`` or
    ``vision``).  Add new engines in ``app/ai/engine_factory.py``.
    """
    global _pipeline
    if _pipeline is None:
        engine = create_engine()
        _pipeline = AnalysisPipeline(engine)
        logger.bind(category="system").info(f"Analysis pipeline initialised: {engine.engine_name} v{engine.engine_version}")
    return _pipeline


# Failures worth retrying: the call may succeed a moment later. Anything else
# (missing record, unreadable image, bad prompt, parse error) fails the same
# way every time, and each retry would re-run a paid AI call for nothing.
TRANSIENT_ERRORS = (ConnectionError, TimeoutError, httpx.TransportError)


class AnalysisTask(Task):
    """Base task class for analysis tasks with automatic error handling."""

    autoretry_for = TRANSIENT_ERRORS
    max_retries = 2
    default_retry_delay = 10  # seconds between retries
    acks_late = True  # Re-deliver if worker crashes
    reject_on_worker_lost = True
    track_started = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log task failures."""
        analysis_id = args[0] if args else "unknown"
        logger.bind(category="analysis").error(
            f"Celery task failed for analysis {analysis_id}: {exc}"
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)


@celery_app.task(
    bind=True,
    base=AnalysisTask,
    name="run_analysis_task",
    queue="analysis",
)
def run_analysis_task(
    self: AnalysisTask,
    analysis_id: str,
    image_path: str,
) -> Dict[str, Any]:
    """
    Run the AI analysis pipeline for a given image.

    This task:
    1. Measures total processing time
    2. Runs the AI analysis pipeline (mock or real) using only the explicit
       ``image_path`` — never a global or shared reference
    3. Updates the analysis record in the database
    4. Creates a history entry
    5. Returns the analysis result

    Args:
        analysis_id: The UUID of the Analysis record.
        image_path: Absolute path to the uploaded image file.

    Returns:
        Dict with analysis results.
    """
    db: Session = SessionLocal()
    processing_start = datetime.now(timezone.utc)
    try:
        logger.bind(category="analysis").info(
            f"Task started for analysis: {analysis_id} (image: {image_path})"
        )

        # 1. Load the analysis record to get the request_id for traceability
        analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
        if not analysis:
            raise ValueError(f"Analysis record {analysis_id} not found in database")

        request_id = analysis.request_id
        proc = ProcessingService(db)

        # 2. Log: ai_analysis started
        proc.log_processing_step(request_id, "ai_analysis", "started")

        logger.bind(category="analysis").info(
            f"Processing analysis {analysis_id} request_id={request_id}"
        )

        # 3. Run the analysis pipeline
        pipeline = get_analysis_pipeline()
        try:
            asyncio.get_running_loop()
            # Eager mode: running inside an event loop
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    pipeline.run(image_path, context={"request_id": request_id})
                ).result()
        except RuntimeError:
            # Worker process: no running event loop
            result = asyncio.run(pipeline.run(
                image_path, context={"request_id": request_id}
            ))

        # 4. Log: ai_analysis completed
        processing_end = datetime.now(timezone.utc)
        processing_time = (processing_end - processing_start).total_seconds()
        proc.log_processing_step(
            request_id, "ai_analysis", "completed",
            metadata={"total_time": round(processing_time, 3)},
        )

        # 5. Log tool execution steps from result
        tool_logs = result.get("_tool_executions", [])
        for tool in tool_logs:
            proc.log_tool_execution(
                request_id=request_id,
                analysis_id=analysis_id,
                tool_name=tool.get("name", "unknown"),
                execution_order=tool.get("order", 0),
                status=tool.get("status", "completed"),
                output_summary=tool.get("output", ""),
                execution_time=tool.get("duration", 0.0),
            )

        # 6. Serialise gemstones list to JSON string
        gemstones_json = result.get("gemstones", [])
        if isinstance(gemstones_json, list):
            gemstones_json = json.dumps(gemstones_json)

        # 7. Update the analysis record in the database
        analysis.status = "completed"
        analysis.processing_time = processing_time
        analysis.material = result.get("material")
        analysis.gold_purity = result.get("gold_purity")
        analysis.weight = result.get("weight")
        analysis.category = result.get("category")
        analysis.estimated_price = result.get("estimated_price")
        analysis.confidence = result.get("confidence")
        analysis.gemstones = gemstones_json
        analysis.style = result.get("style")
        analysis.era = result.get("era")
        analysis.condition = result.get("condition")
        analysis.summary = result.get("summary")
        analysis.analyzed_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(analysis)

        # 8. Record version history
        proc.record_version(
            request_id=request_id,
            analysis_id=analysis_id,
            version_number=analysis.version_number,
            result_json=json.dumps(result, default=str),
            summary=result.get("summary", "")[:500],
        )

        # 9. Update image processing status
        image = db.query(Image).filter(Image.id == analysis.image_id).first()
        if image:
            image.processing_status = "completed"
            db.commit()

        # 10. Create a history entry
        history_entry = HistoryEntry(
            user_id=analysis.user_id,
            analysis_id=analysis.id,
            image_id=analysis.image_id,
            product_name=f"{analysis.category or 'Unknown'} Jewellery",
            image_url=image.image_url if image else None,
            status="completed",
            estimated_price=result.get("estimated_price"),
        )
        db.add(history_entry)
        db.commit()

        # 11. Audit log
        proc.log_audit(
            action="analysis_completed",
            status="success",
            request_id=request_id,
            resource_type="analysis",
            resource_id=analysis_id,
            details=f"Analysis completed in {processing_time:.2f}s, version={analysis.version_number}",
        )

        # 12. If this was a retry, mark the retry as completed
        if analysis.retry_count > 0:
            last_retry = (
                db.query(RetryHistory)
                .filter(
                    RetryHistory.request_id == request_id,
                    RetryHistory.analysis_id == analysis_id,
                    RetryHistory.attempt_number == analysis.retry_count,
                )
                .order_by(RetryHistory.retry_time.desc())
                .first()
            )
            if last_retry:
                last_retry.status = "completed"
                last_retry.result_summary = result.get("summary", "")[:500]
                last_retry.processing_time = processing_time
                db.commit()

        logger.bind(category="analysis").info(
            f"Task completed for analysis: {analysis_id} "
            f"request_id={request_id} "
            f"processing_time={processing_time:.2f}s "
            f"version={analysis.version_number}"
        )

        return {
            "status": "completed",
            "analysis_id": analysis_id,
            "request_id": request_id,
            "processing_time": processing_time,
            **result,
        }

    except Exception as exc:
        # Mark the analysis as failed
        try:
            analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
            if analysis:
                analysis.status = "failed"
                db.commit()
                # Also update image processing status
                image = db.query(Image).filter(Image.id == analysis.image_id).first()
                if image:
                    image.processing_status = "failed"
                    db.commit()

            # Log failure in processing logs
            try:
                analysis_check = db.query(Analysis).filter(Analysis.id == analysis_id).first()
                if analysis_check:
                    proc_check = ProcessingService(db)
                    proc_check.log_processing_step(
                        analysis_check.request_id, "ai_analysis", "failed",
                        error_message=str(exc),
                    )
                    # Update retry status if applicable
                    if analysis_check.retry_count > 0:
                        last_retry = (
                            db.query(RetryHistory)
                            .filter(
                                RetryHistory.request_id == analysis_check.request_id,
                                RetryHistory.analysis_id == analysis_id,
                                RetryHistory.attempt_number == analysis_check.retry_count,
                            )
                            .order_by(RetryHistory.retry_time.desc())
                            .first()
                        )
                        if last_retry:
                            last_retry.status = "failed"
                            last_retry.error_message = str(exc)[:500]
                            db.commit()
            except Exception:
                pass
        except Exception:
            pass

        logger.bind(category="analysis").error(
            f"Task failed for analysis {analysis_id}: {exc}"
        )
        raise exc

    finally:
        db.close()
