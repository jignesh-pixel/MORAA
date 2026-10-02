"""Analysis API routes."""

import base64
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.ai.provider_manager import AIProviderManager
from app.api.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.analysis import (
    AnalysisProcessingResponse,
    AnalysisRequest,
    AnalysisResponse,
    SyncAnalysisRequest,
    SyncAnalysisResponse,
)
from app.services.analysis_service import AnalysisService
from app.utils.logger import logger

router = APIRouter(prefix="/api", tags=["Analysis"])


# ── Synchronous analysis endpoint ──────────────────────────────────────

@router.post(
    "/analyze/sync",
    response_model=SyncAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyse image synchronously with automatic provider failover",
    description=(
        "Analyse an image using the AI Provider Manager's configured chain "
        "(e.g. Gemini → OpenAI → local_vision).  The response is returned "
        "synchronously — no polling required.  ``custom_prompt`` can carry "
        "the frontend's rich prompt so all providers return the same format.\n\n"
        "This is the single entry point for all production analysis requests. "
        "Provider failover, retries, and fallback happen entirely inside the "
        "AI provider layer — the caller is unaware of which provider executed."
    ),
)
async def analyze_image_sync(
    request: SyncAnalysisRequest,
):
    """Analyse an image synchronously with automatic provider failover.

    This is the production analysis endpoint called by the frontend's
    Next.js API route.  It:
    1. Decodes the base64 image and saves it to a temporary file
    2. Calls AIProviderManager with the optional custom_prompt
    3. Returns the raw structured analysis result (same JSON structure
       regardless of which provider generated it)
    4. Cleans up the temporary file

    The AIProviderManager handles:
    - Provider selection via config (PRIMARY → BACKUP → FALLBACK)
    - Retries per provider (2 retries with 2s delay)
    - Fallthrough to next provider on exhaustion
    - Logging at every step
    """
    provider_manager = AIProviderManager()
    temp_path: Optional[Path] = None

    try:
        # Decode base64 image and save to a temporary file
        image_bytes = base64.b64decode(request.image_base64)
        suffix = ".jpg"
        if request.mime_type == "image/png":
            suffix = ".png"
        elif request.mime_type == "image/webp":
            suffix = ".webp"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(image_bytes)
            temp_path = Path(tmp.name)

        logger.info(
            f"Sync analysis: saved {len(image_bytes)} bytes to {temp_path} "
            f"mime_type={request.mime_type}"
        )

        # Call AIProviderManager with the prompt
        manager_context: Dict[str, Any] = {}
        if request.custom_prompt:
            manager_context["custom_prompt"] = request.custom_prompt

        result = await provider_manager.analyze(
            image_paths=[str(temp_path)],
            context=manager_context,
        )

        if result.success:
            logger.info(
                f"Sync analysis succeeded: provider={result.provider_name} "
                f"fallback={result.fallback_used} time={result.processing_time:.2f}s"
            )

            # Extract model info from tool_executions if available
            model_used = result.provider_name
            for tool in (result.tool_executions or []):
                output = tool.get("output", "")
                if "model=" in output:
                    model_used = output.split("model=")[-1].split(" ")[0]

            return SyncAnalysisResponse(
                success=True,
                data=result.data,
                metadata={
                    "provider": result.provider_name,
                    "processing_time": round(result.processing_time, 3),
                    "model": model_used,
                    "fallback_used": result.fallback_used,
                },
            )
        else:
            logger.error(
                f"Sync analysis failed: {result.error} "
                f"provider={result.provider_name}"
            )
            return SyncAnalysisResponse(
                success=False,
                data=None,
                error=result.error or "All AI providers failed",
                metadata={
                    "provider": result.provider_name,
                    "processing_time": round(result.processing_time, 3),
                },
            )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Sync analysis endpoint error: {error_msg}")
        return SyncAnalysisResponse(
            success=False,
            data=None,
            error=error_msg,
        )

    finally:
        # Clean up temporary file
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to clean up temp file {temp_path}: {e}")


