"""Health and metrics routes.

* ``/health`` and ``/health/live``: the process is up (never touches the database, so a database outage does not
  make a load balancer kill a healthy process).
* ``/health/ready``: the process can do its job right now: database answers, schema is at the migrations' head,
  upload folder is writable. 503 when any check fails, so a deploy or load balancer only sends traffic to a
  process that is truly ready (OBS-2).
* ``/metrics``: Prometheus text format (OBS-3), including the shared daily spend, order counts and parked payments.

All of these sit behind the public-host guard like every other non-webhook route: they are reachable from the
local machine / reverse proxy only, never from the open internet.
"""

import os
import tempfile
import time
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.config import settings
from app.schemas.common import HealthResponse
from app.services import metrics

router = APIRouter(tags=["Health"])

_SCHEMA_CACHE_SECONDS = 30.0
_schema_cache: Dict[str, Any] = {"at": 0.0, "value": (True, "unchecked")}


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns the health status of the API service (same as /health/live).",
)
async def health_check():
    """Return service health status."""
    return HealthResponse(status="healthy")


@router.get("/health/live", response_model=HealthResponse, summary="Liveness: the process is running")
async def health_live():
    return HealthResponse(status="healthy")


def _check_database() -> Tuple[bool, str]:
    from app.database import engine

    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, f"ok ({(time.perf_counter() - started) * 1000:.0f} ms)"
    except Exception as e:  # noqa: BLE001
        return False, f"database unreachable: {type(e).__name__}"


def _check_schema() -> Tuple[bool, str]:
    """Is the database at the migrations' head? Cached for a short time: it cannot change while we run."""
    now = time.monotonic()
    if now - _schema_cache["at"] < _SCHEMA_CACHE_SECONDS:
        return _schema_cache["value"]
    try:
        from app.database import get_schema_status

        schema = get_schema_status()
        value = (schema.up_to_date, "ok" if schema.up_to_date else f"behind: database {schema.current}, code {schema.head}")
    except Exception as e:  # noqa: BLE001
        value = (False, f"schema check failed: {type(e).__name__}")
    _schema_cache.update(at=now, value=value)
    return value


def _check_disk() -> Tuple[bool, str]:
    try:
        settings.UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=str(settings.UPLOAD_PATH), prefix=".ready_")
        os.close(fd)
        os.remove(path)
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"upload folder not writable: {type(e).__name__}"


@router.get("/health/ready", summary="Readiness: database, schema and storage all work")
def health_ready(response: Response):
    """Plain function (FastAPI runs it on a worker thread): the checks are blocking database / disk calls."""
    checks = {"database": _check_database(), "schema": _check_schema(), "disk": _check_disk()}
    # With the database down the schema check cannot say anything useful: report it as skipped, not as a second failure.
    ready = all(ok for ok, _ in checks.values())
    metrics.registry.set_gauge("moraa_db_up", 1 if checks["database"][0] else 0)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "checks": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()},
    }


@router.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics")
def metrics_endpoint():
    """Plain function: refreshing the database gauges is a blocking call (cached for a few seconds)."""
    return PlainTextResponse(metrics.render_all(), media_type="text/plain; version=0.0.4")
