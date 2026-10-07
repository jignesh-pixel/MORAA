"""Gemini Image Generation provider — uses Gemini image-capable models via the google-genai SDK.

This provider connects to the Google Gemini API to generate images using Gemini
models that support image output (e.g. ``gemini-3.1-flash-image``, the current
Nano Banana 2 generation). It uses the ``generate_content`` API with
``response_modalities=["IMAGE"]``, which is the recommended approach
(``ImageGenerationModel`` and ``generate_images`` are deprecated).

Requires ``GEMINI_API_KEY`` environment variable in the backend .env.
"""

import asyncio
import base64
import threading
import time
import weakref
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from app.ai.providers.image_base import BaseImageGenerationProvider, ImageGenerationResult
from app.ai.product_fidelity import REFERENCE_IMAGE_ANCHOR
from app.config import settings
from app.utils.logger import logger


# ─── Reference-image identity anchor ────────────────────────────────────
# Sent as the FIRST content part whenever a reference image is present.
# Gemini's native image models weigh the earliest parts most strongly, so
# anchoring the request with a concise identity lock — BEFORE the (possibly
# long) scene/preservation prompt — forces the model to treat the attached
# image as the exact product to photograph instead of re-drawing the piece
# from the text description. The image part follows the anchor immediately,
# and the scene prompt trails after it as "what to change" context.
# REFERENCE_IMAGE_ANCHOR imported from app.ai.product_fidelity


# One SDK client per event loop, reused for every call (PERF-4): a new client per call meant a new TLS
# handshake each time. The async client belongs to the loop it was created on, hence the per-loop cache
# (the application has one loop per process; tests and tools may create several).
_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Tuple[str, Any]]" = weakref.WeakKeyDictionary()


def _get_client(genai: Any, types: Any) -> Any:
    """The shared Gemini client for the running loop, created on first use with the request timeout."""
    loop = asyncio.get_running_loop()
    key = settings.GEMINI_API_KEY
    cached = _CLIENTS.get(loop)
    if cached is not None and cached[0] == key:
        return cached[1]
    timeout_ms = int(float(settings.GEMINI_IMAGE_TIMEOUT_SECONDS or 0) * 1000)
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=timeout_ms) if timeout_ms > 0 else None,
    )
    _CLIENTS[loop] = (key, client)
    return client


# ─── API audit: Gemini image calls per user action ──────────────────────
# Each Gemini image call is counted per order (its request_id) just before it is sent and logged at INFO as
# "Count N", so the number of paid calls behind one order can be read from the logs. A Clean Studio Shot is one
# call; a Catalog Pack is one call per style (every style shares the order's request_id), so a count above 1 is
# expected there and is not an error. In-process only, last 1000 orders.
_API_AUDIT_MAX_TRACKED = 1000
_api_audit_counts: "OrderedDict[str, int]" = OrderedDict()
_api_audit_lock = threading.Lock()


def _audit_gemini_image_call(request_id: Any, model_name: str) -> int:
    """Count and log one Gemini image call for ``request_id``. Returns this call's number for the order."""
    key = str(request_id or "")
    if not key or key == "unknown":
        logger.warning(
            f"[API-AUDIT] Gemini Image Call Triggered: untracked (no request_id) model={model_name}"
        )
        return 0
    with _api_audit_lock:
        count = _api_audit_counts.pop(key, 0) + 1
        _api_audit_counts[key] = count
        while len(_api_audit_counts) > _API_AUDIT_MAX_TRACKED:
            _api_audit_counts.popitem(last=False)
    logger.info(f"[API-AUDIT] Gemini Image Call Triggered: Count {count} request_id={key} model={model_name}")
    return count


async def close_gemini_client() -> None:
    """Close the running loop's shared client (called at application shutdown)."""
    try:
        cached = _CLIENTS.pop(asyncio.get_running_loop(), None)
    except RuntimeError:
        return
    if cached is not None:
        try:
            await cached[1].aio.aclose()
        except Exception as e:  # noqa: BLE001 -- shutdown must never fail on this
            logger.warning(f"Gemini client close failed: {e}")


