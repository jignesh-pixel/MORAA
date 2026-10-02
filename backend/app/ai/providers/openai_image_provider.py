"""OpenAI Image Generation provider — uses OpenAI ChatGPT image models.

Primary path (``gpt-image-1`` and newer): /v1/images/edits with the
uploaded reference image for high-fidelity product preservation.

Legacy path (``dall-e-3``): text-to-image only — the reference image is
ignored. Kept for backward compatibility via env override.

The ``response_format`` parameter is omitted because openai v2.x sends
it in a way that some API endpoints reject. Instead this provider
requests a URL and downloads the image bytes internally.

Requires ``OPENAI_API_KEY`` environment variable in the backend .env.
"""

import asyncio
import base64
import os
import tempfile
import time
import weakref
from typing import Any, Dict, Optional, Tuple

import httpx

from app.ai.providers.image_base import BaseImageGenerationProvider, ImageGenerationResult
from app.config import settings
from app.utils.logger import logger


# ─── OpenAI-only reference-image identity anchor ────────────────────────
# Prepended to the prompt ONLY when a reference image is supplied and the
# model supports image editing (gpt-image-1+).  Gemini has its own
# anchoring strategy (REFERENCE_IMAGE_ANCHOR) — this is intentionally
# provider-specific.
#
# The anchor protects product identity without analysing the product.
# It makes NO claims about stone count, metal colour, product type,
# or any other product attribute.  The reference image remains the
# sole source of product truth.
OPENAI_IDENTITY_ANCHOR = (
    "The uploaded reference image is the authoritative source for the "
    "product's identity.\n"
    "Preserve the exact product shown in the reference image.\n"
    "Do not redesign, reinterpret, simplify, add, remove, or substitute "
    "product features.\n"
    "Preserve all visible product-specific details, including:\n"
    "- overall design and geometry\n"
    "- proportions\n"
    "- stone arrangement and placement\n"
    "- metal appearance\n"
    "- attachment structure\n"
    "- decorative details\n"
    "Only apply the requested scene or presentation changes.\n"
    "If the scene instruction conflicts with the reference product's "
    "physical identity, preserve the reference product."
)


def _request_timeout() -> httpx.Timeout:
    """Total request time limit with a short connect limit, from settings."""
    return httpx.Timeout(
        float(settings.OPENAI_IMAGE_TIMEOUT_SECONDS or 120.0),
        connect=float(settings.OPENAI_IMAGE_CONNECT_TIMEOUT_SECONDS or 10.0),
    )


# One SDK client per event loop, reused for every call (PERF-4); see gemini_image_provider._CLIENTS.
_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Tuple[str, Any]]" = weakref.WeakKeyDictionary()


def _get_client(async_openai_cls: Any) -> Any:
    loop = asyncio.get_running_loop()
    key = settings.OPENAI_API_KEY
    cached = _CLIENTS.get(loop)
    if cached is not None and cached[0] == key:
        return cached[1]
    client = async_openai_cls(api_key=key, max_retries=0, timeout=_request_timeout())
    _CLIENTS[loop] = (key, client)
    return client


async def close_openai_client() -> None:
    """Close the running loop's shared client (called at application shutdown)."""
    try:
        cached = _CLIENTS.pop(asyncio.get_running_loop(), None)
    except RuntimeError:
        return
    if cached is not None:
        try:
            await cached[1].close()
        except Exception as e:  # noqa: BLE001 -- shutdown must never fail on this
            logger.warning(f"OpenAI client close failed: {e}")


