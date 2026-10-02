"""Logging configuration using loguru for structured logs."""

import sys

from loguru import logger

from app.config import settings


def safe_log(value: object, limit: int = 300) -> str:
    """Make client-controlled text safe to write into a log line.

    A request path or header can carry newlines or other control characters
    ("/x%0a2026-10-01 ... | INFO | Payment credited"), which would forge extra
    log lines. Control characters are escaped and the length is capped.
    """
    text = str(value)
    cleaned = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text)
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def mask_phone(value: object) -> str:
    """Mask a phone number / WhatsApp id for logs: '919812345678' -> '91******5678'.

    Keeps enough to correlate a customer's log lines without writing the full
    number (personal data) into log files.
    """
    digits = str(value or "")
    if len(digits) <= 6:
        return "*" * len(digits)
    return f"{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"


def setup_logging() -> None:
    """Configure application-wide logging."""

    # Remove default handler
    logger.bind(category="system").remove()

    # Console handler
    logger.add(
        sys.stdout,
        format=settings.LOG_FORMAT,
        level=settings.LOG_LEVEL,
        colorize=True,
        backtrace=True,
        diagnose=settings.DEBUG,
    )

    # File handler - error logs (LOG_PATH is absolute: backend/logs by default)
    log_dir = settings.LOG_PATH
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.add(
        log_dir / "error_{time:YYYY-MM-DD}.log",
        format=settings.LOG_FORMAT,
        level="ERROR",
        rotation="1 day",
        retention="30 days",
        compression="gz",
        backtrace=True,
        # diagnose=True wrote every local variable (payment payloads, tokens,
        # phone numbers) into the error log on each exception.
        diagnose=False,
        # File writes go through a background queue so logging never blocks the event loop (PERF-6).
        enqueue=True,
    )

    # File handler - all logs
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        level="INFO",
        rotation="1 day",
        retention="7 days",
        compression="gz",
        enqueue=True,
    )

    # File handler - upload logs (separate file)
    logger.add(
        log_dir / "uploads_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
        level="INFO",
        rotation="1 day",
        retention="30 days",
        filter=lambda record: record["extra"].get("category") == "upload",
        enqueue=True,
    )

    # File handler - API logs
    logger.add(
        log_dir / "api_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
        level="INFO",
        rotation="1 day",
        retention="7 days",
        filter=lambda record: record["extra"].get("category") == "api",
        enqueue=True,
    )

    logger.info("Logging configured successfully")
