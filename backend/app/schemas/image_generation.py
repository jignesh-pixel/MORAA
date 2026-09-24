"""Pydantic schemas for AI image generation.

Defines the request and response contracts for the image generation
endpoint, following the consistent response format specified in the
integration requirements.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ImageGenerationRequest(BaseModel):
    """Request to generate an image from a text prompt."""

    prompt: str = Field(
        ..., min_length=1, max_length=20000,
        description="Text prompt for image generation (from prompt generation pipeline)",
    )
    aspect_ratio: str = Field(
        default="4:5",
        description="Aspect ratio for the generated image (e.g. '1:1', '4:5', '16:9')",
    )
    image_id: Optional[str] = Field(
        None, description="Optional image/analysis ID for tracking purposes",
    )
    reference_image: Optional[str] = Field(
        None,
        description=(
            "Optional base64-encoded reference image of the original product. "
            "When provided, the AI will use this as a visual reference to preserve "
            "product identity (shape, stones, metalwork, proportions). "
            "Supports data:image/...;base64,... format or raw base64."
        ),
    )
    reference_mime_type: str = Field(
        default="image/jpeg",
        description="MIME type of the reference image (default: image/jpeg)",
    )
    marketplace: Optional[str] = Field(
        None,
        description=(
            "Optional marketplace presentation target. "
            "When provided, marketplace-specific presentation rules are appended "
            "to the prompt. Examples: 'amazon_india_fashion_earrings'. "
            "When None, the prompt is sent without marketplace overlay."
        ),
    )
    force_provider: Optional[str] = Field(
        None,
        description=(
            "Optional provider name to force (e.g. 'openai' or 'gemini'). "
            "When set, uses ONLY this provider with no automatic fallback. "
            "Used for manual provider switching by the user. "
            "When None, uses the default provider chain with automatic failover."
        ),
    )
    enforce_quality_floor: bool = Field(
        default=False,
        description=(
            "When true, the generated image must pass a lightweight "
            "deterministic quality floor (decodable, minimum dimensions) "
            "before being returned as a success. Default false — every "
            "existing caller is unaffected unless it opts in."
        ),
    )


class ImageGenerationResponse(BaseModel):
    """Response from the image generation endpoint.

    Follows the consistent response contract:
    - success: bool
    - provider: str (which provider generated the image)
    - fallback_used: bool
    - fallback_reason: Optional[str]
    - image_url: Optional[str] (base64 data URL of the generated image)
    - generation_time: float (seconds)
    - error: Optional[str]
    """

    success: bool = Field(..., description="Whether image generation succeeded")
    provider: str = Field(default="", description="AI provider that generated the image")
    fallback_used: bool = Field(default=False, description="Whether a fallback provider was used")
    fallback_reason: Optional[str] = Field(None, description="Reason for fallback if used")
    image_url: Optional[str] = Field(None, description="Base64 data URL of the generated image")
    generation_time: float = Field(default=0.0, description="Total generation time in seconds")
    model_used: Optional[str] = Field(None, description="Model name used for generation")
    error: Optional[str] = Field(None, description="Error message if generation failed")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional generation metadata")


class ImageGenerationHealthResponse(BaseModel):
    """Health check response for image generation service."""

    status: str = Field("ready")
    service: str = "image-generation"
    primary_provider: str = Field(default="gemini")
    fallback_provider: str = Field(default="openai")
    providers: list = Field(default_factory=list)
    timestamp: str = Field(default="")
