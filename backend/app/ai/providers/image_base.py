"""Abstract base class for AI image generation providers.

Every image generation provider extends ``BaseImageGenerationProvider``
and implements ``generate_image()``. The ``ImageGenerationManager``
orchestrates failover across registered providers.

This follows the same pattern as ``BaseAIProvider`` for image analysis,
but is specialised for text-to-image generation.
"""

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ImageGenerationResult:
    """Standardised result returned by every image generation provider."""

    success: bool
    image_url: Optional[str] = None
    image_data: Optional[bytes] = None
    mime_type: str = "image/png"
    provider_name: str = ""
    model_used: str = ""
    processing_time: float = 0.0
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Providers fill ``image_data`` (the raw bytes) and leave ``image_url`` empty: a base64 data-URL copy of a
    # 3 MB image is another ~4 MB per output that nothing on the WhatsApp path needs (PERF-5). The two helpers
    # below build whichever form a caller wants, only when it is wanted. Older callers and tests that set
    # ``image_url`` still work.

    def as_bytes(self) -> Optional[bytes]:
        """The image as raw bytes (from ``image_data``, else decoded from ``image_url``)."""
        if isinstance(self.image_data, (bytes, bytearray)) and self.image_data:
            return bytes(self.image_data)
        if isinstance(self.image_url, str) and self.image_url.startswith("data:") and "," in self.image_url:
            try:
                return base64.b64decode(self.image_url.split(",", 1)[1]) or None
            except Exception:  # noqa: BLE001 -- a malformed URL simply means "no usable image"
                return None
        return None

    def as_data_url(self) -> Optional[str]:
        """The image as a ``data:`` URL (``image_url`` when set, else built from ``image_data``)."""
        if self.image_url:
            return self.image_url
        data = self.as_bytes()
        if not data:
            return None
        return f"data:{self.mime_type or 'image/png'};base64,{base64.b64encode(data).decode('ascii')}"


class BaseImageGenerationProvider(ABC):
    """Abstract base for an AI image generation provider.

    Each provider wraps a specific AI image generation service (Gemini
    Imagen, OpenAI DALL-E, Stable Diffusion, Flux, etc.) and normalises
    its output into the standard ``ImageGenerationResult`` format.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. ``gemini``, ``openai``)."""
        ...

    @property
    @abstractmethod
    def provider_version(self) -> str:
        """Provider implementation version."""
        ...

    @abstractmethod
    async def generate_image(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        reference_image: Optional[bytes] = None,
        reference_mime_type: str = "image/jpeg",
    ) -> ImageGenerationResult:
        """Generate an image from a text prompt, optionally using a reference image.

        Args:
            prompt: The text prompt to generate an image from.
            context: Optional contextual data (request_id, aspect_ratio, etc.).
            reference_image: Optional bytes of the original product image to use
                as a reference for preserving product identity. When provided,
                the provider should use image-editing or image-conditioned
                generation instead of pure text-to-image generation.
            reference_mime_type: MIME type of the reference image (default: image/jpeg).

        Returns:
            An ``ImageGenerationResult`` with the generated image or error details.
        """
        ...

    @property
    def is_available(self) -> bool:
        """Whether this provider is configured and ready."""
        return True

    @property
    def capabilities(self) -> List[str]:
        """List of capabilities this provider supports."""
        return ["image_generation"]

    def supports_reference_image(self) -> bool:
        """Whether this provider supports passing a reference image.

        Override in subclasses that support image-conditioned generation
        (e.g., Gemini multimodal, OpenAI gpt-image-1 edits).
        """
        return False
