"""Test database factory: SQLite by default, real PostgreSQL on request.

Default (no environment): every test gets exactly the engine it always had
(in-memory SQLite, one shared connection; or a temp file when the test needs
several connections). Nothing changes for a plain ``pytest`` run.

PostgreSQL mode (``MORAA_TEST_DB=postgres``): every test that calls
``make_engine()`` gets its own empty schema in a real PostgreSQL, so SQLite's
missing row locks, lax typing and foreign-key behaviour cannot hide a bug.

  * ``TEST_DATABASE_URL`` set  -> use that server (CI service container).
  * otherwise                  -> start a throw-away local server with the
                                  ``pgserver`` dev dependency (data dir in a
                                  temp folder, deleted at exit).

The database is only ever reached through this module. ``assert_safe_test_url``
refuses any non-loopback host unless ``MORAA_ALLOW_REMOTE_TEST_DB=1``, so a
production URL pasted into TEST_DATABASE_URL by mistake cannot be used.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
import uuid
from typing import Optional

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FORBIDDEN_HOST_PARTS = ("supabase.co", "supabase.com", "pooler.supabase")

_admin_engine: Optional[Engine] = None
_admin_url: Optional[str] = None
_local_server = None
_local_dir: Optional[str] = None


def postgres_mode() -> bool:
    """True when the whole suite should run its databases on PostgreSQL."""
    return os.environ.get("MORAA_TEST_DB", "sqlite").strip().lower() == "postgres"


# libpq lets query parameters override or add connection targets, so a URL whose authority says
# "localhost" can still connect elsewhere (postgresql://u:p@localhost/x?host=db.prod.example).
_FORBIDDEN_QUERY_KEYS = ("host", "hostaddr", "service", "passfile", "sslrootcert")


def assert_safe_test_url(url: str) -> None:
    """Refuse a database URL that could be a real deployment."""
    parsed = make_url(url)
    query_keys = {key.lower() for key in parsed.query}
    bad_keys = sorted(query_keys & set(_FORBIDDEN_QUERY_KEYS))
    if bad_keys:
        raise RuntimeError(f"Refusing test database URL with connection-target override(s): {', '.join(bad_keys)}.")
    hosts = [h.strip().strip("[]").lower().rstrip(".") for h in (parsed.host or "").split(",") if h.strip()]
    if not hosts:
        raise RuntimeError("Refusing test database URL without an explicit local host.")
    for host in hosts:
        if any(part in host for part in _FORBIDDEN_HOST_PARTS):
            raise RuntimeError(f"Refusing to run tests against {host}: looks like a hosted production database.")
        if host not in _LOOPBACK_HOSTS and os.environ.get("MORAA_ALLOW_REMOTE_TEST_DB") != "1":
            raise RuntimeError(
                f"Refusing to run tests against non-local host {host!r}. "
                "Use a local/throw-away PostgreSQL, or set MORAA_ALLOW_REMOTE_TEST_DB=1 if you are sure."
            )


def _stop_local_server() -> None:
    global _local_server, _local_dir
    try:
        if _local_server is not None:
            _local_server.cleanup()
    finally:
        _local_server = None
        if _local_dir:
            shutil.rmtree(_local_dir, ignore_errors=True)
            _local_dir = None


def _start_local_server() -> str:
    """Start a throw-away PostgreSQL with pgserver and return a URL for database ``moraa_test``."""
    global _local_server, _local_dir
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "PostgreSQL tests need TEST_DATABASE_URL or the 'pgserver' dev dependency "
            "(pip install -r requirements-dev.txt)."
        ) from exc
    import psycopg2

    _local_dir = tempfile.mkdtemp(prefix="moraa_pg_")
    _local_server = pgserver.get_server(os.path.join(_local_dir, "data"), cleanup_mode="stop")
    atexit.register(_stop_local_server)
    base = _local_server.get_uri()
    conn = psycopg2.connect(base)
    try:
        conn.autocommit = True
        conn.cursor().execute("CREATE DATABASE moraa_test")
    finally:
        conn.close()
    return base.rsplit("/", 1)[0] + "/moraa_test"


def postgres_url() -> str:
    """URL of the shared test PostgreSQL (started on first use)."""
    global _admin_url
    if _admin_url is None:
        url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if url:
            assert_safe_test_url(url)
        else:
            url = _start_local_server()
        _admin_url = url
    return _admin_url


def _admin() -> Engine:
    global _admin_engine
    if _admin_engine is None:
        _admin_engine = create_engine(postgres_url(), isolation_level="AUTOCOMMIT", poolclass=StaticPool)
    return _admin_engine


def _postgres_engine() -> Engine:
    """A fresh, empty schema in the test PostgreSQL; dropped when the engine is disposed."""
    schema = "t_" + uuid.uuid4().hex[:12]
    with _admin().connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        postgres_url(),
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=10,
        max_overflow=20,
        pool_timeout=15,
    )

    @event.listens_for(engine, "engine_disposed")
    def _drop_schema(_engine: Engine) -> None:
        try:
            with _admin().connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception:  # pragma: no cover - best effort cleanup of a throw-away schema
            pass

    engine.moraa_schema = schema  # type: ignore[attr-defined]
    return engine


def _sqlite_memory_engine() -> Engine:
    return create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _sqlite_file_engine() -> Engine:
    """Temp-file SQLite for tests that need several real connections; file removed on dispose."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "engine_disposed")
    def _remove_file(_engine: Engine) -> None:
        try:
            os.remove(path)
        except OSError:  # pragma: no cover
            pass

    return engine


def make_engine(*, concurrent: bool = False) -> Engine:
    """Empty database engine for one test.

    ``concurrent=True`` means the test opens several sessions at once (threads),
    so SQLite needs a real file instead of one shared in-memory connection.
    Tables are NOT created; callers run ``Base.metadata.create_all`` as before.
    """
    if postgres_mode():
        return _postgres_engine()
    return _sqlite_file_engine() if concurrent else _sqlite_memory_engine()


def postgres_engine() -> Engine:
    """PostgreSQL engine regardless of mode, for tests about PostgreSQL itself."""
    return _postgres_engine()


def sqlite_engine() -> Engine:
    """In-memory SQLite engine regardless of mode, for tests about SQLite-specific startup paths."""
    return _sqlite_memory_engine()


requires_postgres = unittest.skipUnless(
    postgres_mode(), "PostgreSQL-only test: run with MORAA_TEST_DB=postgres (see tests/db_support.py)"
)
"""Class/function decorator for tests that are only meaningful on PostgreSQL."""

postgres = pytest.mark.postgres
