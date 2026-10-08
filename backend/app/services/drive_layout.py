"""Each customer's folders in the business shared drive (Phase 8).

    {phone}/                         root, shared with the customer's email as reader (never "anyone with the link")
      Images/{7 Oct 2026}/001.png    one folder per IST day; deleted after DRIVE_IMAGES_RETENTION_DAYS
      Invoices/                      invoice PDFs, kept (8-year accounting rule)
      Usage Logs                     Google Sheet, a view of customer_sku_credits (app/services/usage_log.py)

The folder ids are stored on the customer (migration 0022). They are created once: a per-customer lock in this
process, then ONE guarded UPDATE (``WHERE drive_folder_id IS NULL``); a process that loses that race deletes the set
it made and uses the stored one. Database work runs in short sessions on the I/O pool and never spans a Drive call.
"""

from __future__ import annotations

import asyncio
import time
import re
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services import google_drive, metrics
from app.services.google_drive import DriveError
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone

IST = timezone(timedelta(hours=5, minutes=30))
IMAGES_FOLDER = "Images"
INVOICES_FOLDER = "Invoices"
USAGE_LOG_NAME = "Usage Logs"
USAGE_LOG_HEADER = ["Time (IST)", "Event", "Images used", "Balance left"]
SHARE_KIND = "drive_share"
IMAGE_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

GOOGLE_EMAIL_REQUEST = (
    "We could not share your Google Drive image folder with the email address you gave us, because Google only "
    "shares it with a Google account. Please send us an email address that is a Google account (for example a "
    "Gmail address) and we will share your folder with it."
)

metrics.registry.describe("moraa_drive_share_total", "Customer Drive folder shares by outcome")

_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def folder_shared_sync(customer_id: str) -> bool:
    """(blocking) True when this customer's folder exists and is shared with their current email, so a Drive link
    is something they can open. Otherwise delivery stays on WhatsApp; a share is queued if an email is known."""
    info = _customer(customer_id)
    if info is None:
        return False
    shared = bool(info.get("root")) and bool(info.get("email")) and info.get("shared_to") == info.get("email")
    if not shared and info.get("email"):
        queue_share(customer_id)
    return shared


def delivery_enabled() -> bool:
    return bool(settings.DRIVE_DELIVERY_ENABLED) and google_drive.configured()


def customer_lock(customer_id: str) -> asyncio.Lock:
    """The in-process lock for one customer's folder set, day folders and file numbers."""
    lock = _locks.get(customer_id)
    if lock is None:
        lock = _locks[customer_id] = asyncio.Lock()
    return lock


def day_label(when: Optional[datetime] = None) -> str:
    """The IST day folder name, e.g. "7 Oct 2026" (no leading zero). A naive time is taken as UTC."""
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    day = moment.astimezone(IST)
    return f"{day.day} {day:%b %Y}"


# ── the customer row (blocking, short sessions) ────────────────────────────────────────────────────────────

def _customer(customer_id: str) -> Optional[Dict[str, Optional[str]]]:
    from app.database import SessionLocal
    from app.models.customer import Customer

    with SessionLocal() as db:
        c = db.get(Customer, customer_id)
        if c is None:
            return None
        return {"phone": c.whatsapp_id, "email": c.email, "shared_to": c.drive_shared_to, "root": c.drive_folder_id,
                "images": c.drive_images_folder_id, "invoices": c.drive_invoices_folder_id, "sheet": c.drive_sheet_id}


def _store_folders(customer_id: str, ids: Dict[str, str]) -> bool:
    """Store a new folder set unless another process already did. True when this one was stored."""
    from app.database import SessionLocal
    from app.models.customer import Customer

    with SessionLocal() as db:
        stored = db.query(Customer).filter(Customer.id == customer_id, Customer.drive_folder_id.is_(None)).update({
            Customer.drive_folder_id: ids["root"], Customer.drive_images_folder_id: ids["images"],
            Customer.drive_invoices_folder_id: ids["invoices"], Customer.drive_sheet_id: ids["sheet"],
        }, synchronize_session=False)
        db.commit()
        return stored == 1


def _update_customer(customer_id: str, values: Dict[str, Any]) -> None:
    from app.database import SessionLocal
    from app.models.customer import Customer

    with SessionLocal() as db:
        db.query(Customer).filter(Customer.id == customer_id).update(values, synchronize_session=False)
        db.commit()