class GeminiImageProvider(BaseImageGenerationProvider):
    """AI image generation provider using Google Gemini image-capable models."""

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def provider_version(self) -> str:
        return "2.0.0"

    @property
    def is_available(self) -> bool:
        return bool(settings.GEMINI_API_KEY)

    def supports_reference_image(self) -> bool:
        """Gemini supports multimodal input (text + images)."""
        return True

    async def generate_image(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        reference_image: Optional[bytes] = None,
        reference_mime_type: str = "image/jpeg",
    ) -> ImageGenerationResult:
        """Generate an image using Gemini's image-capable models.

        Uses ``client.models.generate_content()`` with
        ``response_modalities=["IMAGE"]`` via the ``google-genai`` SDK.

        When a reference_image is provided, Gemini uses multimodal input
        (text + image) to preserve the original product identity.

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
        has_reference = reference_image is not None
        start_time = time.time()

        logger.info(
            f"GeminiImageProvider generating image "
            f"request_id={request_id} aspect_ratio={aspect_ratio} "
            f"has_reference={has_reference}"
        )

        # Lazy import — google-genai is only required for this provider
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            return ImageGenerationResult(
                success=False,
                error="google-genai package not installed. Run: pip install google-genai",
                provider_name=self.provider_name,
                processing_time=time.time() - start_time,
            )

        try:
            client = _get_client(genai, types)

            model_name = settings.GEMINI_IMAGE_MODEL or "gemini-3.1-flash-image"

            # Map aspect ratio — Gemini image models support these ratios
            supported_aspect_ratios = {
                "1:1", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9",
            }
            if aspect_ratio not in supported_aspect_ratios:
                aspect_ratio = "4:5"

            # Build the generation config
            config = types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=types.ImageConfig(
                    aspect_ratio=aspect_ratio,
                ),
            )

            # Build contents — include reference image if provided.
            # When present, the request is anchored as:
            #   [identity-lock text] → [reference image] → [scene prompt]
            # so the model locks onto the uploaded product BEFORE reading the
            # scene/preservation text (which can be long and descriptive and
            # would otherwise compete with the image for product identity).
            if has_reference:
                reference_part = types.Part.from_bytes(
                    data=reference_image,
                    mime_type=reference_mime_type,
                )
                contents = [REFERENCE_IMAGE_ANCHOR, reference_part, prompt]
                logger.info(
                    f"GeminiImageProvider: using reference image for "
                    f"product preservation request_id={request_id}"
                )
            else:
                contents = prompt

            # Native async call (no worker thread, so the shared 6-thread pool can never be filled up by a
            # Pack), under the client timeout set in _get_client plus a hard deadline as a backstop.
            timeout = float(settings.GEMINI_IMAGE_TIMEOUT_SECONDS or 0)
            _audit_gemini_image_call(request_id, model_name)
            call = client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            response = await (asyncio.wait_for(call, timeout=timeout + 10) if timeout > 0 else call)

            # ── Process the response ────────────────────────────────────
            if response.candidates:
                candidate = response.candidates[0]
                for part in candidate.content.parts:
                    if part.inline_data:
                        image_data = part.inline_data.data
                        mime_type = part.inline_data.mime_type or "image/png"

                        processing_time = time.time() - start_time
                        logger.info(
                            f"GeminiImageProvider completed "
                            f"request_id={request_id} time={processing_time:.2f}s"
                        )

                        # Raw bytes only: the base64 data URL is built by result.as_data_url() if a caller
                        # ever needs it (PERF-5).
                        return ImageGenerationResult(
                            success=True,
                            image_data=image_data,
                            mime_type=mime_type,
                            provider_name=self.provider_name,
                            model_used=model_name,
                            processing_time=processing_time,
                            metadata={
                                "aspect_ratio": aspect_ratio,
                            },
                        )

            # No image data found in the response
            processing_time = time.time() - start_time
            logger.warning(
                f"GeminiImageProvider: no image data in response "
                f"request_id={request_id}"
            )
            return ImageGenerationResult(
                success=False,
                error="Gemini returned a response but no image data was found.",
                provider_name=self.provider_name,
                processing_time=processing_time,
            )

        except Exception as e:
            error_msg = str(e)
            if isinstance(e, asyncio.TimeoutError) and not error_msg:
                error_msg = (
                    f"Gemini image request timeout: deadline exceeded after "
                    f"{float(settings.GEMINI_IMAGE_TIMEOUT_SECONDS or 0) + 10:.0f}s"
                )
            processing_time = time.time() - start_time

            # Classify the error for fallback decision
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
                    "resource exhausted",
                ]
            )
            is_bad_request = any(
                term in error_lower
                for term in [
                    "invalid", "bad request", "400",
                    "permission", "403", "api key",
                    "not found", "404", "not supported",
                    "unsupported", "safety", "blocked",
                    "harmful", "content filtered",
                ]
            )

            logger.error(
                f"GeminiImageProvider failed: {error_msg} "
                f"request_id={request_id} time={processing_time:.2f}s "
                f"recoverable={is_recoverable}"
            )

            return ImageGenerationResult(
                success=False,
                error=error_msg,
                provider_name=self.provider_name,
                processing_time=processing_time,
                metadata={
                    "recoverable": is_recoverable,
                    "bad_request": is_bad_request,
                    "error_type": "recoverable" if is_recoverable
                    else "bad_request" if is_bad_request
                    else "unknown",
                },
            )
