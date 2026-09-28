"""Public host guard.

When the API is reached through a public host (the ngrok tunnel the Meta and
Razorpay webhooks need), only those two webhook endpoints are served. Every
other route stays reachable from localhost only. Requests from localhost are
untouched.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.utils.logger import logger

PUBLIC_ALLOWED_PATHS = frozenset({
    "/api/meta/webhook",
    "/api/payments/razorpay/webhook",
})

# Headers a tunnel/reverse proxy adds; a direct local request never has them.
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded")


def _hostname(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:  # name:port
        return host.split(":", 1)[0]
    return host


def is_public_request(request: Request) -> bool:
    local_hosts = {h.strip().lower() for h in settings.LOCAL_API_HOSTS.split(",") if h.strip()}
    if _hostname(request.headers.get("host", "")) not in local_hosts:
        return True
    return any(request.headers.get(h) for h in _PROXY_HEADERS)


class PublicHostGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if settings.PUBLIC_HOST_GUARD_ENABLED and is_public_request(request):
            path = request.url.path.rstrip("/") or "/"
            if path not in PUBLIC_ALLOWED_PATHS:
                logger.warning(
                    f"Public host guard: blocked {request.method} {request.url.path} "
                    f"host={request.headers.get('host', '')}",
                    extra={"category": "api"},
                )
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return await call_next(request)


def setup_public_host_guard(app: FastAPI) -> None:
    """Add last so it runs first (outermost middleware)."""
    app.add_middleware(PublicHostGuardMiddleware)
