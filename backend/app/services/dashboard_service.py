"""Read-only queries behind the WhatsApp chat dashboard.

Everything is limited to the last DASHBOARD_HISTORY_DAYS days (90), matching how long chats and photos are kept.
Nothing here writes. Files are never returned directly: each image gets a short-lived signed address
(``sign_media``) that the media route checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import BASE_DIR, settings
from app.models.chat_log import ChatMessage, InvoiceRecord, OrderOutput
from app.models.customer import Customer
from app.models.image import Image
from app.models.wallet_transaction import WalletTransaction
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.utils.phone import normalize_phone

MEDIA_LINK_SECONDS = 900          # a link is valid for 10 to 15 minutes
MEDIA_LINK_WINDOW = 300           # expiry is rounded to 5-minute steps, so the address stays the same between refreshes


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.isoformat() if value else None


def history_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=int(settings.DASHBOARD_HISTORY_DAYS))


# ── signed media addresses ───────────────────────────────────────────────────────────────────────────────

def _mac(kind: str, media_id: str, expires: int) -> str:
    key = (settings.SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, f"{kind}:{media_id}:{expires}".encode("utf-8"), hashlib.sha256).hexdigest()


def sign_media(kind: str, media_id: str) -> str:
    """A path that serves one image for the next few minutes (a forwarded link soon stops working)."""
    expires = (int(time.time() + MEDIA_LINK_SECONDS) // MEDIA_LINK_WINDOW + 1) * MEDIA_LINK_WINDOW
    return f"/api/dashboard/media/{kind}/{media_id}?exp={expires}&sig={_mac(kind, media_id, expires)}"


def media_signature_ok(kind: str, media_id: str, expires: int, signature: str) -> bool:
    if expires < int(time.time()):
        return False
    return hmac.compare_digest(_mac(kind, media_id, expires), signature or "")


def resolve_media_file(db: Session, kind: str, media_id: str) -> Optional[Dict[str, str]]:
    """The file on disk for a photo or an output, only if it lives inside the uploads folder. None otherwise."""
    root = settings.UPLOAD_PATH.resolve()
    if kind == "photo":
        image = db.get(Image, media_id)
        raw, mime = (image.file_path, image.mime_type) if image else (None, None)
    elif kind == "output":
        output = db.get(OrderOutput, media_id)
        raw, mime = ((root / output.file_path) if output and output.file_path else None, output.mime_type if output else None)
    else:
        return None
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        path = path.resolve()
    except OSError:
        return None
    if root not in path.parents or not path.is_file():
        return None
    return {"path": str(path), "mime": mime or "image/jpeg"}


# ── customers ────────────────────────────────────────────────────────────────────────────────────────────

def _customer_for(db: Session, phone: str) -> Optional[Customer]:
    from app.services.wallet_service import find_customer_by_phone

    return find_customer_by_phone(db, phone)


def _preview(msg_type: str, text: Optional[str]) -> str:
    if msg_type == "image":
        return "📷 " + (text or "Photo")
    if msg_type == "document":
        return "📄 " + ((text or "Document").splitlines() or ["Document"])[0]
    return (text or "").replace("\n", " ")[:80]


def list_customers(db: Session, query: str = "", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    since = history_start()
    q = (query or "").strip()
    base = db.query(
        ChatMessage.customer_phone.label("phone"), func.max(ChatMessage.created_at).label("last_at"),
        func.count(ChatMessage.id).label("n"),
    ).filter(ChatMessage.created_at >= since)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")
        like = f"%{escaped}%"
        digits = "".join(ch for ch in q if ch.isdigit())          # a number search only when the search has digits
        conditions = [Customer.full_name.ilike(like, escape="\\"), Customer.business_name.ilike(like, escape="\\")]
        if digits:
            conditions.append(Customer.whatsapp_id.like(f"%{digits}%"))
        named = {normalize_phone(c.whatsapp_id) for c in db.query(Customer.whatsapp_id).filter(or_(*conditions)).limit(500).all()}
        phone_filters = [ChatMessage.customer_phone.in_(named or {"-"})]
        if digits:
            phone_filters.append(ChatMessage.customer_phone.like(f"%{digits}%"))
        base = base.filter(or_(*phone_filters))
    rows = base.group_by(ChatMessage.customer_phone).order_by(func.max(ChatMessage.created_at).desc()) \
        .limit(max(min(limit, 200), 1)).offset(max(offset, 0)).all()
    phones = [r.phone for r in rows]
    latest: Dict[str, ChatMessage] = {}
    if phones:
        ranked = db.query(
            ChatMessage.id.label("id"),
            func.row_number().over(partition_by=ChatMessage.customer_phone, order_by=ChatMessage.created_at.desc()).label("rn"),
        ).filter(ChatMessage.customer_phone.in_(phones), ChatMessage.created_at >= since).subquery()
        for msg in db.query(ChatMessage).join(ranked, ranked.c.id == ChatMessage.id).filter(ranked.c.rn == 1).all():
            latest[msg.customer_phone] = msg
    variants = {v for p in phones for v in (p, f"+{p}", p[-10:])}
    by_phone: Dict[str, Customer] = {}
    if variants:
        for c in db.query(Customer).filter(Customer.whatsapp_id.in_(variants)).all():
            by_phone.setdefault(normalize_phone(c.whatsapp_id), c)
    out = []
    for r in rows:
        customer = by_phone.get(r.phone) or _customer_for(db, r.phone)
        last = latest.get(r.phone)
        out.append({
            "phone": r.phone,
            "name": (customer.full_name if customer and customer.is_registered else None),
            "business": (customer.business_name if customer and customer.is_registered else None),
            "last_at": _iso(r.last_at),
            "last_preview": _preview(last.msg_type, last.text) if last else "",
            "last_direction": last.direction if last else None,
            "messages": int(r.n),
        })
    return out


# ── conversation ─────────────────────────────────────────────────────────────────────────────────────────

_EVENT_TEXT = {
    "credit_payment": "💳 Payment received: ₹{amt} (Razorpay {ref})",
    "credit_whatsapp_pay": "💳 Payment received: ₹{amt} (WhatsApp Pay {ref})",
    "debit_order": "🧾 ₹{amt} charged for an order",
    "refund_order": "↩️ ₹{amt} refunded for a failed order",
    "debit_refund": "↩️ ₹{amt} taken back (payment refunded)",
    "debit_dispute": "⚠️ ₹{amt} taken back (payment dispute)",
}


def _invoice_url(name: Optional[str]) -> Optional[str]:
    from urllib.parse import quote

    base = (settings.ERPNEXT_BASE_URL or "").rstrip("/")
    if not base or not name or not base.lower().startswith(("https://", "http://")):
        return None                                  # never build a link from a non-web address
    return f"{base}/app/sales-invoice/{quote(name, safe='')}"


def _message_item(db: Session, m: ChatMessage, outputs: Dict[str, OrderOutput]) -> Dict[str, Any]:
    media = None
    if m.image_id:
        media = {"kind": "photo", "url": sign_media("photo", m.image_id), "drive_link": m.drive_link}
    elif m.output_id:
        out = outputs.get(m.output_id)
        media = {"kind": "output", "url": sign_media("output", m.output_id), "drive_link": out.drive_link if out else None}
    return {
        "id": m.id, "type": "message", "direction": m.direction, "msg_type": m.msg_type, "text": m.text,
        "at": _iso(m.created_at), "ingestion_id": m.ingestion_id, "media": media,
    }


def timeline(db: Session, phone: str, before: Optional[datetime] = None, limit: int = 150) -> Dict[str, Any]:
    """The newest ``limit`` items of the chat (messages plus payment and invoice notes) older than ``before``.

    Each source is read newest-first up to ``limit`` rows. A source that hit its limit may have older rows we did not read,
    so nothing older than the OLDEST row it returned is shown; otherwise a later "load earlier" request could skip rows.
    ``cursor`` (the oldest time shown) is what to pass as ``before`` for the next page."""
    phone = normalize_phone(phone)
    since = history_start()
    upper = _aware(before) or datetime.now(timezone.utc) + timedelta(minutes=1)
    limit = max(min(limit, 500), 1)
    lower_bounds = []                                  # one per source that may have more rows than it returned

    msgs = db.query(ChatMessage).filter(
        ChatMessage.customer_phone == phone, ChatMessage.created_at >= since, ChatMessage.created_at < upper
    ).order_by(ChatMessage.created_at.desc()).limit(limit).all()
    if len(msgs) == limit:
        lower_bounds.append(_aware(msgs[-1].created_at))
    output_ids = {m.output_id for m in msgs if m.output_id}
    outputs = {o.id: o for o in db.query(OrderOutput).filter(OrderOutput.id.in_(output_ids)).all()} if output_ids else {}
    items = [_message_item(db, m, outputs) for m in msgs]

    customer = _customer_for(db, phone)
    if customer is not None:
        txs = db.query(WalletTransaction).filter(
            WalletTransaction.customer_id == customer.id, WalletTransaction.created_at >= since,
            WalletTransaction.created_at < upper, WalletTransaction.kind != "opening_balance",
        ).order_by(WalletTransaction.created_at.desc()).limit(limit).all()
        if len(txs) == limit:
            lower_bounds.append(_aware(txs[-1].created_at))
        for t in txs:
            template = _EVENT_TEXT.get(t.kind, "{kind} ₹{amt}")
            items.append({"id": f"tx-{t.id}", "type": "event", "event": "money", "text": template.format(
                amt=f"{abs(int(t.amount)):,}", ref=t.ref or "", kind=t.kind), "at": _iso(t.created_at),
                "ingestion_id": t.ingestion_id})
    invs = db.query(InvoiceRecord).filter(
        InvoiceRecord.customer_phone == phone, InvoiceRecord.created_at >= since, InvoiceRecord.created_at < upper
    ).order_by(InvoiceRecord.created_at.desc()).limit(limit).all()
    if len(invs) == limit:
        lower_bounds.append(_aware(invs[-1].created_at))
    for inv in invs:
        label = {"sent": "🧾 Invoice sent", "failed": "⚠️ Invoice could not be sent yet (retrying)", "pending": "🧾 Invoice pending"}[inv.status]
        items.append({"id": f"inv-{inv.id}", "type": "event", "event": "invoice",
                      "text": f"{label}: ₹{inv.amount_rupees:,}" + (f" ({inv.erpnext_invoice})" if inv.erpnext_invoice else ""),
                      "at": _iso(inv.created_at), "link": _invoice_url(inv.erpnext_invoice)})
    items.sort(key=lambda i: i["at"] or "")
    has_more = False
    if lower_bounds:
        floor = _iso(max(lower_bounds))
        has_more = True
        items = [i for i in items if (i["at"] or "") >= floor]
    if len(items) > limit:
        items, has_more = items[-limit:], True
    return {"phone": phone, "items": items, "has_more": has_more, "cursor": items[0]["at"] if items else None}


# ── profile ──────────────────────────────────────────────────────────────────────────────────────────────

def profile(db: Session, phone: str) -> Optional[Dict[str, Any]]:
    phone = normalize_phone(phone)
    customer = _customer_for(db, phone)
    since = history_start()
    first = db.query(func.min(ChatMessage.created_at)).filter(
        ChatMessage.customer_phone == phone, ChatMessage.created_at >= since).scalar()
    if customer is None and first is None:
        return None
    result: Dict[str, Any] = {"phone": phone, "first_seen": _iso(first), "registered": False}
    if customer is not None:
        result.update({
            "registered": bool(customer.is_registered), "name": customer.full_name, "business": customer.business_name,
            "gstin": customer.gst_number, "gst_verified": bool(customer.is_gst_verified), "address": customer.address,
            "tier": customer.tier, "balance": int(customer.wallet_balance or 0), "customer_since": _iso(customer.created_at),
        })
        from app.models.sku_credit import SKU_CREATIVE, SKU_WHITE_BG
        from app.services import google_drive, sku_packs

        result["sku_credits"] = {"white_bg": sku_packs.balance(db, customer.id, SKU_WHITE_BG),
                                 "creative_pack": sku_packs.balance(db, customer.id, SKU_CREATIVE)}
        result["drive_folder"] = google_drive.folder_link(customer.drive_folder_id) if customer.drive_folder_id else None
        txs = db.query(WalletTransaction).filter(
            WalletTransaction.customer_id == customer.id, WalletTransaction.created_at >= since
        ).order_by(WalletTransaction.created_at.desc()).all()
        result["payments"] = [
            {"at": _iso(t.created_at), "amount": int(t.amount),
             "source": "WhatsApp Pay" if t.kind == "credit_whatsapp_pay" else "Razorpay", "reference": t.ref}
            for t in txs if t.kind in ("credit_payment", "credit_whatsapp_pay")
        ]
        result["totals"] = {
            "recharged": sum(int(t.amount) for t in txs if t.kind in ("credit_payment", "credit_whatsapp_pay")),
            "spent": -sum(int(t.amount) for t in txs if t.kind == "debit_order"),
            "refunded": sum(int(t.amount) for t in txs if t.kind == "refund_order"),
        }
    orders = db.query(WhatsAppIngestion).filter(
        WhatsAppIngestion.external_user_id == phone, WhatsAppIngestion.created_at >= since
    ).order_by(WhatsAppIngestion.created_at.desc()).limit(100).all()
    counts = dict(db.query(OrderOutput.ingestion_id, func.count(OrderOutput.id)).filter(
        OrderOutput.ingestion_id.in_([o.id for o in orders] or ["-"])).group_by(OrderOutput.ingestion_id).all())
    result["orders"] = [
        {"id": o.id, "at": _iso(o.created_at), "status": o.status, "product": o.product_code,
         "amount": int(o.amount_charged or 0), "images": int(counts.get(o.id, 0)), "error": o.error_message}
        for o in orders
    ]
    result["invoices"] = [
        {"at": _iso(i.created_at), "payment_id": i.payment_id, "amount": i.amount_rupees, "status": i.status,
         "number": i.erpnext_invoice, "link": _invoice_url(i.erpnext_invoice)}
        for i in db.query(InvoiceRecord).filter(InvoiceRecord.customer_phone == phone, InvoiceRecord.created_at >= since)
        .order_by(InvoiceRecord.created_at.desc()).all()
    ]
    return result


# ── SKU pack stats ───────────────────────────────────────────────────────────────────────────────────────

def sku_stats(db: Session) -> Dict[str, Any]:
    """Packs sold and their revenue (captured pack payments) and the credits customers still hold (ledger sum)."""
    from app.models.audit_log import AuditLog
    from app.models.sku_credit import SKU_CREATIVE, SKU_WHITE_BG, CustomerSkuCredit
    from app.services.sku_packs import PAYMENT_CAPTURED_ACTION, PURPOSE_SKU_PACK

    packs = revenue = 0
    rows = db.query(AuditLog.details).filter(
        AuditLog.action == PAYMENT_CAPTURED_ACTION, AuditLog.details.contains(f'"purpose": "{PURPOSE_SKU_PACK}"'))
    for (details,) in rows.yield_per(500):
        try:
            data = json.loads(details)
        except (TypeError, ValueError):
            continue
        if data.get("purpose") == PURPOSE_SKU_PACK:
            packs += 1
            revenue += int(data.get("amount_paid") or 0)
    outstanding = dict(db.query(CustomerSkuCredit.sku, func.coalesce(func.sum(CustomerSkuCredit.quantity), 0))
                       .group_by(CustomerSkuCredit.sku).all())
    credits = {sku: max(int(outstanding.get(sku) or 0), 0) for sku in (SKU_WHITE_BG, SKU_CREATIVE)}
    return {"packs_sold": packs, "pack_revenue": revenue, "credits_outstanding": credits}


# ── weekly audit ─────────────────────────────────────────────────────────────────────────────────────────

def audit(db: Session, days: int = 7, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    days = max(min(days, int(settings.DASHBOARD_HISTORY_DAYS)), 1)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.created_at >= since, WhatsAppIngestion.image_id.isnot(None))
    if status == "problems":
        query = query.filter(WhatsAppIngestion.status.in_(("failed", "delivery_failed", "rejected", "delivered_partial")))
    elif status:
        query = query.filter(WhatsAppIngestion.status == status)
    orders = query.order_by(WhatsAppIngestion.created_at.desc()).limit(max(min(limit, 200), 1)).offset(max(offset, 0)).all()
    ids = [o.id for o in orders]
    outputs: Dict[str, List[OrderOutput]] = {}
    for out in db.query(OrderOutput).filter(OrderOutput.ingestion_id.in_(ids or ["-"])).order_by(OrderOutput.created_at).all():
        outputs.setdefault(out.ingestion_id, []).append(out)
    result = []
    for o in orders:
        customer = _customer_for(db, o.external_user_id)
        result.append({
            "id": o.id, "at": _iso(o.created_at), "phone": normalize_phone(o.external_user_id), "status": o.status,
            "name": customer.full_name if customer and customer.is_registered else None,
            "business": customer.business_name if customer and customer.is_registered else None,
            "product": o.product_code, "amount": int(o.amount_charged or 0), "error": o.error_message,
            "input": {"url": sign_media("photo", o.image_id)} if o.image_id else None,
            "outputs": [{"url": sign_media("output", x.id), "style": x.style, "drive_link": x.drive_link}
                        for x in outputs.get(o.id, []) if x.file_path],
        })
    return result