def _folder_set(info: Optional[Dict[str, Optional[str]]]) -> Optional[Dict[str, str]]:
    """The stored ids, None when there are none yet."""
    if info is None:
        raise DriveError(404, "customerNotFound", retryable=False)
    if not info["root"]:
        return None
    if not (info["images"] and info["invoices"] and info["sheet"]):
        raise DriveError(410, "customerErased", retryable=False)   # only erasure leaves a partial set
    return {"root": info["root"], "images": info["images"], "invoices": info["invoices"], "sheet": info["sheet"]}


# ── folders ────────────────────────────────────────────────────────────────────────────────────────────────

async def _discard(folder_id: str) -> None:
    try:
        await google_drive.delete(folder_id)
    except Exception as e:  # noqa: BLE001 -- an unused empty folder is harmless
        logger.warning(f"An unused Drive folder could not be removed ({type(e).__name__})")


async def ensure_customer_folders(customer_id: str) -> Dict[str, str]:
    """{"root", "images", "invoices", "sheet"} for this customer, created on first use."""
    found = _folder_set(await run_io(_customer, customer_id))
    if found:
        return found
    async with customer_lock(customer_id):
        info = await run_io(_customer, customer_id)
        found = _folder_set(info)
        if found:
            return found
        root = await google_drive.create_folder(info["phone"], google_drive.shared_drive_id())
        try:
            ids = {"root": root,
                   "images": await google_drive.create_folder(IMAGES_FOLDER, root),
                   "invoices": await google_drive.create_folder(INVOICES_FOLDER, root),
                   "sheet": await google_drive.create_sheet(USAGE_LOG_NAME, root)}
            await google_drive.sheet_append(ids["sheet"], [USAGE_LOG_HEADER])
            stored = await run_io(_store_folders, customer_id, ids)
        except Exception:
            await _discard(root)
            raise
        if stored:
            logger.bind(category="system").info(f"Drive folders created for {mask_phone(info['phone'])}")
            return ids
        await _discard(root)                     # another process stored its set first: use that one
        return _folder_set(await run_io(_customer, customer_id))


async def _day_folder(images_id: str, when: Optional[datetime]) -> Tuple[str, str]:
    """Find or create the IST day folder under Images. The caller holds the customer lock."""
    label = day_label(when)
    found = await google_drive.find_child(label, images_id, google_drive.FOLDER_MIME)
    return (found or await google_drive.create_folder(label, images_id)), label


async def ensure_day_folder(customer_id: str, when: Optional[datetime] = None) -> Tuple[str, str]:
    """(folder_id, label) of the customer's Images/{day} folder for ``when`` (default now), in IST."""
    folders = await ensure_customer_folders(customer_id)
    async with customer_lock(customer_id):
        return await _day_folder(folders["images"], when)


def _next_number(children: List[Dict[str, Any]]) -> int:
    numbers = [int(m.group(1)) for c in children if (m := re.fullmatch(r"(\d+)\.\w+", c.get("name") or ""))]
    return max(numbers, default=0) + 1


async def upload_delivery_image(customer_id: str, path: str, mime: str,
                                when: Optional[datetime] = None) -> Dict[str, str]:
    """Upload a finished image as the next number in its day folder (001.png, 002.png ...), streamed from disk.
    Returns {"file_id", "name", "day_folder_id", "day_folder_link"}."""
    folders = await ensure_customer_folders(customer_id)
    extension = IMAGE_EXTENSIONS.get(mime, ".png")
    # ponytail: the day-folder search and the numbering are locked per process only. Two server processes can rarely
    # pick the same number (Drive keeps both files); a per-customer counter in the database is the upgrade.
    async with customer_lock(customer_id):
        day_id, _label = await _day_folder(folders["images"], when)
        name = f"{_next_number(await google_drive.list_children(day_id)):03d}{extension}"
        uploaded = await google_drive.upload_file(path, name, day_id, mime)
    return {"file_id": uploaded["id"], "name": name, "day_folder_id": day_id,
            "day_folder_link": google_drive.folder_link(day_id)}


async def upload_invoice(customer_id: str, pdf_path: str, name: str) -> Dict[str, str]:
    """Upload an invoice PDF into the customer's Invoices folder. Returns {"file_id", "link"}."""
    folders = await ensure_customer_folders(customer_id)
    uploaded = await google_drive.upload_file(pdf_path, name, folders["invoices"], "application/pdf")
    return {"file_id": uploaded["id"], "link": uploaded["link"]}


# ── sharing ────────────────────────────────────────────────────────────────────────────────────────────────

