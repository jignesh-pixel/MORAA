"""Claim a Meta message id once (MON-10). See ``app/models/processed_message.py``."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.processed_message import ProcessedMessage
from app.utils.logger import logger


def claim_message(db: Session, message_id: str) -> bool:
    """Record ``message_id`` as being handled. True = first delivery, go ahead; False = a repeat, skip it.

    The claim is committed on its own right away, so a second delivery that arrives while this one is
    still being handled is already refused. An empty id cannot be tracked and is always let through.
    A database error is let through too: handling a message twice is better than silently dropping it.
    """
    if not message_id:
        return True
    try:
        db.add(ProcessedMessage(message_id=message_id[:255]))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    except Exception as e:
        db.rollback()
        logger.error(f"Message dedupe claim failed for a message; handling it anyway: {e}")
        return True


PROCESSED_MESSAGE_RETENTION = timedelta(days=14)    # Meta stops retrying well before this


def purge_old_processed_messages(older_than: timedelta = PROCESSED_MESSAGE_RETENTION) -> int:
    """Delete duplicate-delivery bookkeeping older than ``older_than``. Own session; never raises."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = db.execute(
            delete(ProcessedMessage).where(ProcessedMessage.created_at < datetime.now(timezone.utc) - older_than)
        )
        db.commit()
        return int(result.rowcount or 0)
    except Exception as e:
        db.rollback()
        logger.error(f"Could not purge old processed-message ids: {e}")
        return 0
    finally:
        db.close()


def release_messages(db: Session, message_ids: list) -> None:
    """Give claims back after handling failed, so Meta's retry of the message is handled, not skipped."""
    if not message_ids:
        return
    try:
        db.rollback()
        db.execute(delete(ProcessedMessage).where(ProcessedMessage.message_id.in_([m[:255] for m in message_ids])))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Could not release {len(message_ids)} message claim(s) after a failure: {e}")
