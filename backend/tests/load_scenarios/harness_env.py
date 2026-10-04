"""Zero-cost, zero-secret bootstrap for the load-simulation harness.

Importing this module (BEFORE any ``app.*`` import) guarantees:

* the process working directory is a fresh temp dir, so pydantic's relative
  ``env_file=".env"`` can never find ``backend/.env`` and no ``logs/`` dir is
  created inside the repo;
* every key/token/secret setting is a dummy string;
* DATABASE_URL points at a throw-away SQLite file in the temp dir;
* uploads go to the temp dir;
* outbound network is blocked (any non-loopback DNS lookup or HTTP request
  raises and is counted in ``NETWORK_BLOCKED``).
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import List

BACKEND_DIR = Path(__file__).resolve().parents[2]
WORKDIR = Path(tempfile.mkdtemp(prefix="moraa_load_"))
DB_FILE = WORKDIR / "load_sim.db"
NETWORK_BLOCKED: List[str] = []

_DUMMY = "DUMMY-LOAD-SIM-NOT-A-REAL-KEY"
_SECRET_ENV = [
    "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "META_VERIFY_TOKEN",
    "META_WHATSAPP_TOKEN", "META_APP_SECRET", "OPS_SECRET", "ERPNEXT_API_KEY",
    "ERPNEXT_API_SECRET", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET", "SECRET_KEY",
]


def _prepare_environment() -> None:
    os.chdir(WORKDIR)
    # config.py finds backend/.env by absolute path; "" disables it entirely.
    os.environ["MORAA_ENV_FILE"] = ""
    os.environ["LOG_DIR"] = str(WORKDIR / "logs")
    for name in _SECRET_ENV:
        os.environ[name] = _DUMMY
    # META_APP_SECRET empty + ALLOW_UNSIGNED_WEBHOOKS lets the webhook accept
    # unsigned test payloads (DEBUG no longer relaxes signature checks).
    os.environ["ALLOW_UNSIGNED_WEBHOOKS"] = "true"
    os.environ["META_APP_SECRET"] = ""
    os.environ["OPS_SECRET"] = ""
    os.environ["OPS_INBOUND_URL"] = ""
    os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
    os.environ["UPLOAD_DIR"] = str(WORKDIR / "uploads")
    os.environ["REPORT_DIR"] = str(WORKDIR / "reports")
    os.environ["DEBUG"] = "true"
    os.environ["DRY_RUN_IMAGE_MODE"] = "false"
    os.environ["GENERATION_ENABLED"] = "true"
    os.environ["ERPNEXT_INVOICE_ENABLED"] = "false"
    os.environ["GST_VERIFICATION_ENABLED"] = "false"
    os.environ["IMAGE_PREVALIDATION_ENABLED"] = "false"
    os.environ["RATE_LIMIT_ENABLED"] = "true"
    os.environ["WHATSAPP_PAY_ENABLED"] = "false"
    os.environ["LOG_LEVEL"] = "WARNING"
    for extra in (str(BACKEND_DIR),):
        if extra not in sys.path:
            sys.path.insert(0, extra)


def _install_network_guard() -> None:
    real_getaddrinfo = socket.getaddrinfo
    local_hosts = {"localhost", "127.0.0.1", "::1", "", None, "testserver", "testclient"}

    def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host in local_hosts:
            return real_getaddrinfo(host, *args, **kwargs)
        NETWORK_BLOCKED.append(f"dns:{host}")
        raise OSError(f"load-sim network guard: DNS lookup for {host!r} blocked")

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]

    real_connect = socket.socket.connect

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        NETWORK_BLOCKED.append(f"connect:{host}")
        raise OSError(f"load-sim network guard: connect to {host!r} blocked")

    socket.socket.connect = guarded_connect  # type: ignore[assignment]

    import httpx

    allowed = {"testserver", "localhost", "127.0.0.1", "::1"}
    real_send = httpx.Client.send
    real_async_send = httpx.AsyncClient.send

    def _blocked_send(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        if request.url.host in allowed:
            return real_send(self, request, *args, **kwargs)
        NETWORK_BLOCKED.append(f"httpx:{request.url}")
        raise httpx.ConnectError("load-sim network guard: outbound HTTP blocked")

    async def _blocked_async_send(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        if request.url.host in allowed:
            return await real_async_send(self, request, *args, **kwargs)
        NETWORK_BLOCKED.append(f"httpx:{request.url}")
        raise httpx.ConnectError("load-sim network guard: outbound HTTP blocked")

    httpx.Client.send = _blocked_send  # type: ignore[assignment]
    httpx.AsyncClient.send = _blocked_async_send  # type: ignore[assignment]


def _patch_pool() -> None:
    """LOADSIM_POOL=big lifts the SQLAlchemy pool (default 5+10) so scenarios other than the
    pool-exhaustion probe are not confounded; LOADSIM_POOL=default keeps app defaults and only
    shortens pool_timeout (LOADSIM_POOL_TIMEOUT, default app value is 30 s)."""
    import sqlalchemy

    mode = os.environ.get("LOADSIM_POOL", "big")
    original = sqlalchemy.create_engine

    def patched(url, **kwargs):  # type: ignore[no-untyped-def]
        if mode == "big":
            kwargs.update(pool_size=400, max_overflow=400)
        elif os.environ.get("LOADSIM_POOL_TIMEOUT"):
            kwargs["pool_timeout"] = float(os.environ["LOADSIM_POOL_TIMEOUT"])
        return original(url, **kwargs)

    sqlalchemy.create_engine = patched  # type: ignore[assignment]


def bootstrap() -> None:
    _prepare_environment()
    _patch_pool()
    _install_network_guard()
    from app.config import settings

    assert str(DB_FILE.as_posix()) in settings.DATABASE_URL, "DB not isolated"
    assert settings.GEMINI_API_KEY == _DUMMY, "real GEMINI key leaked into settings"
    assert settings.OPENAI_API_KEY == _DUMMY, "real OPENAI key leaked into settings"
    assert settings.META_WHATSAPP_TOKEN == _DUMMY, "real Meta token leaked into settings"
    assert settings.RAZORPAY_KEY_SECRET == _DUMMY, "real Razorpay key leaked into settings"
    assert settings.ERPNEXT_API_KEY == _DUMMY, "real ERPNext key leaked into settings"
    import logging

    from app.database import engine, init_db

    engine.echo = False
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    init_db()


bootstrap()
