"""OpenAI provider — connects to OpenAI Vision API for image analysis.

This provider calls the OpenAI API directly using the openai Python SDK.
It is managed by the ``AIProviderManager`` and supports failover.

Uses ``gpt-4o-mini`` (or configured model) with ``response_format={"type": "json_object"}``
so the response is always valid JSON.

Requires ``OPENAI_API_KEY`` environment variable in the backend .env.
"""

import base64
import json
import time
from typing import Any, Dict, List, Optional

from app.ai.providers.base import BaseAIProvider, ProviderResult
from app.config import settings
from app.utils.logger import logger


class OpenAIProvider(BaseAIProvider):
    """AI provider that uses OpenAI Vision API for image analysis."""

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def provider_version(self) -> str:
        return "1.0.0"

    @property
    def is_available(self) -> bool:
        return bool(settings.OPENAI_API_KEY)

    async def validate_image(self, image_path: str) -> bool:
        """Validate image by checking it can be opened and base64-encoded."""
        try:
            from PIL import Image as PILImage
            img = PILImage.open(image_path)
            img.verify()
            return True
        except Exception:
            return False

    async def analyze(
        self,
        image_paths: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> ProviderResult:
        """Analyse one or more images using OpenAI Vision API.

        For multi-image analysis, all images are sent together as
        multiple image_url parts in a single message so the model
        can cross-reference them.

        Args:
            image_paths: Path(s) to uploaded image files.
            context: Optional dict with ``request_id``, ``custom_prompt``, etc.

        Returns:
            ProviderResult with structured analysis data.
        """
        request_id = (context or {}).get("request_id", "unknown")
        custom_prompt = (context or {}).get("custom_prompt", "")
        start_time = time.time()

        logger.info(
            f"OpenAIProvider analyzing {len(image_paths)} image(s) "
            f"request_id={request_id}"
        )

        # Lazy import — openai is only required for this provider
        try:
            from openai import AsyncOpenAI
        except ImportError:
            return ProviderResult(
                success=False,
                error="openai package not installed. Run: pip install openai",
                provider_name=self.provider_name,
                processing_time=time.time() - start_time,
            )

        # Build the analysis prompt
        analysis_prompt = custom_prompt or (
            "You are MORAA GemVision, a precise jewellery and product analysis AI. "
            "Analyse the provided image(s) and return a structured JSON response. "
            "Do NOT include markdown formatting or code fences — return raw JSON only.\n\n"
            "Fields:\n"
            "- material: string (detected material, e.g. 'Yellow Gold', 'Silver', 'Bronze')\n"
            "- gold_purity: string (e.g. '18K', '14K', '925', or 'Unknown')\n"
            "- weight: float (estimated weight in grams)\n"
            "- category: string (e.g. 'Ring', 'Necklace', 'Earrings', 'Electronics')\n"
            "- estimated_price: float (estimated market price in USD)\n"
            "- confidence: float (0.0 to 1.0)\n"
            "- gemstones: array of strings\n"
            "- style: string\n"
            "- era: string\n"
            "- condition: string\n"
            "- summary: string (natural language description)"
        )

        # Build content parts for OpenAI message format
        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": analysis_prompt}
        ]

        for img_path in image_paths:
            try:
                with open(img_path, "rb") as f:
                    image_bytes = f.read()
                base64_data = base64.b64encode(image_bytes).decode("utf-8")
                # Determine mime type from file extension
                mime_type = "image/jpeg"
                if img_path.lower().endswith(".png"):
                    mime_type = "image/png"
                elif img_path.lower().endswith(".webp"):
                    mime_type = "image/webp"

                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_data}",
                        "detail": "high",
                    },
                })
            except Exception as e:
                logger.error(f"OpenAIProvider: failed to load image {img_path}: {e}")
                return ProviderResult(
                    success=False,
                    error=f"Failed to load image: {e}",
                    provider_name=self.provider_name,
                    processing_time=time.time() - start_time,
                )

        if len(image_paths) > 1:
            content_parts.append({
                "type": "text",
                "text": (
                    "The above images are different views/angles of the same item. "
                    "Analyse them collectively and provide a single unified analysis."
                ),
            })

        try:
            # max_retries=0: one HTTP attempt per call (the SDK default is 2 retries).
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0)

            response = await client.chat.completions.create(
                model=settings.OPENAI_MODEL or "gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": content_parts,
                    }
                ],
                max_tokens=4096,
                temperature=0.2,
                response_format={"type": "json_object"},
            )

            raw_text = response.choices[0].message.content or ""

            # Parse JSON from response
            cleaned = raw_text.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            result_data = json.loads(cleaned)

            processing_time = time.time() - start_time
            model_used = response.model or settings.OPENAI_MODEL

            logger.info(
                f"OpenAIProvider completed: {result_data.get('category', 'unknown')} "
                f"request_id={request_id} model={model_used} time={processing_time:.2f}s"
            )

            return ProviderResult(
                success=True,
                data=result_data,
                provider_name=self.provider_name,
                processing_time=processing_time,
                tool_executions=[
                    {
                        "name": "openai_analysis",
                        "order": 0,
                        "status": "completed",
                        "duration": round(processing_time, 3),
                        "output": f"Analysed {len(image_paths)} image(s) via OpenAI API ({model_used})",
                    }
                ],
            )

        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time
            logger.error(
                f"OpenAIProvider failed: {error_msg} "
                f"request_id={request_id} time={processing_time:.2f}s"
            )
            return ProviderResult(
                success=False,
                error=error_msg,
                provider_name=self.provider_name,
                processing_time=processing_time,
            )