# ── Async analysis endpoint (existing, unchanged) ───────────────────────

@router.post(
    "/analyze",
    response_model=AnalysisProcessingResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start jewellery image analysis",
    description=(
        "Submit an image ID for AI analysis. "
        "Returns immediately with a processing status and ``requestId``. "
        "The actual analysis runs asynchronously. "
        "Use GET /api/analyze/{analysis_id} to retrieve results."
    ),
)
async def analyze_image(
    request: AnalysisRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """
    Start an AI analysis on a previously uploaded image.

    Every analysis is linked to the ``request_id`` that was generated during
    upload (stored in the Image model).  The response includes this ID so
    the frontend can always correlate result → upload.
    """
    service = AnalysisService(db)
    try:
        user_id = current_user.id if current_user else None
        result = await service.start_analysis(
            image_id=request.image_id,
            user_id=user_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/analyze/{analysis_id}",
    response_model=AnalysisResponse,
    summary="Get analysis result",
    description="Retrieve the result of a completed jewellery analysis.",
)
def get_analysis(
    analysis_id: str,
    db: Session = Depends(get_db),
):
    """Get the analysis result by ID."""
    service = AnalysisService(db)
    result = service.get_analysis(analysis_id)

    if result is None:
        # Check if analysis exists at all
        status_info = service.get_analysis_status(analysis_id)
        if status_info is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Analysis not found",
            )
        if status_info["status"] == "processing":
            raise HTTPException(
                status_code=status.HTTP_202_ACCEPTED,
                detail="Analysis still processing",
            )
        if status_info["status"] == "failed":
            raise HTTPException(
                status_code=422,
                detail="Analysis failed",
            )

    return result


@router.get(
    "/analyses",
    response_model=list[AnalysisResponse],
    summary="List all analyses",
    description="Get a list of all completed analyses.",
)
def list_analyses(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get all completed analyses."""
    service = AnalysisService(db)
    user_id = current_user.id if current_user else None
    return service.get_analysis_history(user_id=user_id)


@router.post(
    "/analyze/{analysis_id}/retry",
    response_model=AnalysisProcessingResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed analysis",
    description=(
        "Retry a previously failed analysis. Uses the SAME request_id — "
        "a new request is NOT generated. (Part 10)"
    ),
)
async def retry_analysis(
    analysis_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """
    Retry a failed analysis.

    Retries use the EXISTING request_id — they do NOT create a new upload
    request. Each retry is recorded in RetryHistory for full traceability.
    """
    service = AnalysisService(db)
    try:
        user_id = current_user.id if current_user else None
        result = await service.retry_analysis(
            analysis_id=analysis_id,
            user_id=user_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/analyze/{analysis_id}/regenerate",
    response_model=AnalysisProcessingResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Regenerate analysis (create new version)",
    description=(
        "Regenerate an analysis — the previous result is preserved as a "
        "version and the analysis is re-run. (Part 11)"
    ),
)
async def regenerate_analysis(
    analysis_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """
    Regenerate an analysis, preserving the previous result as a version.

    Version 1 → Version 2 — both are queryable via the version history API.
    """
    service = AnalysisService(db)
    try:
        user_id = current_user.id if current_user else None
        result = await service.regenerate_analysis(
            analysis_id=analysis_id,
            user_id=user_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/analyze/{analysis_id}/timeline",
    response_model=dict,
    summary="Get analysis event timeline",
    description=(
        "Get the complete event timeline for an analysis, including "
        "processing steps, tool executions, and audit events. (Part 12)"
    ),
)
def get_analysis_timeline(
    analysis_id: str,
    db: Session = Depends(get_db),
):
    """Get the full event timeline for an analysis."""
    service = AnalysisService(db)
    timeline = service.get_timeline(analysis_id)
    return {
        "analysis_id": analysis_id,
        "events": timeline,
    }
