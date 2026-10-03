"""Retention and erasure of customer data (DATA-7, PRIV-3).

Retention (off until RETENTION_ENABLED is true; run daily by ``run_retention_forever``):
  * customer photos from finished WhatsApp orders are deleted from disk after RETENTION_MEDIA_DAYS (the database row
    stays, marked ``expired``, so order history still reads correctly);
  * phone numbers inside audit-log details are masked after RETENTION_AUDIT_MASK_DAYS;
  * financial records (wallet ledger, payment and refund rows, invoices) are NEVER touched here: they are kept for the
    statutory period (8 years) and only a person may remove them.

Erasure ("DELETE MY DATA", or ``scripts/erase_customer.py``): removes a customer's photos and personal details but keeps
the money trail. The wallet ledger is linked to the customer row by id, so the row stays with its name, business,
GSTIN and address replaced and its phone number replaced by a placeholder; the phone number is removed from the
customer's order and audit records. Payment and invoice records that carry the number are kept for tax and audit
purposes and the customer is told so. A customer with money left in the wallet is refused (a person must settle it).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.image import Image
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.utils.file_helpers import delete_directory
from app.utils.logger import logger

# Orders in these states are over; anything else may still need the photo.
FINISHED_STATUSES = ("delivered", "delivered_partial", "failed", "delivery_failed", "rejected", "unfunded")
EXPIRED = "expired"
ERASURE_REQUEST_ACTION = "erasure_requested"
ERASED_ACTION = "customer_erased"
ERASURE_CONFIRM_WINDOW = timedelta(minutes=15)

_LONG_NUMBER_RE = re.compile(r"(?<!\d)(\+?\d{10,15})(?!\d)")


def mask_numbers(text: Optional[str]) -> Optional[str]:
    """Replace every 10-15 digit run with its first two and last four digits (919812345678 -> 91******5678)."""
    if not text:
        return text

    def _mask(match: "re.Match[str]") -> str:
        digits = match.group(1).lstrip("+")
        return f"{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"

    return _LONG_NUMBER_RE.sub(_mask, text)


def _delete_image_files(image: Image) -> None:
    delete_directory(str(settings.UPLOAD_PATH / image.request_id))


def purge_expired_media(db: Session, days: Optional[int] = None, batch: int = 200) -> int:
    """Delete the photo files of finished WhatsApp orders older than ``days``. Returns how many images were expired."""
    days = int(settings.RETENTION_MEDIA_DAYS if days is None else days)
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(WhatsAppIngestion.image_id)
        .filter(WhatsAppIngestion.image_id.isnot(None), WhatsAppIngestion.status.in_(FINISHED_STATUSES),
                WhatsAppIngestion.created_at < cutoff)
        .limit(batch * 3)
        .all()
    )
    expired = 0
    for (image_id,) in rows:
        if expired >= batch:
            break
        image = db.get(Image, image_id)
        if image is None or image.processing_status == EXPIRED:
            continue
        _delete_image_files(image)
        image.processing_status = EXPIRED
        expired += 1
    db.commit()
    if expired:
        logger.bind(category="system").info(f"Retention: expired {expired} customer photo(s) older than {days} days")
    return expired


def mask_old_audit_details(db: Session, days: Optional[int] = None, batch: int = 500) -> int:
    """Mask phone numbers in the details of audit rows older than ``days``, page by page until every old row has been
    looked at. Rows that a person still has to act on (a pending payment review, a pending clawback) keep their numbers
    until they are resolved. Returns how many rows changed."""
    days = int(settings.RETENTION_AUDIT_MASK_DAYS if days is None else days)
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    changed = 0
    last_id = ""
    while True:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.created_at < cutoff, AuditLog.details.isnot(None), AuditLog.id > last_id,
                    or_(AuditLog.status.is_(None), AuditLog.status != "pending"))
            .order_by(AuditLog.id)
            .limit(batch)
            .all()
        )
        if not rows:
            break
        for row in rows:
            masked = mask_numbers(row.details)
            if masked != row.details:
                row.details = masked
                changed += 1
        db.commit()
        last_id = rows[-1].id
    return changed


def run_retention_pass() -> Dict[str, int]:
    """One retention pass in its own session (blocking). Does nothing unless RETENTION_ENABLED."""
    if not settings.RETENTION_ENABLED:
        return {"photos": 0, "audit_rows": 0}
    from app.database import SessionLocal

    with SessionLocal() as db:
        photos = purge_expired_media(db)
        audit_rows = mask_old_audit_details(db)
    return {"photos": photos, "audit_rows": audit_rows}


async def run_retention_forever() -> None:
    """Background loop started from the application lifespan: one pass a day, in one process only."""
    import asyncio

    from app.services.scheduler_lease import holds_lease
    from app.utils.executors import run_io

    interval = 24 * 3600
    while True:
        await asyncio.sleep(3600)                       # wake hourly; the lease keeps it to one pass a day
        try:
            if not settings.RETENTION_ENABLED:
                continue
            if not await holds_lease("retention", interval):
                continue
            result = await run_io(run_retention_pass)
            if any(result.values()):
                logger.bind(category="system").info(f"Retention pass: {result}")
            await asyncio.sleep(interval - 3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Retention pass failed: {type(e).__name__}: {e}")


# ── Erasure ───────────────────────────────────────────────────────────────────────────────────────────────

def request_erasure(db: Session, customer: Customer) -> None:
    """Record that this customer asked to be erased; the second confirming message must follow soon."""
    db.add(AuditLog(user_id=None, action=ERASURE_REQUEST_ACTION, resource_id=customer.id, resource_type="customer",
                    status="pending"))
    db.commit()


def erasure_requested_recently(db: Session, customer: Customer) -> bool:
    since = datetime.now(timezone.utc) - ERASURE_CONFIRM_WINDOW
    return db.query(AuditLog.id).filter(
        AuditLog.action == ERASURE_REQUEST_ACTION, AuditLog.resource_id == customer.id, AuditLog.created_at >= since
    ).first() is not None


class ErasureRefused(Exception):
    """Raised when erasure cannot proceed; the message is safe to show the customer."""


def erase_customer(db: Session, customer: Customer) -> Dict[str, int]:
    """Erase one customer's personal data (see the module docstring). Returns counts of what was removed."""
    db.query(Customer).filter(Customer.id == customer.id).with_for_update().first()   # no credit can slip in meanwhile
    db.refresh(customer)
    if customer.tier == "ADMIN":
        raise ErasureRefused("Team accounts are removed by the owner, not through this command.")
    if int(customer.wallet_balance or 0) > 0:
        raise ErasureRefused(
            f"Your wallet still holds ₹{int(customer.wallet_balance):,}. Wallet balances are not refunded, so please use "
            "your balance for orders first, then ask again."
        )
    phone = customer.whatsapp_id
    variants = {phone, f"+{phone}", phone[-10:]} if phone.isdigit() else {phone}
    # Refuse while anything involving money or an order for this number is still moving: a refund, a late payment or a
    # generation in progress would otherwise land on an erased, unreachable account.
    from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
    from app.services.meta_whatsapp_service import STUCK_PAID_STATUSES

    busy = db.query(WhatsAppIngestion.id).filter(
        WhatsAppIngestion.external_user_id.in_(variants),
        WhatsAppIngestion.status.in_(tuple(STUCK_PAID_STATUSES) + ("choice_claimed", "received")),
    ).first() is not None or db.query(WhatsAppPaymentOrder.id).filter(
        WhatsAppPaymentOrder.whatsapp_id.in_(variants),
        WhatsAppPaymentOrder.status.in_(("created", "sent", "pending")),
    ).first() is not None
    if busy:
        raise ErasureRefused("You still have an order or a payment in progress. Please wait until it finishes, then ask again.")
    photos = 0
    ingestions = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.external_user_id.in_(variants)).all()
    for ingestion in ingestions:
        if ingestion.image_id:
            image = db.get(Image, ingestion.image_id)
            if image is not None and image.processing_status != EXPIRED:
                _delete_image_files(image)
                image.processing_status = EXPIRED
                photos += 1
        ingestion.external_user_id = f"erased-{customer.id[:8]}"
        ingestion.caption = None
        ingestion.external_media_id = None
    masked_rows = 0
    last4 = phone[-4:]
    pattern = re.compile(r"(?<!\d)\+?(?:" + "|".join(re.escape(v.lstrip("+")) for v in variants) + r")(?!\d)")
    for row in db.query(AuditLog).filter(AuditLog.details.isnot(None), AuditLog.details.contains(last4)).limit(5000).all():
        masked = pattern.sub(lambda m: mask_numbers(m.group(0)) or "", row.details)
        if masked != row.details:
            row.details = masked
            masked_rows += 1
    customer.full_name = "Deleted customer"
    customer.business_name = "-"
    customer.gst_number = "-"
    customer.address = "-"
    customer.is_registered = False
    customer.is_gst_verified = False
    customer.whatsapp_id = f"erased-{customer.id}"[:100]
    db.add(AuditLog(user_id=None, action=ERASED_ACTION, resource_id=customer.id, resource_type="customer",
                    status="success", details=f"photos={photos} orders={len(ingestions)}"))
    db.commit()
    logger.bind(category="system").info(f"Customer {customer.id} erased: photos={photos} orders={len(ingestions)}")
    return {"photos": photos, "orders": len(ingestions), "audit_rows": masked_rows}


