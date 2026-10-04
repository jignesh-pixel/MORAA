"""Simple in-memory rate limiting middleware."""

import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.config import settings
from app.middleware.public_host_guard import PUBLIC_ALLOWED_PATHS, routed_path
from app.utils.logger import logger, safe_log

# Never counted: the health probe, and static customer images under /uploads
# (one page view is many hits; /uploads is local-only behind the host guard).
_EXEMPT_PATHS = frozenset({"/health"})
_EXEMPT_PREFIXES = ("/uploads/",)

# The provider webhooks get their own, larger per-IP bucket
# (WEBHOOK_RATE_LIMIT_REQUESTS): Meta and Razorpay call from a handful of IPs,
# so the dashboard's 100/min would 429 real customers' messages and payments,
# but an unlimited public endpoint invites floods and verify-token guessing.
_WEBHOOK_BUCKET = "webhook"
_DEFAULT_BUCKET = "api"


def is_rate_limit_exempt(path: str) -> bool:
    """True for paths the limiter never counts (path as the router sees it)."""
    normalized = path.rstrip("/") or "/"
    return normalized in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


def rate_limit_bucket(path: str) -> Optional[str]:
    """Bucket name for a path, or None when the path is exempt."""
    if is_rate_limit_exempt(path):
        return None
    if (path.rstrip("/") or "/") in PUBLIC_ALLOWED_PATHS:
        return _WEBHOOK_BUCKET
    return _DEFAULT_BUCKET


class RateLimitMiddleware:
    """Simple in-memory rate limiter based on client IP, one window per bucket (pure ASGI)."""

    def __init__(self, app):
        self.app = app
        self._request_counts: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        self.max_requests = settings.RATE_LIMIT_REQUESTS
        self.webhook_max_requests = settings.WEBHOOK_RATE_LIMIT_REQUESTS
        self.window_seconds = settings.RATE_LIMIT_WINDOW_SECONDS
        self._last_prune = 0.0

    async def __call__(self, scope, receive, send):
        """Check rate limit before processing request."""
        if scope["type"] != "http" or not settings.RATE_LIMIT_ENABLED:
            await self.app(scope, receive, send)
            return

        # scope["path"] is what the router serves; request.url.path is rebuilt
        # from the Host header and can be made to look like another path.
        bucket = rate_limit_bucket(scope.get("path") or "/")
        if bucket is None:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        key = (bucket, client_ip)
        limit = self.webhook_max_requests if bucket == _WEBHOOK_BUCKET else self.max_requests
        # Monotonic: a wall-clock step backwards must not stall the window.
        now = time.monotonic()

        # Drop timestamps outside the window, and whole idle keys, so the
        # table cannot grow without bound.
        self._prune(now)
        self._request_counts[key] = [
            ts for ts in self._request_counts[key]
            if now - ts < self.window_seconds
        ]

        if len(self._request_counts[key]) >= limit:
            logger.bind(category="api").warning(
                "Rate limit exceeded for {} on {} ({})", client_ip, safe_log(routed_path(Request(scope))), bucket
            )
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please try again later.",
                    "retry_after": self.window_seconds,
                },
                headers={"Retry-After": str(self.window_seconds)},
            )
            await response(scope, receive, send)
            return

        self._request_counts[key].append(now)
        await self.app(scope, receive, send)

    def _prune(self, now: float) -> None:
        """Forget keys whose newest request is outside the window (at most once per window)."""
        if now - self._last_prune < self.window_seconds:
            return
        self._last_prune = now
        stale = [
            key for key, stamps in self._request_counts.items()
            if not stamps or now - stamps[-1] >= self.window_seconds
        ]
        for key in stale:
            del self._request_counts[key]


def setup_rate_limit(app: FastAPI) -> None:
    """Add rate limiting middleware."""
    app.add_middleware(RateLimitMiddleware)
