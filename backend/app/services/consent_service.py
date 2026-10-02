"""Consent to the data notice before any personal data is collected (PRIV-2).

OFF until the owner supplies the notice wording: with ``CONSENT_REQUIRED`` false, or an empty ``CONSENT_NOTICE_TEXT``,
nothing changes for customers. When on, a phone number that has not agreed to the current ``CONSENT_VERSION`` is shown
the notice with an "I agree" button before the welcome form, a registration reply or a photo is processed. The time and
version of each agreement are stored in ``consent_records``.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.consent_record import ConsentRecord
from app.utils.phone import normalize_phone

CONSENT_YES = "gv_consent_yes"
CONSENT_NO = "gv_consent_no"
DECLINED_MESSAGE = "No problem. We have not stored anything. Send HI whenever you would like to start."
AGREED_MESSAGE = "Thank you! You can now send HI to begin, or send your photo."


def is_active() -> bool:
    """Is the consent step switched on (and properly configured)?"""
    return bool(settings.CONSENT_REQUIRED) and bool((settings.CONSENT_NOTICE_TEXT or "").strip())


def has_consented(db: Session, phone: str) -> bool:
    if not is_active():
        return True
    return db.query(ConsentRecord.id).filter(
        ConsentRecord.whatsapp_id == normalize_phone(phone), ConsentRecord.version == str(settings.CONSENT_VERSION)
    ).first() is not None


def record_consent(db: Session, phone: str) -> None:
    """Store that this number agreed to the current version (a repeat tap is harmless)."""
    try:
        db.add(ConsentRecord(whatsapp_id=normalize_phone(phone), version=str(settings.CONSENT_VERSION)))
        db.commit()
    except IntegrityError:
        db.rollback()


async def ask_for_consent(phone: str, reply_to_message_id: str | None = None) -> bool:
    """Send the notice with the two buttons. Returns True when it was sent."""
    from app.services.meta_whatsapp_service import send_reply_buttons

    return await send_reply_buttons(
        phone,
        settings.CONSENT_NOTICE_TEXT.strip(),
        [(CONSENT_YES, "I agree"), (CONSENT_NO, "No thanks")],
        reply_to_message_id=reply_to_message_id,
    )
