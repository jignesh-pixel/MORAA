"""Database engine, session factory, and declarative base."""

from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


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
    """Create all database tables.

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
        query = text("SELECT 1 FROM pg_indexes WHERE indexname = :name")
    elif dialect == "sqlite":
        query = text("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name")
    else:
        return None
    with engine.connect() as conn:
        return conn.execute(query, {"name": MONEY_ONCE_INDEX}).first() is not None