async def _share_refused(phone: str, error: DriveError) -> None:
    """Google would not share with that address: the folder stays private and the customer is asked for a Google
    email. Never falls back to link sharing."""
    logger.error(f"ALERT drive share refused for {mask_phone(phone)} ({error.reason or error.status}): the folder "
                 "stays private and the customer was asked for a Google email")
    metrics.registry.inc("moraa_drive_share_total", {"outcome": "refused"})
    from app.services.meta_whatsapp_service import send_whatsapp_text

    try:
        await send_whatsapp_text(phone, GOOGLE_EMAIL_REQUEST)
    except Exception as e:  # noqa: BLE001 -- the alert above already tells the team
        logger.warning(f"Google email request not sent to {mask_phone(phone)}: {type(e).__name__}")


async def share_customer_folder(customer_id: str) -> str:
    """Share the customer's root folder with their email as reader. Returns "shared", "already", "no_email",
    "refused" or "not_configured". Raises a retryable DriveError for the outbox to try again."""
    if not delivery_enabled():
        return "not_configured"
    info = await run_io(_customer, customer_id)
    email = ((info or {}).get("email") or "").strip().lower()
    if not email:
        return "no_email"
    old = (info["shared_to"] or "").strip().lower()
    if old == email:
        return "already"
    root = (await ensure_customer_folders(customer_id))["root"]
    try:
        await google_drive.share_with_email(root, email, "reader")
    except DriveError as e:
        if e.retryable:
            raise
        await _share_refused(info["phone"], e)
        return "refused"
    if old:                                      # the email changed: the old address loses access
        for permission in await google_drive.list_permissions(root):
            if permission.get("type") == "user" and (permission.get("emailAddress") or "").lower() == old:
                await google_drive.remove_permission(root, permission["id"])
    await run_io(_update_customer, customer_id, {"drive_shared_to": email})
    metrics.registry.inc("moraa_drive_share_total", {"outcome": "shared"})
    return "shared"


def queue_share(customer_id: str) -> None:
    """(blocking) Ask the outbox to share the customer's folder (once per email address). Never raises."""
    try:
        if not delivery_enabled():
            return
        info = _customer(customer_id)
        if info is None:
            return
        from app.services import outbox

        bucket = int(time.time() // 60)
        outbox.enqueue_job(SHARE_KIND, {"customer_id": customer_id}, f"share:{customer_id}:{bucket}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Drive share could not be queued: {type(e).__name__}")


async def handle_share_job(payload: Dict[str, Any]) -> bool:
    """Outbox job "drive_share". A retryable Drive failure raises (the outbox retries); anything else is done."""
    try:
        outcome = await share_customer_folder(payload["customer_id"])
    except DriveError as e:
        if e.retryable:
            raise
        logger.error(f"Drive share gave up ({e.status} {e.reason})")
        return True
    logger.bind(category="system").info(f"Drive share: {outcome}")
    return True


# ── erasure and retention ──────────────────────────────────────────────────────────────────────────────────

async def erase_customer_drive(customer_id: str) -> Dict[str, int]:
    """Customer erasure: delete the Images folder and the Usage Logs sheet, take every direct user permission off the
    root and rename it erased-<id>. Invoices stay (accounting rule). Raises DriveError when Drive fails."""
    removed = {"images": 0, "sheet": 0, "permissions": 0, "renamed": 0}
    info = await run_io(_customer, customer_id)
    if info is None or not info["root"]:
        return removed
    root = info["root"]
    if info["images"]:
        await google_drive.delete(info["images"])
        removed["images"] = 1
    if info["sheet"]:
        await google_drive.delete(info["sheet"])
        removed["sheet"] = 1
    for permission in await google_drive.list_permissions(root):
        inherited = any(d.get("inherited") for d in permission.get("permissionDetails") or [])
        if permission.get("type") == "user" and not inherited:   # shared-drive members are inherited: they stay
            await google_drive.remove_permission(root, permission["id"])
            removed["permissions"] += 1
    await google_drive.rename(root, f"erased-{customer_id[:8]}")
    removed["renamed"] = 1
    await run_io(_update_customer, customer_id, {"drive_images_folder_id": None, "drive_sheet_id": None,
                                                 "drive_shared_to": None})
    return removed


def delete_drive_files_sync(ids) -> int:
    """(blocking) Retention: delete delivered images from Drive. Returns how many are gone."""
    return google_drive.delete_files_sync(ids)
