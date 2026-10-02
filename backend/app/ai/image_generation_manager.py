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
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional

from app.ai.providers.image_base import (
    BaseImageGenerationProvider,
    ImageGenerationResult,
)
from app.ai.provider_protection import breaker, bucket
from app.ai.providers.gemini_image_provider import GeminiImageProvider
from app.ai.providers.openai_image_provider import OpenAIImageProvider
from app.ai.marketplaces.registry import get_marketplace_presentation
from app.ai.product_fidelity import REFERENCE_PRIORITY_BLOCK, evaluate_fidelity
from app.config import settings
from app.services import metrics, spend_counter
from app.utils.executors import run_io
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

# Markers that the money or the DAY's quota is gone: nothing a retry or the other provider could fix.
_HARD_EXHAUSTION_MARKERS = (
    "credit_balance_exhausted",
    "insufficient_quota",
    "billing_not_active",
    "billing hard limit",
    "billing_hard_limit",
    "prepayment",
    "per day",
    "perday",
    "requests per day",
    "daily",
    "limit: 0",
)
# Markers that the limit is per minute / per second: it clears within seconds, so a retry is right.
_RATE_LIMIT_MARKERS = (
    "per minute",
    "perminute",
    "per second",
    "persecond",
    "rate limit",
    "rate_limit",
    "too many requests",
    "retry in",
    "retrydelay",
    "retry_delay",
    "retry-after",
    "overloaded",
    "service unavailable",
    "unavailable",
    "503",
    "429",
)


def _is_quota_exhaustion(error_message: str) -> bool:
    """Check if an error is billing / daily-quota exhaustion (halt, no retry, no fallback).

    A Gemini 429 ``RESOURCE_EXHAUSTED`` is NOT automatically this: the same wording ("check your plan and
    billing details") is used for a per-minute limit that clears in seconds. Only hard markers (credit gone,
    billing inactive, per-day quota, ``limit: 0``) halt; a message that names a per-minute / rate limit does
    not, and is retried and then falls back instead (EXT-2).
    """
    if not error_message:
        return False
    error_lower = error_message.lower()
    if any(marker in error_lower for marker in _HARD_EXHAUSTION_MARKERS):
        return True
    if any(marker in error_lower for marker in _RATE_LIMIT_MARKERS):
        return False
    return any(pattern in error_lower for pattern in _QUOTA_EXHAUSTION_PATTERNS)


def _is_retryable_rate_limit(error_message: str) -> bool:
    """A rate limit (429) or temporary overload (503) worth retrying on the same provider after a short wait."""
    if not error_message or _is_quota_exhaustion(error_message):
        return False
    error_lower = error_message.lower()
    return any(marker in error_lower for marker in _RATE_LIMIT_MARKERS) or any(
        pattern in error_lower for pattern in ("resource exhausted", "resource_exhausted")
    )


_RETRY_HINT_RE = re.compile(r"(?:retry in|retrydelay['\": ]+|retry-after['\": ]+)\s*([0-9]+(?:\.[0-9]+)?)\s*s?", re.I)


def _retry_delay_seconds(error_message: str, attempt: int) -> Optional[float]:
    """Seconds to wait before retry number ``attempt`` (0-based), or None when waiting is not worth it.

    Exponential backoff with +-50% jitter (so a burst of failed calls does not retry in lockstep). If the
    provider says how long to wait and that is longer than the cap, retrying here would stall the order, so
    None is returned and the caller moves on to the next provider.
    """
    base = max(float(settings.IMAGE_RETRY_BACKOFF_BASE_SECONDS), 0.0)
    cap = max(float(settings.IMAGE_RETRY_BACKOFF_CAP_SECONDS), 0.0)
    backoff = min(base * (2 ** attempt), cap)
    delay = backoff * random.uniform(0.5, 1.5) if backoff else 0.0
    hint = _RETRY_HINT_RE.search(error_message or "")
    if hint:
        asked = float(hint.group(1))
        if asked > cap:
            return None
        # Jitter only our own backoff: never retry earlier than the provider said it would allow.
        delay = max(delay, asked)
    return delay


def _is_provider_outage(error_message: str) -> bool:
    """A failure that says the provider itself is unwell (timeout, 5xx, overload, rate or quota exhaustion)."""
    if not error_message:
        return False
    lowered = error_message.lower()
    if "timeout" in lowered or "timed out" in lowered or "connect" in lowered:
        return True
    return _is_recoverable_error(error_message) or _is_retryable_rate_limit(error_message)         or _is_quota_exhaustion(error_message)


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
    # Gemini answered but returned no image (e.g. a safety finish without an
    # exception). The call was already made; do not pay a second provider.
    "no image data",
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


_spend_lock = threading.Lock()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _counts_against_cap() -> bool:
    """True when the cap is a real number (otherwise nothing was counted, so there is nothing to give back)."""
    cap = getattr(settings, "MAX_GENERATIONS_PER_DAY", None)
    return isinstance(cap, int) and not isinstance(cap, bool)


