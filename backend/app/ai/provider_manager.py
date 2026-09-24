"""AI Provider Manager — orchestrates analysis across multiple AI providers.

The manager provides a single ``analyze()`` entry point that:
1. Selects the primary provider from configuration
2. Falls back through registered providers on failure
3. Retries each provider before moving to the next
4. Logs every step for enterprise traceability

Usage::

    from app.ai.provider_manager import AIProviderManager

    manager = AIProviderManager()
    result = await manager.analyze(image_paths=["/path/to/image.jpg"])
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.ai.providers.base import BaseAIProvider, ProviderResult
from app.ai.providers import (
    GeminiProvider,
    OpenAIProvider,
    ClaudeProvider,
    LocalVisionProvider,
)
from app.config import settings
from app.utils.logger import logger


# ─── Registry — maps config values to provider classes ─────────────────
_PROVIDER_REGISTRY: Dict[str, type[BaseAIProvider]] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "claude": ClaudeProvider,
    "local_vision": LocalVisionProvider,
}

# Same quota/billing halt used in image_generation_manager.py: on a
# quota-exhaustion-class error, stop escalating through the rest of the
# provider chain instead of burning a call against every paid provider.
_QUOTA_EXHAUSTION_PATTERNS = (
    "quota exceeded",
    "resource exhausted",
    "resource_exhausted",
    "credit_balance_exhausted",
    "billing",
)


def _is_quota_exhaustion(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(p in lowered for p in _QUOTA_EXHAUSTION_PATTERNS)


class AIProviderManager:
    """Orchestrates AI analysis with automatic provider failover.

    The manager:
    - Selects the primary provider from ``settings.PRIMARY_AI_PROVIDER``
    - Falls back to ``settings.BACKUP_AI_PROVIDER`` then ``local_vision``
    - Performs ONE controlled attempt per provider (no hidden retries)
    - Logs every step, retry, and fallback event
    """

    # Zero retries: a single attempt per provider. Matches
    # ImageGenerationManager so neither manager can silently burn credits in
    # an uncontrolled retry loop. Fallback still occurs on transient errors.
    MAX_RETRIES_PER_PROVIDER: int = 0
    RETRY_DELAY_SECONDS: float = 2.0

    def __init__(self):
        self._providers: Dict[str, BaseAIProvider] = {}
        self._initialised = False

    def _init_providers(self) -> None:
        """Lazy-initialise provider instances."""
        if self._initialised:
            return
        for name, cls in _PROVIDER_REGISTRY.items():
            try:
                self._providers[name] = cls()
            except Exception as e:
                logger.warning(f"Failed to initialise provider '{name}': {e}")
        self._initialised = True

    def _get_provider(self, name: str) -> Optional[BaseAIProvider]:
        """Get a provider instance by name."""
        self._init_providers()
        return self._providers.get(name)

    def _get_provider_chain(self) -> List[str]:
        """Build the ordered provider chain from config."""
        chain: List[str] = []
        primary = settings.PRIMARY_AI_PROVIDER.strip().lower()
        backup = settings.BACKUP_AI_PROVIDER.strip().lower()
        fallback = settings.FALLBACK_AI_PROVIDER.strip().lower()

        if primary and primary in _PROVIDER_REGISTRY:
            chain.append(primary)
        if backup and backup in _PROVIDER_REGISTRY and backup not in chain:
            chain.append(backup)
        if fallback and fallback in _PROVIDER_REGISTRY and fallback not in chain:
            chain.append(fallback)

        # Always end with local_vision as the last resort
        if "local_vision" not in chain:
            chain.append("local_vision")

        return chain

    async def analyze(
        self,
        image_paths: List[str],
        context: Optional[Dict[str, Any]] = None,
        force_provider: Optional[str] = None,
    ) -> ProviderResult:
        """Analyse images with automatic failover across providers.

        Args:
            image_paths: Absolute paths to uploaded image files.
            context: Optional dict with ``request_id``, ``custom_prompt``, etc.
            force_provider: If set, uses ONLY this provider (no failover).

        Returns:
            A ``ProviderResult`` with the analysis data or the last error.
        """
        request_id = (context or {}).get("request_id", "unknown")

        if force_provider:
            provider_chain = [force_provider]
        else:
            provider_chain = self._get_provider_chain()

        logger.info(
            f"AIProviderManager: analysing {len(image_paths)} image(s) "
            f"request_id={request_id} chain={provider_chain}"
        )

        last_error: Optional[str] = None
        provider_used: Optional[str] = None
        fallback_used = False

        for idx, provider_name in enumerate(provider_chain):
            provider = self._get_provider(provider_name)
            if provider is None:
                logger.warning(f"Provider '{provider_name}' not available, skipping")
                continue

            if not provider.is_available:
                logger.info(f"Provider '{provider_name}' not configured, skipping")
                continue

            fallback_used = idx > 0
            if fallback_used:
                logger.info(
                    f"Falling back to provider '{provider_name}' "
                    f"request_id={request_id}"
                )

            # Retry loop for this provider
            for attempt in range(self.MAX_RETRIES_PER_PROVIDER + 1):
                try:
                    if attempt > 0:
                        logger.info(
                            f"Retry {attempt}/{self.MAX_RETRIES_PER_PROVIDER} "
                            f"for provider '{provider_name}' "
                            f"request_id={request_id}"
                        )
                        await asyncio.sleep(self.RETRY_DELAY_SECONDS)

                    result = await provider.analyze(image_paths, context)

                    if result.success:
                        result.provider_name = provider_name
                        result.fallback_used = fallback_used
                        logger.info(
                            f"AIProviderManager: success with provider '{provider_name}' "
                            f"fallback={fallback_used} "
                            f"time={result.processing_time:.2f}s "
                            f"request_id={request_id}"
                        )
                        return result

                    # Provider returned failure
                    last_error = result.error
                    logger.warning(
                        f"Provider '{provider_name}' attempt {attempt + 1} "
                        f"failed: {last_error} request_id={request_id}"
                    )
                    if _is_quota_exhaustion(last_error):
                        logger.error(
                            f"Provider '{provider_name}' quota/billing exhausted - "
                            f"halting chain instead of escalating request_id={request_id}"
                        )
                        return ProviderResult(
                            success=False,
                            error=last_error,
                            provider_name=provider_name,
                            fallback_used=fallback_used,
                        )

                except Exception as e:
                    last_error = str(e)
                    logger.warning(
                        f"Provider '{provider_name}' attempt {attempt + 1} "
                        f"exception: {last_error} request_id={request_id}"
                    )
                    if _is_quota_exhaustion(last_error):
                        logger.error(
                            f"Provider '{provider_name}' quota/billing exhausted - "
                            f"halting chain instead of escalating request_id={request_id}"
                        )
                        return ProviderResult(
                            success=False,
                            error=last_error,
                            provider_name=provider_name,
                            fallback_used=fallback_used,
                        )

            # All retries exhausted for this provider
            logger.warning(
                f"Provider '{provider_name}' exhausted after "
                f"{self.MAX_RETRIES_PER_PROVIDER + 1} attempt(s) "
                f"request_id={request_id}"
            )

        # All providers failed
        logger.error(
            f"AIProviderManager: all providers failed "
            f"request_id={request_id} last_error={last_error}"
        )

        return ProviderResult(
            success=False,
            error=last_error or "All AI providers failed",
            provider_name=provider_used or "none",
            fallback_used=True,
        )

    def get_available_providers(self) -> List[Dict[str, Any]]:
        """Return a list of available providers with status."""
        self._init_providers()
        result = []
        for name, provider in self._providers.items():
            result.append({
                "name": name,
                "version": provider.provider_version,
                "available": provider.is_available,
                "capabilities": provider.capabilities,
            })
        return result

    def get_provider_chain(self) -> List[str]:
        """Return the current provider chain order."""
        return self._get_provider_chain()
