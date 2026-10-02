"""Prompt generation API routes.

Uses the EXISTING analysis result from the database — ZERO Gemini calls.
Takes the completed Analysis record, maps its data to the n8n workflow
format, and generates all 8 prompt categories using deterministic templates.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.database import get_db
from app.models.image import Image
from app.models.analysis import Analysis
from app.models.user import User
from app.schemas.prompts import (
    PromptGenerateRequest,
    PromptGenerateResponse,
    PromptProcessingResponse,
)
from app.services.prompt_generation_service import PromptGenerationService
from app.utils.logger import logger

router = APIRouter(prefix="/api", tags=["Prompts"])


@router.post(
    "/prompts/generate",
    response_model=PromptGenerateResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate promotional prompts from an existing analysis",
    description=(
        "Generates 8 promotional prompts (Professional Shot, Use Case Shot, "
        "Ingredient Story, Festive, Transformation, Scale Reference, "
        "Complementary Shot, UGC Style) from an ALREADY-COMPLETED analysis. "
        "ZERO Gemini API calls — uses existing analysis data + local templates."
    ),
)
async def generate_prompts(
    request: PromptGenerateRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Generate 8 promotional prompts using an existing analysis result.

    Looks up the completed Analysis record associated with the image,
    maps its data to the n8n 11-field workflow format, and generates
    all prompt categories using local template functions.
    """
    # Load the image record
    image = db.query(Image).filter(Image.id == request.image_id).first()
    if not image:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image not found: {request.image_id}",
        )

    # Find the completed Analysis record for this image
    analysis = (
        db.query(Analysis)
        .filter(
            Analysis.image_id == request.image_id,
            Analysis.status == "completed",
        )
        .order_by(Analysis.version_number.desc())
        .first()
    )

    if not analysis:
        # Check if any analysis exists but is still processing
        pending = (
            db.query(Analysis)
            .filter(
                Analysis.image_id == request.image_id,
                Analysis.status == "processing",
            )
            .first()
        )
        if pending:
            raise HTTPException(
                status_code=status.HTTP_202_ACCEPTED,
                detail="Analysis is still processing. Please wait for it to complete.",
            )

        # Check if analysis record exists via the image's analyses relationship
        analysis_list = list(image.analyses) if image.analyses else []
        if not analysis_list:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No analysis found for this image. "
                    "Please run the analysis pipeline first before generating prompts."
                ),
            )

        raise HTTPException(
            status_code=422,
            detail="Analysis exists but did not complete successfully. Please re-run analysis.",
        )

    logger.bind(category="prompts").info(
        f"Prompt generation requested: image={request.image_id} "
        f"analysis={analysis.id} request_id={image.request_id}"
    )

    # Build analysis data dict from the DB record
    analysis_data = {
        "material": analysis.material or "",
        "gold_purity": analysis.gold_purity or "",
        "weight": analysis.weight or 0.0,
        "category": analysis.category or "",
        "estimated_price": analysis.estimated_price or 0.0,
        "confidence": analysis.confidence or 0.0,
        "gemstones": analysis.gemstones or "",
        "style": analysis.style or "",
        "era": analysis.era or "",
        "condition": analysis.condition or "",
        "summary": analysis.summary or "",
    }

    # Use the existing request_id from the image
    effective_request_id = image.request_id or request.image_id

    service = PromptGenerationService()
    result = await service.generate_prompts(
        analysis_data=analysis_data,
        description=request.description or "",
        request_id=effective_request_id,
    )

    if not result.get("success"):
        logger.bind(category="prompts").error(
            f"Prompt generation failed: {result.get('error')} "
            f"image={request.image_id} analysis={analysis.id}"
        )
        raise HTTPException(
            status_code=422,
            detail=result.get("error", "Prompt generation failed"),
        )

    logger.bind(category="prompts").info(
        f"Prompt generation completed: image={request.image_id} "
        f"analysis={analysis.id} "
        f"time={result.get('generation_time_ms', 0):.0f}ms "
        f"tokens_consumed=0"
    )

    return PromptGenerateResponse(
        success=True,
        workflow_analysis=result.get("workflow_analysis", {}),
        prompts=result.get("prompts", {}),
        generation_time_ms=result.get("generation_time_ms", 0),
        image_id=request.image_id,
    )


@router.get(
    "/prompts/generate/health",
    summary="Prompt generation health check",
    description="Check if the prompt generation endpoint is ready.",
)
async def prompt_health():
    """Health check for the prompt generation service."""
    from app.config import settings

    return {
        "status": "ready",
        "service": "prompt-generation",
        "model": "local-template-engine",
        "configured": True,
        "tokens_consumed": 0,
        "api_calls_required": 0,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