def _spend_check(count: int, consume: bool) -> Optional[str]:
    """The one rule for the kill switch and the daily cap; ``consume`` decides whether slots are taken.

    The count is shared by every server process through the database (``spend_counter``). If the database
    cannot answer, the in-process counter below is used instead, so a counter problem never blocks customers.
    """
    global _spend_day, _spend_count
    if getattr(settings, "GENERATION_ENABLED", True) is False:
        return "Image generation is disabled (GENERATION_ENABLED=false)"
    cap = getattr(settings, "MAX_GENERATIONS_PER_DAY", None)
    if isinstance(cap, bool) or not isinstance(cap, int):
        return None  # no valid cap configured -- never throttle on a bad value
    today = _today()
    n = max(count, 1)
    if consume:
        shared = spend_counter.reserve(today, n, max(cap, 1))
    else:
        already = spend_counter.used(today)
        shared = None if already is None else already + n <= max(cap, 1)
    if shared is True:
        return None
    if shared is False:
        return "Daily image-generation cap reached (MAX_GENERATIONS_PER_DAY)"
    with _spend_lock:
        if _spend_day != today:
            _spend_day, _spend_count = today, 0
        if _spend_count + n > max(cap, 1):
            return "Daily image-generation cap reached (MAX_GENERATIONS_PER_DAY)"
        if consume:
            _spend_count += n
    return None


def current_spend_day() -> str:
    """Today's UTC date as used by the spend counter. Capture it when slots are taken and pass it to
    ``release_generation_slots``, so a release after midnight UTC still credits the day that was charged."""
    return _today()


def release_generation_slots(count: int, day: Optional[str] = None) -> None:
    """Give back slots that were taken for calls that failed (the daily ceiling counts only real spend).

    ``day`` is the day the slots were taken on (default: today)."""
    global _spend_count
    n = max(int(count), 0)
    if not n or not _counts_against_cap():
        return
    charged_day = day or _today()
    if spend_counter.release(charged_day, n) is True:
        return
    with _spend_lock:
        if _spend_day == charged_day:           # the in-process counter only holds the current day
            _spend_count = max(_spend_count - n, 0)


def record_external_spend(count: int = 1) -> None:
    """Count paid AI calls made outside ``generate_image`` (the photo pre-check) toward today's total.

    Only counted, never blocked: refusing a customer's photo because the cap is full would be worse than
    letting the count run slightly over; the generation calls themselves are what the cap stops."""
    global _spend_day, _spend_count
    if not _counts_against_cap():
        return
    n = max(int(count), 1)
    if spend_counter.reserve(_today(), n, None) is True:
        return
    with _spend_lock:
        today = _today()
        if _spend_day != today:
            _spend_day, _spend_count = today, 0
        _spend_count += n


def _spend_blocked(count: int = 1) -> Optional[str]:
    """Consume ``count`` generation slots, or return why that is not allowed.

    All-or-nothing: when fewer than ``count`` slots remain, nothing is
    consumed. ``count=1`` is the original per-call behaviour.
    """
    return _spend_check(count, consume=True)


def generation_capacity_blocked(count: int = 1) -> Optional[str]:
    """Would ``count`` generations be allowed right now? Same rules as ``_spend_blocked``, but consumes nothing.

    Used BEFORE a customer is charged (UX-4), so an order that cannot be generated today is declined
    without taking money. Best effort: the real slots are still taken when generation starts.
    """
    return _spend_check(count, consume=False)


