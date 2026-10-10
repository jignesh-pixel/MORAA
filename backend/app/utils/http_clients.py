"""Shared outbound HTTP clients for Meta Graph, WhatsApp Pay, Razorpay and the GST vendor (PERF-6).

Every call used to build its own ``httpx.AsyncClient``. That cost twice per call: about 0.2 s of CPU to load the
CA bundle into a new TLS context (on the event loop, so every other customer waited), plus a new TCP + TLS handshake
to the same host. Now:

* ONE TLS context per process, built off the event loop on first use (``ssl.SSLContext`` is safe to share).
* ONE connection pool (``httpx.AsyncHTTPTransport``) per event loop, kept alive between calls for
  ``HTTP_KEEPALIVE_SECONDS`` (httpx's default of 5 s drops the connection between most chat messages).
* ONE small client per (timeout, redirects, auth) setting on top of that pool, so every call keeps exactly the
  timeout and auth it had, while all of them share the warm connections.

Behaviour kept from the per-call clients: no cookie ever persists between calls (a no-store cookie jar), and the
client is built through ``httpx.AsyncClient`` at call time and entered once, like the ``async with`` it replaces.

Lifecycle: ``warm_up()`` at application startup builds the TLS context before the first customer;
``close_shared_clients()`` at shutdown closes the running loop's clients and pool. A loop that ends without it (a
short ``asyncio.run``) drops its entry with the loop (weak reference). A client that was closed is rebuilt.
"""

import asyncio
import ssl
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, AsyncIterator, Dict, Optional, Tuple, Union

import httpx

from app.config import settings
from app.utils.logger import logger

TimeoutTypes = Union[float, int, httpx.Timeout]
_ClientKey = Tuple[Tuple[Optional[float], ...], bool, Optional[Tuple[str, str]]]

_SSL_CONTEXT: Optional[ssl.SSLContext] = None
_SSL_LOCK = threading.Lock()


@dataclass
class _LoopState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    transport: Optional[httpx.AsyncHTTPTransport] = None
    # key -> (the client as built, the client as entered): the built one is closed, the entered one is handed out
    # (the same object for a real httpx client), exactly like `async with httpx.AsyncClient(...) as client`.
    clients: Dict[_ClientKey, Tuple[Any, httpx.AsyncClient]] = field(default_factory=dict)


_STATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState]" = weakref.WeakKeyDictionary()


def _build_ssl_context() -> ssl.SSLContext:
    """The process's TLS context (CA bundle loaded once). Blocking: call it off the event loop."""
    global _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = httpx.create_ssl_context()
        return _SSL_CONTEXT


async def _ssl_context() -> ssl.SSLContext:
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    from app.utils.executors import run_io

    return await run_io(_build_ssl_context)


def _limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=max(int(settings.HTTP_MAX_CONNECTIONS), 1),
        max_keepalive_connections=max(int(settings.HTTP_MAX_KEEPALIVE_CONNECTIONS), 0),
        keepalive_expiry=max(float(settings.HTTP_KEEPALIVE_SECONDS), 0.0),
    )


def _no_store_cookies() -> CookieJar:
    """A cookie jar that never stores anything: a shared client must not carry one call's cookies into the next.

    Passed as the jar itself: httpx copies an ``httpx.Cookies`` into a fresh jar with the default policy."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def _key(timeout: TimeoutTypes, follow_redirects: bool, basic_auth: Optional[Tuple[str, str]]) -> _ClientKey:
    t = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(float(timeout))
    return (t.connect, t.read, t.write, t.pool), bool(follow_redirects), tuple(basic_auth) if basic_auth else None


def _is_closed(client: object) -> bool:
    return getattr(client, "is_closed", False) is True


async def shared_client(
    *,
    timeout: TimeoutTypes,
    follow_redirects: bool = False,
    basic_auth: Optional[Tuple[str, str]] = None,
) -> httpx.AsyncClient:
    """The running loop's shared client for this timeout / redirect / Basic-auth setting. Never close it yourself."""
    loop = asyncio.get_running_loop()
    state = _STATES.get(loop)
    if state is None:
        state = _STATES[loop] = _LoopState()
    key = _key(timeout, follow_redirects, basic_auth)
    cached = state.clients.get(key)
    if cached is not None and not _is_closed(cached[0]):
        return cached[1]
    async with state.lock:
        cached = state.clients.get(key)
        if cached is not None and not _is_closed(cached[0]):
            return cached[1]
        if cached is not None:
            # Closing one client closed the pool every client of this loop shares: start again from a new pool.
            logger.warning("A shared HTTP client was closed outside shutdown; rebuilding the connection pool")
            state.clients.clear()
            state.transport = None
        if state.transport is None:
            state.transport = httpx.AsyncHTTPTransport(verify=await _ssl_context(), limits=_limits())
        kwargs = {"timeout": timeout, "transport": state.transport, "cookies": _no_store_cookies()}
        if follow_redirects:
            kwargs["follow_redirects"] = True
        if basic_auth:
            kwargs["auth"] = httpx.BasicAuth(*basic_auth)
        # Looked up at call time (tests patch httpx.AsyncClient) and entered once, as the replaced `async with` did.
        built = httpx.AsyncClient(**kwargs)
        entered = await built.__aenter__()
        state.clients[key] = (built, entered)
        return entered


@asynccontextmanager
async def shared_http(
    *,
    timeout: TimeoutTypes,
    follow_redirects: bool = False,
    basic_auth: Optional[Tuple[str, str]] = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """``async with shared_http(timeout=30.0) as client:`` -- the shared client, NOT closed on exit."""
    yield await shared_client(timeout=timeout, follow_redirects=follow_redirects, basic_auth=basic_auth)


async def warm_up() -> None:
    """Build the TLS context and the default Meta client before the first customer arrives. Never raises."""
    try:
        await shared_client(timeout=30.0)
    except Exception as e:  # noqa: BLE001 -- the first call builds it instead
        logger.warning(f"Shared HTTP client warm-up skipped: {type(e).__name__}: {e}")


async def close_shared_clients() -> None:
    """Close the running loop's shared clients and connection pool (application shutdown). Never raises."""
    try:
        state = _STATES.pop(asyncio.get_running_loop(), None)
    except RuntimeError:
        return
    if state is None:
        return
    for built, _entered in list(state.clients.values()):
        try:
            await built.__aexit__(None, None, None)
        except Exception as e:  # noqa: BLE001 -- shutdown must never fail on this
            logger.warning(f"Shared HTTP client close failed: {type(e).__name__}: {e}")
    state.clients.clear()
    if state.transport is not None:
        try:
            await state.transport.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Shared HTTP pool close failed: {type(e).__name__}: {e}")
