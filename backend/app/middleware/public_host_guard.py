"""Public host guard.

When the API is reached through a public host (the ngrok tunnel the Meta and
Razorpay webhooks need), only those two webhook endpoints are served. Every
other route stays reachable from this machine only. Requests from this
machine are untouched.

"Local" needs all three: the real socket peer is loopback (or listed in
LOCAL_PEER_ADDRESSES), the Host header is a local name, and no proxy header
is present. The Host header alone is attacker-controlled, so it never makes
a request local by itself. Behind uvicorn's default proxy handling, a tunnel
connecting from 127.0.0.1 has the peer rewritten to the X-Forwarded-For
client, so tunnelled traffic stays public.
"""

import ipaddress
import re
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.config import settings
from app.utils.logger import logger, safe_log

PUBLIC_ALLOWED_PATHS = frozenset({
    "/api/meta/webhook",
    "/api/payments/razorpay/webhook",
})

# Headers a tunnel/reverse proxy adds; a direct local request never has them.
_PROXY_HEADERS = frozenset({b"x-forwarded-for", b"x-forwarded-host", b"forwarded", b"x-real-ip"})

# A Host header is a name or IP literal with an optional port, nothing else.
# Starlette builds request.url from the Host header, so "x/api/meta/webhook#"
# used to make request.url.path look like a webhook while the router served
# a different route. Anything else is rejected outright.
_VALID_HOST = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(:\d{1,5})?$")


# Internet scanners hit the public URL constantly. Each blocked request must
# not write several log lines (disk-fill amplification): at most this many
# detailed lines per minute, then a single summary when the window rolls over.
_BLOCKED_LOG_PER_MINUTE = 20
_blocked_window_start = 0.0
_blocked_logged = 0
_blocked_suppressed = 0


def _log_blocked(method: str, path: object, host: str) -> None:
    global _blocked_window_start, _blocked_logged, _blocked_suppressed
    now = time.monotonic()
    if now - _blocked_window_start >= 60:
        if _blocked_suppressed:
            logger.bind(category="api").warning(
                "Public host guard: {} more blocked requests in the last minute not logged",
                _blocked_suppressed,
            )
        _blocked_window_start, _blocked_logged, _blocked_suppressed = now, 0, 0
    if _blocked_logged >= _BLOCKED_LOG_PER_MINUTE:
        _blocked_suppressed += 1
        return
    _blocked_logged += 1
    logger.bind(category="api").warning(
        "Public host guard: blocked {} {} host={!r}", method, safe_log(path), safe_log(host, 100)
    )


def routed_path(request: Request) -> str:
    """The path the router matches (scope["path"]), never derived from Host.

    Normalised without a trailing slash so it compares with PUBLIC_ALLOWED_PATHS.
    """
    return (request.scope.get("path") or "/").rstrip("/") or "/"


def _hostname(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:  # name:port
        return host.split(":", 1)[0]
    return host


def _is_local_peer(request: Request) -> bool:
    """True when the TCP peer is this machine (loopback) or an allowed peer."""
    peer = (request.client.host if request.client else "") or ""
    peer = peer.strip().lower()
    if not peer:
        return False
    allowed = {p.strip().lower() for p in settings.LOCAL_PEER_ADDRESSES.split(",") if p.strip()}
    if peer in allowed:
        return True
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


def _has_proxy_header(request: Request) -> bool:
    """True if any proxy header is present at all, even empty or duplicated."""
    return any(name.lower() in _PROXY_HEADERS for name, _ in request.scope.get("headers", []))


def is_public_request(request: Request) -> bool:
    if not _is_local_peer(request):
        return True
    local_hosts = {h.strip().lower() for h in settings.LOCAL_API_HOSTS.split(",") if h.strip()}
    if _hostname(request.headers.get("host", "")) not in local_hosts:
        return True
    return _has_proxy_header(request)


def _content_length(request: Request) -> int:
    try:
        return int(request.headers.get("content-length") or 0)
    except ValueError:
        return -1


class PublicHostGuardMiddleware:
    """Pure ASGI. The app has no websocket routes: a websocket that would be public is closed, so one can never
    slip past the guard."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        host = request.headers.get("host", "")
        method = scope.get("method", "WS")
        if scope["type"] == "http" and host and not _VALID_HOST.match(host):
            _log_blocked(method, scope.get("path"), host)
            await JSONResponse(status_code=400, content={"detail": "Invalid Host header"})(scope, receive, send)
            return

        if settings.PUBLIC_HOST_GUARD_ENABLED and is_public_request(request):
            path = routed_path(request)
            if scope["type"] == "websocket" or path not in PUBLIC_ALLOWED_PATHS:
                _log_blocked(method, scope.get("path"), host)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await JSONResponse(status_code=404, content={"detail": "Not Found"})(scope, receive, send)
                return
            # Webhook payloads are small JSON; refuse oversized or malformed
            # bodies before the route reads them into memory.
            length = _content_length(request)
            if length < 0 or length > settings.MAX_WEBHOOK_BODY_BYTES:
                logger.bind(category="api").warning(
                    "Public host guard: refused webhook body content-length={}",
                    request.headers.get("content-length"),
                )
                await JSONResponse(status_code=413, content={"detail": "Payload too large"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


class WebhookBodyLimitMiddleware:
    """Cap request bodies on the public webhook paths by counting real bytes.

    The guard's Content-Length check cannot see a chunked upload (no
    Content-Length), and the routes read the whole body into memory before
    verifying the signature. This pure-ASGI wrapper counts bytes as they
    arrive. Past the cap it tells the route the client disconnected (so no
    more is buffered) and replaces whatever the route answers with a 413 --
    no exception is used, because the routes catch broad exceptions around
    body parsing. Honest Meta and Razorpay payloads (a few KB) are unaffected.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") not in ("POST", "PUT", "PATCH")
            or ((scope.get("path") or "/").rstrip("/") or "/") not in PUBLIC_ALLOWED_PATHS
        ):
            await self.app(scope, receive, send)
            return

        limit = settings.MAX_WEBHOOK_BODY_BYTES
        received = 0
        exceeded = False
        replaced = False  # the 413 has been sent

        async def send_413():
            nonlocal replaced
            if replaced:
                return
            replaced = True
            body = b'{"detail":"Payload too large"}'
            await send({
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii"))],
            })
            await send({"type": "http.response.body", "body": body})

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    logger.bind(category="api").warning(
                        "Public host guard: webhook body exceeded {} bytes while streaming", limit
                    )
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            if exceeded:
                # Drop the route's own answer; the client gets the 413 instead.
                await send_413()
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await send_413()


def setup_public_host_guard(app: FastAPI) -> None:
    """Install the body limiter, then the guard last so the guard runs first (outermost)."""
    app.add_middleware(WebhookBodyLimitMiddleware)
    app.add_middleware(PublicHostGuardMiddleware)
