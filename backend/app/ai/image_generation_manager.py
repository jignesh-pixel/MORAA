"""Image Generation Manager — orchestrates image generation across multiple AI providers.

The manager provides a single ``generate_image()`` entry point that:
1. Attempts generation with the primary provider
2. Falls back to the secondary provider on recoverable failures
3. Only falls back for: HTTP 429 rate limits, timeouts, temporary 5xx,
   transient network failures
4. Does NOT fall back for: invalid prompts, bad requests, missing config,
   programming errors
5. Halts immediately (no fallback, no retries) on quota/billing exhaustion
   and authentication/configuration failures — one controlled attempt per
   provider, never an uncontrolled retry loop.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.ai.providers.image_base import (
    BaseImageGenerationProvider,
    ImageGenerationResult,
)
from app.ai.providers.gemini_image_provider import GeminiImageProvider
from app.ai.providers.openai_image_provider import OpenAIImageProvider
from app.ai.marketplaces.registry import get_marketplace_presentation
from app.ai.product_fidelity import REFERENCE_PRIORITY_BLOCK, evaluate_fidelity
from app.config import settings
from app.utils.logger import logger


# ─── Registry — maps config values to provider classes ─────────────────
_IMAGE_PROVIDER_REGISTRY: Dict[str, type[BaseImageGenerationProvider]] = {
    "gemini": GeminiImageProvider,
    "openai": OpenAIImageProvider,
}


# ─── Recoverable failure patterns ──────────────────────────────────────
_RECOVERABLE_PATTERNS = [
    "429",
    "rate_limit",
    "rate limit",
    "timeout",
    "deadline exceeded",
    "deadline_exceeded",
    "unavailable",
    "service unavailable",
    "temporarily",
    "network",
    "connection",
    "reset",
    "internal",
    "server error",
    "resource exhausted",
    "resource_exhausted",
    "500",
    "502",
    "503",
    "504",
    "5xx",
]


def _is_recoverable_error(error_message: str) -> bool:
    """Check if an error is recoverable and should trigger a fallback.

    NOTE: quota/billing exhaustion is a project-level condition, not a
    transient one. It is explicitly excluded here so it can never be treated
    as a recoverable error that justifies retrying or falling back.
    """
    if not error_message:
        return False
    if _is_quota_exhaustion(error_message):
        return False
    error_lower = error_message.lower()
    return any(pattern in error_lower for pattern in _RECOVERABLE_PATTERNS)


# ─── Quota / billing exhaustion (halt — never fall back) ────────────
# Quota and billing exhaustion are project-level conditions: if the primary
# provider has no capacity left, burning the fallback provider's quota on
# the same request is uncontrolled credit consumption. These errors halt
# the chain immediately after ONE attempt — no fallback, no retries.
_QUOTA_EXHAUSTION_PATTERNS = [
    "quota exceeded",
    "resource exhausted",
    "resource_exhausted",
    "credit_balance_exhausted",
    "billing",
]


def _is_quota_exhaustion(error_message: str) -> bool:
    """Check if an error is quota/billing exhaustion (halt, no fallback)."""
    if not error_message:
        return False
    error_lower = error_message.lower()
    return any(pattern in error_lower for pattern in _QUOTA_EXHAUSTION_PATTERNS)


# ─── Non-recoverable patterns (never fallback) ─────────────────────────
_NON_RECOVERABLE_PATTERNS = [
    "invalid prompt",
    "invalid payload",
    "invalid api request",
    "bad request",
    "400",
    "api key not",
    "api key is invalid",
    "api key missing",
    "safety",
    "blocked",
    "harmful",
    "content filtered",
    "content_filtered",
    "prompt blocked",
    "prompt was blocked",
]


def _is_non_recoverable_error(error_message: str) -> bool:
    """Check if an error is definitively non-recoverable."""
    if not error_message:
        return False
    error_lower = error_message.lower()
    return any(pattern in error_lower for pattern in _NON_RECOVERABLE_PATTERNS)


# Spend guard for every generation path (WhatsApp pack, web API routes, tests):
# GENERATION_ENABLED is the hard kill switch, MAX_GENERATIONS_PER_DAY a daily
# ceiling on generate_image() calls.
# ponytail: in-process counter, so the real ceiling is cap x worker processes;
# move the counter to the DB/Redis if that ever matters.
_spend_day: Optional[str] = None
_spend_count: int = 0


def _spend_blocked() -> Optional[str]:
    global _spend_day, _spend_count
    if getattr(settings, "GENERATION_ENABLED", True) is False:
        return "Image generation is disabled (GENERATION_ENABLED=false)"
    cap = getattr(settings, "MAX_GENERATIONS_PER_DAY", None)
    if isinstance(cap, bool) or not isinstance(cap, int):
        return None  # no valid cap configured -- never throttle on a bad value
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _spend_day != today:
        _spend_day, _spend_count = today, 0
    if _spend_count >= max(cap, 1):
        return "Daily image-generation cap reached (MAX_GENERATIONS_PER_DAY)"
    _spend_count += 1
    return None


class ImageGenerationManager:
    """Orchestrates image generation with automatic provider failover."""

    MAX_RETRIES_PER_PROVIDER: int = 0
    RETRY_DELAY_SECONDS: float = 2.0

    def __init__(self):
        self._providers: Dict[str, BaseImageGenerationProvider] = {}
        self._initialised = False

    def _init_providers(self) -> None:
        """Lazy-initialise provider instances."""
        if self._initialised:
            return
        for name, cls in _IMAGE_PROVIDER_REGISTRY.items():
            try:
                self._providers[name] = cls()
            except Exception as e:
                logger.warning(f"Failed to initialise image provider '{name}': {e}")
        self._initialised = True

    def _get_provider(self, name: str) -> Optional[BaseImageGenerationProvider]:
        """Get a provider instance by name."""
        self._init_providers()
        return self._providers.get(name)

    def _get_provider_chain(self) -> List[str]:
        """Build the ordered provider chain for image generation.

        The chain strictly respects ``settings.PRIMARY_IMAGE_PROVIDER``: the
        configured primary is always the first provider tried whenever it is
        a registered provider. ``settings.FALLBACK_IMAGE_PROVIDER`` is only
        appended after it. The hardcoded default pair is used solely when the
        configured primary is not a registered provider — the setting is
        never silently ignored in favour of a different primary.
        """
        primary = (getattr(settings, "PRIMARY_IMAGE_PROVIDER", "openai") or "openai").strip().lower()
        fallback = (getattr(settings, "FALLBACK_IMAGE_PROVIDER", "gemini") or "gemini").strip().lower()

        chain: List[str] = []
        if primary in _IMAGE_PROVIDER_REGISTRY:
            chain.append(primary)
        if fallback in _IMAGE_PROVIDER_REGISTRY and fallback != primary:
            chain.append(fallback)

        return chain or ["openai", "gemini"]

    async def generate_image(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        force_provider: Optional[str] = None,
        reference_image: Optional[bytes] = None,
        reference_mime_type: str = "image/jpeg",
        marketplace: Optional[str] = None,
    ) -> ImageGenerationResult:
        context = dict(context) if context else {}
        request_id = context.get("request_id", "unknown")

        blocked = _spend_blocked()
        if blocked:
            logger.error(f"ImageGenerationManager: {blocked} -- no provider called request_id={request_id}")
            return ImageGenerationResult(
                success=False,
                error=blocked,
                provider_name="none",
                metadata={"non_recoverable": True, "spend_guard": True},
            )
        has_reference = reference_image is not None

        if force_provider:
            provider_chain = [force_provider]
        else:
            provider_chain = self._get_provider_chain()

        effective_prompt = prompt
        if has_reference and "REFERENCE IMAGE PRIORITY" not in prompt.upper():
            effective_prompt = f"{prompt}\n\n{REFERENCE_PRIORITY_BLOCK}"

        if marketplace:
            marketplace_presentation = get_marketplace_presentation(marketplace)
            if marketplace_presentation:
                effective_prompt = f"{effective_prompt}\n\n{marketplace_presentation.prompt_block}"
                if marketplace_presentation.default_aspect_ratio:
                    context["aspect_ratio"] = marketplace_presentation.default_aspect_ratio

        logger.info(
            f"ImageGenerationManager: generating image request_id={request_id} "
            f"chain={provider_chain} has_reference={has_reference}"
        )

        last_error: Optional[str] = None
        fallback_reason: Optional[str] = None
        provider_used: Optional[str] = None

        for idx, provider_name in enumerate(provider_chain):
            provider = self._get_provider(provider_name)
            if provider is None or not provider.is_available:
                continue

            is_fallback = idx > 0
            provider_has_ref = has_reference and provider.supports_reference_image()

            try:
                if provider_has_ref:
                    result = await provider.generate_image(
                        effective_prompt,
                        context,
                        reference_image=reference_image,
                        reference_mime_type=reference_mime_type,
                    )
                else:
                    result = await provider.generate_image(effective_prompt, context)

                if result.success:
                    result.provider_name = provider_name
                    result.fallback_used = is_fallback
                    result.fallback_reason = fallback_reason
                    meta = dict(result.metadata) if result.metadata else {}
                    meta["generation_mode"] = "fallback" if is_fallback else "primary"

                    # Product-fidelity QA hook (P1·03). `evaluate_fidelity`
                    # returns a "manual QA required" result when no structured
                    # fidelity spec / generated-image attributes are supplied
                    # (nothing in the current pipeline produces those yet — see
                    # its docstring) rather than fabricating a pass. Wired here
                    # so a future caller that populates
                    # context["product_fidelity"] / context["generated_attributes"]
                    # gets real PASS/FAIL evaluation with no further plumbing.
                    # Never allowed to fail the generation itself.
                    try:
                        fidelity_result = evaluate_fidelity(
                            context.get("product_fidelity"),
                            context.get("generated_attributes"),
                        )
                        meta["fidelity_check"] = fidelity_result.model_dump(
                            exclude_none=True
                        )
                    except Exception as fidelity_error:
                        logger.error(
                            f"Fidelity QA evaluation failed (non-blocking): "
                            f"{fidelity_error} request_id={request_id}"
                        )

                    result.metadata = meta
                    logger.info(
                        f"ImageGenerationManager: success with provider "
                        f"'{provider_name}' mode={meta['generation_mode']} "
                        f"time={result.processing_time:.2f}s "
                        f"request_id={request_id}"
                    )
                    return result

                error_msg = result.error or ""
                last_error = error_msg
                provider_used = provider_name

                # Quota/billing exhaustion and non-recoverable errors halt the
                # chain immediately after ONE attempt — no fallback, no retries.
                if _is_quota_exhaustion(error_msg) or _is_non_recoverable_error(error_msg):
                    logger.warning(
                        f"Image provider '{provider_name}' returned "
                        f"non-recoverable error: {error_msg} "
                        f"request_id={request_id} — skipping fallback"
                    )
                    return ImageGenerationResult(
                        success=False,
                        error=error_msg,
                        provider_name=provider_name,
                        processing_time=result.processing_time,
                        metadata={"non_recoverable": True},
                    )

                fallback_reason = f"{provider_name}_{_classify_error(error_msg)}"
                logger.warning(
                    f"Image provider '{provider_name}' failed ({error_msg}) "
                    f"request_id={request_id} — trying fallback"
                )

            except Exception as e:
                error_msg = str(e)
                last_error = error_msg
                provider_used = provider_name
                fallback_reason = f"{provider_name}_exception"
                logger.warning(f"Provider '{provider_name}' exception: {error_msg}")

        # All providers failed — return an honest failure so callers (and the
        # wallet layer) can handle it; never fabricate a result.
        logger.error(
            f"ImageGenerationManager: all providers failed "
            f"request_id={request_id} last_error={last_error}"
        )
        return ImageGenerationResult(
            success=False,
            error=last_error or "All image generation providers failed",
            provider_name=provider_used or "none",
            fallback_used=True,
            fallback_reason=fallback_reason,
        )

    def get_available_providers(self) -> List[Dict[str, Any]]:
        self._init_providers()
        return [
            {
                "name": name,
                "version": provider.provider_version,
                "available": provider.is_available,
                "capabilities": provider.capabilities,
            }
            for name, provider in self._providers.items()
        ]

    def get_provider_chain(self) -> List[str]:
        return self._get_provider_chain()


def _classify_error(error_message: str) -> str:
    error_lower = error_message.lower()
    if any(t in error_lower for t in ["429", "quota", "rate", "resource exhausted"]):
        return "quota_exceeded"
    if any(t in error_lower for t in ["timeout", "deadline"]):
        return "timeout"
    if any(t in error_lower for t in ["unavailable", "503"]):
        return "service_unavailable"
    if any(t in error_lower for t in ["network", "connection", "reset"]):
        return "network_error"
    if any(t in error_lower for t in ["internal", "500", "502", "504", "5xx"]):
        return "server_error"
    return "transient_error"