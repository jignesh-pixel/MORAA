"""Database engine, session factory, and declarative base."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, settings
from app.utils.logger import logger


# Ensure data directory exists for SQLite
if settings.IS_SQLITE:
    db_path = Path(settings.DATABASE_URL.replace("sqlite:///", ""))
    db_path.parent.mkdir(parents=True, exist_ok=True)

# Create engine
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False} if settings.IS_SQLITE else {},
    pool_pre_ping=True,
    # Exception messages otherwise embed every bound value (customer names,
    # phone numbers, GSTINs, addresses) and those messages are logged.
    hide_parameters=True,
)

# Enable WAL mode for SQLite for better concurrency
if settings.IS_SQLITE:

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Declarative base for all database models."""


def get_db() -> Session:
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables straight from the models (``Base.metadata.create_all``).

    FOR TESTS AND ONE-OFF TOOLS ONLY. The application never calls this at
    startup: the schema is owned by Alembic (``ensure_schema_ready`` below).
    ``create_all`` never alters an existing table, so using it next to the
    migrations hides drift.

    Imports every model to register it with ``Base.metadata`` before
    calling ``create_all``.  New models must be added to the import
    block below AND to ``app.models.__init__``.
    """
    from app.models import (  # noqa: F401 - Import models to register them
        Analysis,
        AuditLog,
        Customer,
        HistoryEntry,
        Image,
        OnboardingSession,
        ProcessingLog,
        Report,
        RetryHistory,
        ToolExecutionLog,
        User,
        VersionHistory,
        WhatsAppIngestion,
        WhatsAppPaymentOrder,
    )

    Base.metadata.create_all(bind=engine)


# Partial unique index that makes every payment credit and refund happen at
# most once (app/models/audit_log.py, migration 0003). create_all never adds
# an index to a table that already exists, so it is checked explicitly.
MONEY_ONCE_INDEX = "uq_audit_logs_money_once"


def money_once_index_present() -> Optional[bool]:
    """True/False when the index can be checked, None for other dialects."""
    dialect = engine.dialect.name
    if dialect == "postgresql":
        query = text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :name AND schemaname = current_schema()"
        )
    elif dialect == "sqlite":
        query = text("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name")
    else:
        return None
    with engine.connect() as conn:
        return conn.execute(query, {"name": MONEY_ONCE_INDEX}).first() is not None


# --- Schema ownership: Alembic only -----------------------------------------


@dataclass(frozen=True)
class SchemaStatus:
    """Where the database is relative to the migrations shipped with this code."""

    current: Optional[str]      # revision recorded in the database (None: never migrated)
    head: str                   # newest revision in backend/alembic/versions
    has_app_tables: bool        # any application table exists (e.g. created by an older create_all)

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


def alembic_config():
    """Alembic ``Config`` with absolute paths, so it works from any working directory."""
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    return cfg


def get_schema_status(target_engine: Optional[Engine] = None) -> SchemaStatus:
    """Compare the database's migration revision with the code's single head."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    eng = target_engine if target_engine is not None else engine
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Alembic has {len(heads)} migration heads ({', '.join(heads)}); merge them.")
    tables = set(inspect(eng).get_table_names())
    current: Optional[str] = None
    if "alembic_version" in tables:
        with eng.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    return SchemaStatus(
        current=current,
        head=heads[0],
        has_app_tables=bool(tables - {"alembic_version"}),
    )


def upgrade_to_head(target_engine: Optional[Engine] = None) -> None:
    """Run every pending migration (``alembic upgrade head``) on this process's connection."""
    from alembic import command

    eng = target_engine if target_engine is not None else engine
    cfg = alembic_config()
    with eng.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def ensure_schema_ready(target_engine: Optional[Engine] = None) -> SchemaStatus:
    """Startup guard: the database must be at the migrations' head.

    * production: refuse to start when behind (or never migrated). A behind
      database fails later in confusing ways (missing columns), and some code
      paths turn those errors into "unknown customer".
    * development on SQLite: run the migrations automatically, so a fresh
      checkout still starts with no manual step (the migrations are written to
      be safe on a database that an older ``create_all`` already populated).
    * development on another database: log a loud error and carry on.

    ``create_all`` is never used here.
    """
    eng = target_engine if target_engine is not None else engine
    status = get_schema_status(eng)
    if status.up_to_date:
        return status

    unmigrated = " (application tables exist but were never migrated)" if status.current is None and status.has_app_tables else ""
    state = f"database revision {status.current or 'none'}, code expects {status.head}{unmigrated}"
    if settings.IS_PRODUCTION:
        raise RuntimeError(f"Database schema is not up to date: {state}. Run `alembic upgrade head` and restart.")

    if eng.dialect.name == "sqlite":
        logger.warning(f"Schema behind head ({state}); running migrations on the local SQLite database.")
        upgrade_to_head(eng)
        status = get_schema_status(eng)
        if not status.up_to_date:
            raise RuntimeError(f"Migrations finished but the database is still at {status.current}.")
        return status

    logger.error(f"Schema behind head ({state}). Run `alembic upgrade head`; continuing in development.")
    return status