def reserve_generation_slots(count: int) -> Optional[str]:
    """Reserve ``count`` slots up front for a multi-image order (Catalog Pack).

    The whole order either fits under MAX_GENERATIONS_PER_DAY or is refused
    before any provider is called, so the cap can never cut a paid pack off
    half-way. Returns None when reserved, else the reason it was refused.
    The calls made for the order then pass ``spend_reserved=True``.
    """
    return _spend_blocked(count)


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

    async def _call_provider_once(
        self,
        provider: BaseImageGenerationProvider,
        prompt: str,
        context: Dict[str, Any],
        reference_image: Optional[bytes],
        reference_mime_type: str,
        request_id: str,
    ) -> ImageGenerationResult:
        """One provider call under a hard deadline: a hung call becomes an honest, recoverable failure."""
        timeout = float(settings.IMAGE_PROVIDER_TIMEOUT_SECONDS or 0)
        if reference_image is not None:
            call = provider.generate_image(
                prompt, context, reference_image=reference_image, reference_mime_type=reference_mime_type
            )
        else:
            call = provider.generate_image(prompt, context)
        if timeout <= 0:
            return await call
        started = time.time()
        try:
            return await asyncio.wait_for(call, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"ImageGenerationManager: provider call timed out after {timeout:.0f}s request_id={request_id}")
            return ImageGenerationResult(
                success=False,
                error=f"Image provider timeout: deadline exceeded after {timeout:.0f}s",
                provider_name=getattr(provider, "provider_name", "unknown"),
                processing_time=time.time() - started,
            )

    async def _attempt_with_retries(
        self,
        provider: BaseImageGenerationProvider,
        provider_name: str,
        prompt: str,
        context: Dict[str, Any],
        reference_image: Optional[bytes],
        reference_mime_type: str,
        request_id: str,
    ) -> ImageGenerationResult:
        """Call one provider; retry a rate limit (429) / overload (503) with jittered waits (EXT-2).

        Billing or daily-quota exhaustion, bad requests and timeouts are returned at once: waiting cannot fix
        them. The retries are on the SAME slot of the daily spend counter (one order = one slot), and the
        prompt and request sent are identical on every attempt.
        """
        retries = max(int(settings.IMAGE_RATE_LIMIT_RETRIES or 0), 0)
        if not breaker.allow(provider_name):
            logger.warning(f"Image provider '{provider_name}' circuit is open; not calling it request_id={request_id}")
            return ImageGenerationResult(
                success=False,
                error=f"Image provider temporarily unavailable: circuit open for '{provider_name}' (503 service unavailable)",
                provider_name=provider_name,
                processing_time=0.0,
            )
        result = await self._guarded_call(
            provider, provider_name, prompt, context, reference_image, reference_mime_type, request_id
        )
        for attempt in range(retries):
            if result.success or not _is_retryable_rate_limit(result.error or ""):
                break
            delay = _retry_delay_seconds(result.error or "", attempt)
            if delay is None:
                logger.warning(
                    f"Image provider '{provider_name}' asked for a long wait; moving on request_id={request_id}"
                )
                break
            logger.warning(
                f"Image provider '{provider_name}' rate limited; retry {attempt + 1}/{retries} "
                f"in {delay:.1f}s request_id={request_id}"
            )
            await asyncio.sleep(delay)
            result = await self._guarded_call(
                provider, provider_name, prompt, context, reference_image, reference_mime_type, request_id
            )
        return result

    async def _guarded_call(
        self,
        provider: BaseImageGenerationProvider,
        provider_name: str,
        prompt: str,
        context: Dict[str, Any],
        reference_image: Optional[bytes],
        reference_mime_type: str,
        request_id: str,
    ) -> ImageGenerationResult:
        """One call through the token bucket, with the outcome fed to the circuit breaker."""
        if not await bucket.acquire(provider_name):
            breaker.record_neutral(provider_name)
            return ImageGenerationResult(
                success=False,
                error=f"Image provider busy: local rate limit for '{provider_name}' (429 too many requests, retry in 60s)",
                provider_name=provider_name,
                processing_time=0.0,
            )
        result = await self._call_provider_once(
            provider, prompt, context, reference_image, reference_mime_type, request_id
        )
        if result.success:
            breaker.record_success(provider_name)
        elif _is_provider_outage(result.error or ""):
            breaker.record_outage_failure(provider_name)
        else:
            breaker.record_neutral(provider_name)        # the request itself was refused: not the provider's health
        return result

    async def generate_image(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        force_provider: Optional[str] = None,
        reference_image: Optional[bytes] = None,
        reference_mime_type: str = "image/jpeg",
        marketplace: Optional[str] = None,
        spend_reserved: bool = False,
    ) -> ImageGenerationResult:
        """Generate one image (see ``_generate_image_inner``). A slot this call took from the daily counter is
        given back if the call ends in failure, so failed generations do not eat the daily ceiling (COST-1)."""
        state: Dict[str, Any] = {}
        succeeded = False
        try:
            result = await self._generate_image_inner(
                prompt, context, force_provider, reference_image, reference_mime_type, marketplace,
                spend_reserved, state,
            )
            succeeded = bool(result.success)
            return result
        finally:
            # Also when the call is cancelled or raises: a slot is kept only by a call that produced an image.
            if state.get("slot_taken") and not succeeded:
                try:
                    await run_io(release_generation_slots, 1, state.get("day"))
                except BaseException:  # noqa: BLE001 -- giving a slot back must never mask the real outcome
                    pass

    async def _generate_image_inner(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]],
        force_provider: Optional[str],
        reference_image: Optional[bytes],
        reference_mime_type: str,
        marketplace: Optional[str],
        spend_reserved: bool,
        state: Dict[str, Any],
    ) -> ImageGenerationResult:
        context = dict(context) if context else {}
        request_id = context.get("request_id", "unknown")

        # spend_reserved: the slot was already taken by reserve_generation_slots()
        # for this order; the kill switch still applies to every call.
        if spend_reserved:
            blocked = (
                "Image generation is disabled (GENERATION_ENABLED=false)"
                if getattr(settings, "GENERATION_ENABLED", True) is False
                else None
            )
        else:
            # The shared counter is a database call: keep it off the event loop.
            state["day"] = _today()                 # the day this slot is charged to (for a later release)
            blocked = await run_io(_spend_blocked)
            state["slot_taken"] = blocked is None and _counts_against_cap()
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
                result = await self._attempt_with_retries(
                    provider,
                    provider_name,
                    effective_prompt,
                    context,
                    reference_image if provider_has_ref else None,
                    reference_mime_type,
                    request_id,
                )

                if result.success:
                    metrics.record_provider(provider_name, "success")
                    if is_fallback:
                        metrics.registry.inc("moraa_provider_fallbacks_total", {"provider": provider_name})
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
                metrics.record_provider(provider_name, _classify_error(error_msg) if error_msg else "failed")

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
                metrics.record_provider(provider_name, "exception")
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