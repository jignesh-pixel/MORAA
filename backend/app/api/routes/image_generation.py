"""API routes for AI image generation.

Generates images using the ImageGenerationManager, which routes through
ChatGPT image generation (OpenAI gpt-image-1, PRIMARY — reference-aware
editing) → Gemini (automatic fallback) with failover for recoverable errors.

Consistent response contract:
    {
        "success": true,
        "provider": "openai",
        "fallback_used": false,
        "image_url": "data:image/png;base64,...",
        "generation_time": 5.8
    }
"""

import base64
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, status

from app.ai.image_generation_manager import ImageGenerationManager
from app.config import settings
from app.schemas.image_generation import (
    ImageGenerationHealthResponse,
    ImageGenerationRequest,
    ImageGenerationResponse,
)
from app.services.image_quality_floor import check_quality_floor
from app.utils.logger import logger

router = APIRouter(prefix="/api", tags=["Image Generation"])


@router.post(
    "/generate-image",
    response_model=ImageGenerationResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate an image from a text prompt",
    description=(
        "Generate an image using the configured AI provider chain. "
        "Primary: ChatGPT image generation (OpenAI gpt-image-1, reference-aware). "
        "Fallback: Gemini. "
        "Automatic failover for recoverable errors (429, quota, timeout, 5xx). "
        "Does NOT fallback for invalid prompts, bad requests, or missing config."
    ),
)
async def generate_image(
    request: ImageGenerationRequest,
):
    """Generate an image from a text prompt with automatic provider failover.

    Flow:
    1. Validates the prompt
    2. Calls ImageGenerationManager with the prompt
    3. Manager attempts the primary provider (ChatGPT image / OpenAI
       gpt-image-1, reference-aware editing), falls back to Gemini on
       recoverable errors
    4. Returns the generated image as a base64 data URL

    The ImageGenerationManager handles:
    - Provider selection (OpenAI → Gemini)
    - Retries per provider (1 retry with 2s delay)
    - Fallthrough to next provider on recoverable failures
    - Logging at every step
    """
    manager = ImageGenerationManager()

    try:
        # Build context
        context = {}
        if request.image_id:
            context["request_id"] = request.image_id
        if request.aspect_ratio:
            context["aspect_ratio"] = request.aspect_ratio

        # Parse reference image if provided
        reference_image = None
        reference_mime_type = request.reference_mime_type or "image/jpeg"
        if request.reference_image:
            try:
                # Handle data:image/...;base64,... format
                img_data = request.reference_image
                if img_data.startswith("data:"):
                    # Extract MIME type from data URL (e.g., "data:image/jpeg;base64,...")
                    header = img_data.split(",", 1)[0]  # "data:image/jpeg;base64"
                    if ";" in header:
                        mime_part = header.split(";")[0]  # "data:image/jpeg"
                        reference_mime_type = mime_part.replace("data:", "")
                    # Extract base64 part after the comma
                    img_data = img_data.split(",", 1)[1]
                
                reference_image = base64.b64decode(img_data)
                logger.info(
                    f"Reference image decoded: size={len(reference_image)} bytes "
                    f"mime={reference_mime_type}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to decode reference image: {e} "
                    f"Continuing with text-only prompt"
                )

        logger.info(
            f"Image generation requested: prompt_len={len(request.prompt)} "
            f"aspect_ratio={request.aspect_ratio} "
            f"has_reference={reference_image is not None}"
        )

        result = await manager.generate_image(
            prompt=request.prompt,
            context=context,
            force_provider=request.force_provider,
            reference_image=reference_image,
            reference_mime_type=reference_mime_type,
            marketplace=request.marketplace,
        )

        if result.success:
            logger.info(
                f"Image generation succeeded: "
                f"provider={result.provider_name} "
                f"fallback={result.fallback_used} "
                f"time={result.processing_time:.2f}s"
            )

            # Deterministic post-generation quality floor (P1·04) — opt-in,
            # default off, so every existing caller is unaffected. On
            # failure this is a single controlled failure (no auto-retry,
            # no wallet interaction — this endpoint never touches the
            # wallet), matching the manager's existing zero-auto-retry
            # philosophy (MAX_RETRIES_PER_PROVIDER = 0).
            if request.enforce_quality_floor and result.image_data:
                floor_result = check_quality_floor(result.image_data)
                if not floor_result.passed:
                    logger.warning(
                        f"Image generation quality floor failed: "
                        f"reason={floor_result.reason} "
                        f"dimensions={floor_result.width}x{floor_result.height}"
                    )
                    return ImageGenerationResponse(
                        success=False,
                        provider=result.provider_name,
                        fallback_used=result.fallback_used,
                        fallback_reason=result.fallback_reason,
                        image_url=None,
                        generation_time=round(result.processing_time, 2),
                        error=f"quality_check_failed:{floor_result.reason}",
                    )

            # Detect manual provider switch from metadata
            generation_mode = (result.metadata or {}).get("generation_mode", "primary")

            return ImageGenerationResponse(
                success=True,
                provider=result.provider_name,
                fallback_used=result.fallback_used,
                fallback_reason=result.fallback_reason,
                image_url=result.image_url,
                generation_time=round(result.processing_time, 2),
                model_used=result.model_used,
                metadata=result.metadata,
            )
        else:
            logger.error(
                f"Image generation failed: {result.error} "
                f"provider={result.provider_name}"
            )

            # Return a proper error response instead of raising HTTPException
            # so the frontend always gets a parseable JSON response
            return ImageGenerationResponse(
                success=False,
                provider=result.provider_name,
                fallback_used=result.fallback_used,
                fallback_reason=result.fallback_reason,
                image_url=None,
                generation_time=round(result.processing_time, 2),
                error=result.error or "All image generation providers failed",
            )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Image generation endpoint error: {error_msg}")
        return ImageGenerationResponse(
            success=False,
            provider="none",
            fallback_used=False,
            image_url=None,
            generation_time=0.0,
            error=error_msg,
        )


@router.get(
    "/generate-image/health",
    response_model=ImageGenerationHealthResponse,
    summary="Image generation health check",
    description="Check if the image generation endpoint is ready and which providers are available.",
)
async def image_generation_health():
    """Health check for the image generation service."""
    manager = ImageGenerationManager()
    providers = manager.get_available_providers()

    return ImageGenerationHealthResponse(
        status="ready",
        service="image-generation",
        primary_provider=settings.PRIMARY_IMAGE_PROVIDER,
        fallback_provider=settings.FALLBACK_IMAGE_PROVIDER,
        providers=providers,
        timestamp=datetime.now().isoformat(),
    )
