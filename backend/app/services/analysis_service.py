"""Analysis service orchestrating the AI analysis pipeline.

In production, analysis runs as a Celery task in a separate worker process.
In development (eager mode), it runs synchronously within the request cycle
without needing Redis.

Every analysis is permanently linked to its ``request_id`` — the same UUID
that was generated during upload — so results can never be confused between
concurrent uploads.

Extended with retry support, version tracking, processing lifecycle logs,
and audit events (Parts 9-14).
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, selectinload

from app.models.analysis import Analysis
from app.models.history import HistoryEntry
from app.models.image import Image
from app.repositories.base import BaseRepository
from app.schemas.analysis import (
    AnalysisProcessingResponse,
    AnalysisResponse,
)
from app.schemas.history import HistoryItemResponse
from app.services.processing_service import ProcessingService
from app.tasks.analysis_tasks import run_analysis_task
from app.utils.logger import logger


class AnalysisService:
    """Jewellery image analysis orchestration service.

    Dispatches analysis work to Celery.  When `CELERY_TASK_ALWAYS_EAGER` is
    True (the default for development), the task runs synchronously so no
    broker (Redis) is needed.
    """

    def __init__(self, db: Session):
        self.db = db
        self.analysis_repo = BaseRepository(Analysis, db)
        self.image_repo = BaseRepository(Image, db)
        self.history_repo = BaseRepository(HistoryEntry, db)

    async def start_analysis(
        self, image_id: str, user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AnalysisProcessingResponse:
        """
        Start an analysis for a given image.

        Creates the database record immediately (including the ``request_id``
        from the owning Image) and dispatches a Celery task.  The task call
        is synchronous in eager mode (no broker required) and truly
        asynchronous when a real broker is configured.

        Returns immediately with a processing status and the request_id.
        """
        proc = ProcessingService(self.db)

        # Verify image exists
        image = self.image_repo.get(image_id)
        if not image:
            raise ValueError(f"Image not found: {image_id}")

        # Update image processing status
        image.processing_status = "queued"
        self.db.flush()

        # Log: queued
        proc.log_processing_step(image.request_id, "queue", "started")

        # Create analysis record — carry the request_id from the image
        analysis = self.analysis_repo.create(
            request_id=image.request_id,
            image_id=image_id,
            user_id=user_id,
            status="processing",
            version_number=1,
        )

        # Log: queue completed
        proc.log_processing_step(image.request_id, "queue", "completed")

        image.processing_status = "processing"
        self.db.flush()

        logger.bind(category="analysis").info(
            f"Analysis started: {analysis.id} "
            f"request_id={image.request_id} "
            f"image={image_id} version={analysis.version_number}"
        )

        # Dispatch the Celery task
        run_analysis_task.delay(analysis.id, image.file_path)

        # Audit event
        proc.log_audit(
            action="analyze",
            status="processing",
            user_id=user_id,
            session_id=session_id,
            request_id=image.request_id,
            resource_type="analysis",
            resource_id=analysis.id,
        )

        return AnalysisProcessingResponse(
            status="processing",
            request_id=image.request_id,
            analysisId=analysis.id,
        )

    async def retry_analysis(
        self, analysis_id: str, user_id: Optional[str] = None,
    ) -> AnalysisProcessingResponse:
        """Retry a failed analysis using the SAME request_id (Part 10)."""
        analysis = self.analysis_repo.get(analysis_id)
        if not analysis:
            raise ValueError(f"Analysis not found: {analysis_id}")

        proc = ProcessingService(self.db)

        # Increment retry count
        analysis.retry_count += 1
        analysis.status = "processing"
        self.db.flush()

        # Record the retry attempt
        proc.record_retry(
            request_id=analysis.request_id,
            analysis_id=analysis.id,
            attempt_number=analysis.retry_count,
            status="processing",
        )

        # Update image status
        image = self.image_repo.get(analysis.image_id)
        if image:
            image.processing_status = "processing"
            self.db.flush()

        # Re-dispatch the task
        image_path = image.file_path if image else ""
        run_analysis_task.delay(analysis.id, image_path)

        logger.bind(category="analysis").info(
            f"Analysis retry: {analysis.id} attempt={analysis.retry_count} "
            f"request_id={analysis.request_id}"
        )

        proc.log_audit(
            action="retry",
            status="processing",
            user_id=user_id,
            request_id=analysis.request_id,
            resource_type="analysis",
            resource_id=analysis.id,
            details=f"Retry attempt {analysis.retry_count}",
        )

        return AnalysisProcessingResponse(
            status="processing",
            request_id=analysis.request_id,
            analysisId=analysis.id,
        )

    async def regenerate_analysis(
        self, analysis_id: str, user_id: Optional[str] = None,
    ) -> AnalysisProcessingResponse:
        """Regenerate analysis — creates a new version while preserving the
        previous result (Part 11 — Version History)."""
        analysis = self.analysis_repo.get(analysis_id)
        if not analysis:
            raise ValueError(f"Analysis not found: {analysis_id}")

        proc = ProcessingService(self.db)

        # Save current result as a version before regenerating
        prev_version = analysis.version_number
        proc.record_version(
            request_id=analysis.request_id,
            analysis_id=analysis.id,
            version_number=prev_version,
            result_json=self._analysis_to_json(analysis),
            summary=analysis.summary or analysis.category or "",
        )

        # Create new version with incremented version number
        new_version = prev_version + 1
        analysis.version_number = new_version
        analysis.status = "processing"
        analysis.processing_time = None
        analysis.material = None
        analysis.gold_purity = None
        analysis.weight = None
        analysis.category = None
        analysis.estimated_price = None
        analysis.confidence = None
        analysis.gemstones = None
        analysis.style = None
        analysis.era = None
        analysis.condition = None
        analysis.summary = None
        analysis.analyzed_at = None
        self.db.flush()

        # Update image status
        image = self.image_repo.get(analysis.image_id)
        if image:
            image.processing_status = "processing"
            self.db.flush()

        # Re-dispatch
        image_path = image.file_path if image else ""
        run_analysis_task.delay(analysis.id, image_path)

        logger.bind(category="analysis").info(
            f"Analysis regenerate: {analysis.id} "
            f"version {prev_version} -> {new_version} "
            f"request_id={analysis.request_id}"
        )

        proc.log_audit(
            action="regenerate",
            status="processing",
            user_id=user_id,
            request_id=analysis.request_id,
            resource_type="analysis",
            resource_id=analysis.id,
            details=f"Regenerated from v{prev_version} to v{new_version}",
        )

        return AnalysisProcessingResponse(
            status="processing",
            request_id=analysis.request_id,
            analysisId=analysis.id,
        )

    def get_analysis(self, analysis_id: str) -> Optional[AnalysisResponse]:
        """Get analysis result by ID."""
        analysis = self.analysis_repo.get(analysis_id)
        if not analysis:
            return None
        return self._to_response(analysis)

    def get_analysis_status(self, analysis_id: str) -> Optional[Dict[str, Any]]:
        """Get analysis status with retry and version info."""
        analysis = self.analysis_repo.get(analysis_id)
        if not analysis:
            return None
        return {
            "id": analysis.id,
            "request_id": analysis.request_id,
            "status": analysis.status,
            "retry_count": analysis.retry_count,
            "version_number": analysis.version_number,
        }

    def get_analysis_history(
        self, user_id: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[AnalysisResponse]:
        """Completed analyses, newest first, one page at a time (DATA-4).

        The images are loaded in ONE extra query (selectinload) instead of one query per row, and the list is
        capped (default 100, at most 500) so it cannot grow without limit as the table does."""
        query = self.db.query(Analysis).options(selectinload(Analysis.image)).filter(Analysis.status == "completed")
        if user_id:
            query = query.filter(Analysis.user_id == user_id)
        analyses = (
            query.order_by(Analysis.analyzed_at.desc())
            .limit(max(1, min(int(limit), 500)))
            .offset(max(int(offset), 0))
            .all()
        )
        return [self._to_response(a) for a in analyses]

    def get_timeline(self, analysis_id: str) -> List[Dict[str, Any]]:
        """Get the full event timeline for an analysis (Part 12)."""
        analysis = self.analysis_repo.get(analysis_id)
        if not analysis:
            return []
        proc = ProcessingService(self.db)
        return proc.get_timeline(analysis.request_id)

    def _to_response(self, analysis: Analysis) -> AnalysisResponse:
        """Convert analysis model to response schema."""
        gemstones = []
        if analysis.gemstones:
            try:
                gemstones = json.loads(analysis.gemstones)
            except (json.JSONDecodeError, TypeError):
                gemstones = []

        # Build the image reference URL from the associated image
        image_reference = ""
        if analysis.image:
            image_reference = analysis.image.image_url or ""

        return AnalysisResponse(
            id=analysis.id,
            request_id=analysis.request_id,
            productId=analysis.image_id,
            material=analysis.material or "",
            goldPurity=analysis.gold_purity or "",
            weight=analysis.weight or 0.0,
            category=analysis.category or "",
            estimatedPrice=analysis.estimated_price or 0.0,
            confidence=analysis.confidence or 0.0,
            gemstones=gemstones,
            style=analysis.style or "",
            era=analysis.era or "",
            condition=analysis.condition or "",
            summary=analysis.summary or "Analysis complete.",
            analyzedAt=analysis.analyzed_at or datetime.now(timezone.utc),
            processing_time=analysis.processing_time,
            image_reference=image_reference,
            status=analysis.status,
        )

    def _analysis_to_json(self, analysis: Analysis) -> str:
        """Serialise current analysis result to JSON for version storage."""
        data = {
            "material": analysis.material,
            "gold_purity": analysis.gold_purity,
            "weight": analysis.weight,
            "category": analysis.category,
            "estimated_price": analysis.estimated_price,
            "confidence": analysis.confidence,
            "gemstones": analysis.gemstones,
            "style": analysis.style,
            "era": analysis.era,
            "condition": analysis.condition,
            "summary": analysis.summary,
            "analyzed_at": str(analysis.analyzed_at) if analysis.analyzed_at else None,
        }
        return json.dumps(data, default=str)