class OpenAIImageProvider(BaseImageGenerationProvider):
    """AI image generation provider using OpenAI DALL-E / gpt-image-1."""

    # Models that support image editing (reference image input)
    IMAGE_EDITING_MODELS = {"gpt-image-1", "gpt-image-1.5", "gpt-image-2", "chatgpt-image-latest"}

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def provider_version(self) -> str:
        return "2.0.0"

    @property
    def is_available(self) -> bool:
        return bool(settings.OPENAI_API_KEY)

    def supports_reference_image(self) -> bool:
        """Check if the configured model supports image editing."""
        model = (settings.OPENAI_IMAGE_MODEL or "dall-e-3").lower()
        return model in self.IMAGE_EDITING_MODELS

    async def generate_image(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        reference_image: Optional[bytes] = None,
        reference_mime_type: str = "image/jpeg",
    ) -> ImageGenerationResult:
        """Generate an image using OpenAI.

        When the model supports image editing (gpt-image-1+) and a reference
        image is provided, uses the /v1/images/edits endpoint for
        image-conditioned generation with high fidelity.

        For DALL-E 3 (text-to-image only), the reference image is ignored
        and a text-only prompt is used.

        Args:
            prompt: The text prompt for image generation.
            context: Optional dict with ``request_id``, ``aspect_ratio``, etc.
            reference_image: Optional bytes of the original product image.
            reference_mime_type: MIME type of the reference image.

        Returns:
            ImageGenerationResult with the generated image data or error.
        """
        request_id = (context or {}).get("request_id", "unknown")
        aspect_ratio = (context or {}).get("aspect_ratio", "4:5")
        model = (settings.OPENAI_IMAGE_MODEL or "dall-e-3")
        model_lower = model.lower()
        use_image_editing = (
            reference_image is not None
            and model_lower in self.IMAGE_EDITING_MODELS
        )
        start_time = time.time()

        logger.info(
            f"OpenAIImageProvider generating image "
            f"request_id={request_id} aspect_ratio={aspect_ratio} "
            f"model={model} use_image_editing={use_image_editing}"
        )

        # Lazy import — openai is only required for this provider
        try:
            from openai import AsyncOpenAI
        except ImportError:
            return ImageGenerationResult(
                success=False,
                error="openai package not installed. Run: pip install openai",
                provider_name=self.provider_name,
                processing_time=time.time() - start_time,
            )

        try:
            # Shared per-loop client (PERF-4) with an explicit timeout (EXT-3: the SDK default is 600 s).
            # max_retries=0: one HTTP attempt per call (the SDK default is 2 retries); the manager owns retries.
            client = _get_client(AsyncOpenAI)

            # Map aspect ratio to a size the ACTIVE model accepts.
            # gpt-image-1 (ChatGPT image, PRIMARY) supports only:
            #   "auto", "1024x1024", "1024x1536", "1536x1024"
            # DALL-E 3 (legacy text-only) supports only:
            #   "1024x1024", "1024x1792", "1792x1024"
            if model_lower in self.IMAGE_EDITING_MODELS:
                size_map = {
                    "1:1": "1024x1024",
                    "4:5": "1024x1536",   # portrait
                    "3:4": "1024x1536",   # portrait
                    "9:16": "1024x1536",   # portrait
                    "5:4": "1536x1024",   # landscape
                    "4:3": "1536x1024",   # landscape
                    "16:9": "1536x1024",  # landscape
                }
            else:
                size_map = {
                    "1:1": "1024x1024",
                    "4:5": "1024x1792",   # portrait
                    "3:4": "1024x1792",   # portrait
                    "9:16": "1024x1792",  # portrait
                    "5:4": "1792x1024",   # landscape
                    "4:3": "1792x1024",   # landscape
                    "16:9": "1792x1024",  # landscape
                }
            size = size_map.get(aspect_ratio, "1024x1024")

            if use_image_editing:
                # ── Image Editing Mode (gpt-image-1+) ──────────────────
                # Uses /v1/images/edits with reference image for product preservation
                logger.info(
                    f"OpenAIImageProvider: using image editing mode "
                    f"with reference image request_id={request_id}"
                )

                # Create a temporary file for the reference image.
                # WINDOWS-SAFE: NamedTemporaryFile(delete=False) must be
                # CLOSED before the SDK opens it for the multipart upload —
                # an open handle locks the file (Errno 13 Permission denied).
                # The file is removed manually afterwards.

                # Determine file extension from MIME type
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }
                ext = ext_map.get(reference_mime_type, ".jpg")

                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp_path = tmp.name
                try:
                    tmp.write(reference_image)
                    tmp.flush()
                    tmp.close()
                    with open(tmp_path, "rb") as img_file:
                        # Prepend identity anchor so OpenAI treats the
                        # reference image as the authoritative product source.
                        # The full existing prompt (scene + fidelity + marketplace)
                        # follows the anchor unchanged.
                        openai_prompt = (
                            f"{OPENAI_IDENTITY_ANCHOR}\n\n{prompt}"
                        )

                        # Use the images.edit endpoint with high input fidelity
                        response = await client.images.edit(
                            model=model,
                            image=img_file,
                            prompt=openai_prompt,
                            size=size,
                            # gpt-image-1 accepts "low" | "medium" | "high" |
                            # "auto". HIGH preserves luxury-detail fidelity on
                            # the primary ChatGPT-image path.
                            quality="high",
                            # input_fidelity controls how strongly the model
                            # preserves details from the input reference image.
                            # "high" maximises product-preservation fidelity —
                            # critical for jewellery where stone count, placement,
                            # geometry, and metal appearance must be exact.
                            input_fidelity="high",
                            # Lossless PNG output to preserve fine jewellery
                            # detail (stones, metalwork, engravings).
                            output_format="png",
                        )
                finally:
                    tmp.close()
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            else:
                # ── Text-to-Image Mode (DALL-E 3) ──────────────────────
                # NOTE: Omit `response_format` to avoid rejection by some API
                # endpoints.  The default is "url", which we download below.
                response = await client.images.generate(
                    model=model,
                    prompt=prompt,
                    size=size,
                    quality="standard",
                    n=1,
                )

            if response.data and len(response.data) > 0:
                image_data_b64 = response.data[0].b64_json
                image_url_from_api = response.data[0].url

                # Prefer b64_json if available (faster, no extra fetch)
                if image_data_b64:
                    image_bytes = base64.b64decode(image_data_b64)
                    data_url = f"data:image/png;base64,{image_data_b64}"

                    processing_time = time.time() - start_time
                    logger.info(
                        f"OpenAIImageProvider completed (b64_json) "
                        f"request_id={request_id} time={processing_time:.2f}s"
                    )

                    return ImageGenerationResult(
                        success=True,
                        image_url=data_url,
                        image_data=image_bytes,
                        mime_type="image/png",
                        provider_name=self.provider_name,
                        model_used=model,
                        processing_time=processing_time,
                        metadata={
                            "aspect_ratio": aspect_ratio,
                            "size": size,
                        },
                    )

                # Otherwise download from the URL
                if image_url_from_api:
                    async with httpx.AsyncClient(timeout=_request_timeout()) as hx:
                        img_resp = await hx.get(image_url_from_api)
                        img_resp.raise_for_status()
                        image_bytes = img_resp.content
                        image_data_b64 = base64.b64encode(image_bytes).decode("utf-8")
                        data_url = f"data:image/png;base64,{image_data_b64}"

                    processing_time = time.time() - start_time
                    revised_prompt = getattr(response.data[0], "revised_prompt", None)

                    logger.info(
                        f"OpenAIImageProvider completed (url-download) "
                        f"request_id={request_id} time={processing_time:.2f}s"
                    )

                    return ImageGenerationResult(
                        success=True,
                        image_url=data_url,
                        image_data=image_bytes,
                        mime_type="image/png",
                        provider_name=self.provider_name,
                        model_used=model,
                        processing_time=processing_time,
                        metadata={
                            "aspect_ratio": aspect_ratio,
                            "size": size,
                            "revised_prompt": revised_prompt,
                        },
                    )

            processing_time = time.time() - start_time
            return ImageGenerationResult(
                success=False,
                error="OpenAI returned an empty response with no image URL.",
                provider_name=self.provider_name,
                processing_time=processing_time,
            )

        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time

            # Classify the error
            error_lower = error_msg.lower()
            is_recoverable = any(
                term in error_lower
                for term in [
                    "429", "quota", "rate_limit", "rate limit",
                    "timeout", "deadline", "unavailable",
                    "5xx", "500", "502", "503", "504",
                    "service unavailable", "temporarily",
                    "network", "connection", "reset",
                    "internal", "server error",
                ]
            )

            logger.error(
                f"OpenAIImageProvider failed: {error_msg} "
                f"request_id={request_id} time={processing_time:.2f}s"
            )

            return ImageGenerationResult(
                success=False,
                error=error_msg,
                provider_name=self.provider_name,
                processing_time=processing_time,
                metadata={
                    "recoverable": is_recoverable,
                    "error_type": "recoverable" if is_recoverable else "unknown",
                },
            )