def erase_customer_by_id(customer_id: str) -> Dict[str, int]:
    """Erase in its own session (blocking). Raises ErasureRefused when erasure is not allowed."""
    from app.database import SessionLocal

    with SessionLocal() as db:
        customer = db.get(Customer, customer_id)
        if customer is None:
            return {"photos": 0, "orders": 0, "audit_rows": 0}
        return erase_customer(db, customer)


ERASURE_ASK_MESSAGE = (
    "You asked us to delete your data. This removes your photos and your name, business details and address, and you "
    "will not be able to use your wallet afterwards. Payment and invoice records we must keep by law stay as they are. "
    "To confirm, reply CONFIRM DELETE within 15 minutes. To keep your account, ignore this message."
)
ERASURE_DONE_MESSAGE = (
    "Done. Your photos and personal details have been deleted. Payment and invoice records that the law requires us "
    "to keep remain. You are welcome to come back any time."
)
ERASURE_NOTHING_MESSAGE = "We could not find an account for this number, so there is nothing to delete."
ERASURE_EXPIRED_MESSAGE = "We have no pending delete request for you (they expire after 15 minutes). Send DELETE MY DATA to start again."


def is_erasure_request(text: str) -> bool:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!") == "delete my data"


def is_erasure_confirmation(text: str) -> bool:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!") == "confirm delete"


def new_placeholder_id() -> str:
    return uuid.uuid4().hex[:8]
