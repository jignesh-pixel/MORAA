"""Analysis Pydantic schemas."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.utils.upload_limits import MAX_BASE64_CHARS


class AnalysisRequest(BaseModel):
    """Analysis initiation request."""

    image_id: str = Field(..., description="ID of uploaded image to analyze")


class AnalysisCreate(BaseModel):
    """Internal analysis creation data."""

    image_id: str
    user_id: Optional[str] = None


class AnalysisResponse(BaseModel):
    """Analysis result response matching frontend AnalysisResult type.

    All fields use camelCase names directly (no aliases) so that Pydantic
    serialises them as-is for the consuming frontend.

    Extended with ``requestId``, ``processingTime``, and ``imageReference``
    for full request-level traceability — the frontend can always verify
    that a result belongs to the image it uploaded.
    """

    id: str = Field(..., description="Analysis ID")
    requestId: str = Field(
        ..., alias="request_id",
        description="Globally unique request ID linking to the originating upload",
    )
    productId: str = Field(..., description="Associated product/image ID")
    material: str = Field(default="", description="Detected material")
    goldPurity: str = Field(default="", description="Gold purity")
    weight: float = Field(default=0.0, description="Estimated weight")
    category: str = Field(default="", description="Product category")
    estimatedPrice: float = Field(default=0.0, description="Estimated price")
    confidence: float = Field(default=0.0, description="AI confidence score")
    gemstones: List[str] = Field(default_factory=list, description="Detected gemstones")
    style: str = Field(default="", description="Jewellery style")
    era: str = Field(default="", description="Design era")
    condition: str = Field(default="", description="Item condition")
    summary: str = Field(default="", description="Analysis summary")
    analyzedAt: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Analysis timestamp",
    )
    processingTime: Optional[float] = Field(
        None, alias="processing_time",
        description="Total processing time in seconds",
    )
    imageReference: str = Field(
        default="", alias="image_reference",
        description="URL or path referencing the analysed image",
    )
    status: str = Field(default="completed", description="Analysis status")

    class Config:
        populate_by_name = True


class SyncAnalysisRequest(BaseModel):
    """Request schema for the synchronous analysis endpoint.

    Used by the Next.js route to forward analysis requests to the backend.
    The ``custom_prompt`` carries the frontend's rich prompt so all providers
    return the same JSON structure — no prompt duplication.
    """

    image_base64: str = Field(
        ..., max_length=MAX_BASE64_CHARS, description="Base64-encoded image data (without data URI prefix)"
    )
    mime_type: str = Field(
        ..., description="MIME type of the image (e.g. image/jpeg, image/png)"
    )
    custom_prompt: str = Field(
        default="", description="Custom analysis prompt; uses default if empty"
    )


class SyncAnalysisResponse(BaseModel):
    """Response schema for the synchronous analysis endpoint.

    Returns the raw JSON from the AI provider plus metadata.
    The frontend Next.js route parses the raw JSON into typed AnalysisResult.
    """

    success: bool = Field(..., description="Whether analysis succeeded")
    data: Optional[Dict[str, Any]] = Field(
        None, description="Raw structured analysis result from the AI provider"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Processing metadata (provider, timing, model)"
    )
    error: Optional[str] = Field(
        None, description="Error message if analysis failed"
    )


class AnalysisProcessingResponse(BaseModel):
    """Response returned while analysis is processing.

    NOTE: Field is named ``analysisId`` (not ``analysis_id``) so that
    Pydantic serialises it in camelCase for the frontend without needing
    an alias.  ``populate_by_name`` is not required here because we pass
    the field keyword directly.
    """

    status: str = Field("processing", description="Processing status")
    requestId: str = Field(
        ..., alias="request_id",
        description="Globally unique request ID linking to the originating upload",
    )
    analysisId: str = Field(..., description="Analysis ID")

    class Config:
        populate_by_name = True
