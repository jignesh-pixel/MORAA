"""Request logging middleware for API calls."""

import time
import uuid

from fastapi import FastAPI

from app.services import metrics
from app.utils.logger import logger, safe_log, set_request_id


class RequestLoggingMiddleware:
    """Log all incoming requests and their response times.

    Pure ASGI (no BaseHTTPMiddleware): it adds no extra task or buffering per request, so it is cheap for static
    files and cannot interfere with streaming responses or background tasks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())[:8]
        scope.setdefault("state", {})["request_id"] = request_id
        set_request_id(request_id)      # every log line this request (and its background jobs) causes carries it

        start_time = time.time()
        method = scope.get("method", "")
        # scope["path"] is the routed path (request.url is rebuilt from the Host header). Values are passed as
        # arguments, never formatted into the template, so a path containing "{x}" cannot break logging.
        path = safe_log(scope.get("path", ""))
        api_log = logger.bind(category="api")
        api_log.info("→ [{}] {} {}", request_id, method, path)

        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", request_id.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            metrics.record_http(method, 500, duration_ms / 1000)
            api_log.error("✗ [{}] {} {} → ERROR ({:.1f}ms): {}", request_id, method, path, duration_ms, str(e))
            raise
        duration_ms = (time.time() - start_time) * 1000
        metrics.record_http(method, status_code, duration_ms / 1000)
        api_log.info("← [{}] {} {} → {} ({:.1f}ms)", request_id, method, path, status_code, duration_ms)


def setup_logging_middleware(app: FastAPI) -> None:
    """Add request logging middleware."""
    app.add_middleware(RequestLoggingMiddleware)
