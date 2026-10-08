"""Post-payment invoice dispatch: ERPNext Sales Invoice PDF, local fallback.

Runs as a FastAPI BackgroundTask AFTER the wallet credit is committed and
the "Payment Received" text is sent, so it never adds webhook latency and
never touches money. Never raises.

    ERPNEXT_INVOICE_ENABLED true + configured + ERPNext succeeds
        -> submitted Sales Invoice (+ Payment Entry) PDF sent on WhatsApp
    anything else (disabled, not configured, error, timeout, send failure)
        -> the existing local ReportLab receipt, exactly as before

A SKU pack purchase (Phase 8) is billed as one line per SKU bought instead of one wallet-recharge line. With Drive
delivery on, the PDF goes into the customer's {phone}/Invoices/ folder and the customer gets a short text with the
link; the WhatsApp document is only the fallback when Drive fails.
"""

import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import settings
from app.services.erpnext_service import get_erpnext_service
from app.utils.executors import run_cpu, run_io
from app.utils.logger import logger, mask_phone

INVOICE_LINK_TEXT = "Your invoice {name} is saved in your Google Drive folder:\n{link}"
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
_PLACEHOLDER_NAMES = {"", "valued customer", "jewelry business", "there", "customer"}


def billing_name(snapshot: Optional[Dict[str, Any]], whatsapp_id: str) -> str:
    """Business name, else full name, else the WhatsApp number (never a placeholder)."""
    snapshot = snapshot or {}
    for key in ("business_name", "full_name"):
        value = str(snapshot.get(key) or "").strip()
        if value.lower() not in _PLACEHOLDER_NAMES:
            return value
    return f"WhatsApp {whatsapp_id}"


def billing_gstin(snapshot: Optional[Dict[str, Any]]) -> Optional[str]:
    value = str((snapshot or {}).get("gst_number") or "").replace(" ", "").upper()
    if not _GSTIN_RE.match(value):
        return None
    # With live verification switched on, only a GSTIN the registry confirmed is printed on an invoice (EXT-8).
    if settings.GST_VERIFICATION_ENABLED and (snapshot or {}).get("is_gst_verified") is not True:
        return None
    return value


async def _note_invoice(payment_id: str, phone: str, amount: int, status: str, invoice: Optional[str] = None,
                        drive: Optional[Dict[str, str]] = None) -> None:
    """Remember this invoice for the chat dashboard. Never raises, never delays the invoice."""
    try:
        from app.services import chat_log

        drive = drive or {}
        chat_log.fire(chat_log.upsert_invoice, payment_id, phone, amount, status, invoice, drive.get("file_id"),
                      drive.get("link"))
    except Exception:  # noqa: BLE001
        pass


# ── SKU packs ──────────────────────────────────────────────────────────────────────────────────────────────

def _payment_context(payment_id: str, recipient_id: str) -> Dict[str, Any]:
    """(blocking) The customer id for this number and the SKUs this payment bought (0 and 0 for a wallet recharge)."""
    from app.database import SessionLocal
    from app.models.customer import Customer
    from app.models.sku_credit import ACTION_PURCHASE, SKU_CREATIVE, SKU_WHITE_BG, CustomerSkuCredit
    from app.services.sku_packs import purchase_reference

    bought = {}
    with SessionLocal() as db:
        for sku in (SKU_WHITE_BG, SKU_CREATIVE):
            bought[sku] = int(db.query(CustomerSkuCredit.quantity).filter(
                CustomerSkuCredit.reference_id == purchase_reference(payment_id, sku),
                CustomerSkuCredit.action == ACTION_PURCHASE).scalar() or 0)
        customer_id = db.query(Customer.id).filter(Customer.whatsapp_id == recipient_id).scalar()
    return {"customer_id": customer_id, "white": bought[SKU_WHITE_BG], "creative": bought[SKU_CREATIVE]}


def _pack_line(qty: int, total: int, description: str) -> Dict[str, Any]:
    from app.services.pricing import erpnext_pack_line

    if qty > 0 and total > 0 and total % qty == 0:
        line = erpnext_pack_line(qty, total // qty)
    else:
        line = erpnext_pack_line(1, total)        # not a whole rupee per unit: one line for the whole amount
    line["description"] = description
    return line


def pack_invoice_lines(white_units: int, creative_packs: int, amount: int) -> List[Dict[str, Any]]:
    """ERPNext lines for a pack payment that add up exactly to ``amount``: white-background SKUs (qty = units, at the
    unit price actually paid) and Catalog Pack SKUs (qty = packs, at the creative pack price). The white unit
    price is (amount - creative total) / units, so a price change between the order and the invoice never shows."""
    from app.services.pricing import CREATIVE_TITLE, creative_pack_price, pack_title

    white_label = f"White-background SKUs ({pack_title(white_units)})"
    creative_label = f"{CREATIVE_TITLE} SKUs ({pack_title(creative_packs)})"
    if not white_units:
        return [_pack_line(creative_packs, amount, creative_label)]
    creative_total = creative_packs * creative_pack_price()
    if amount - creative_total <= 0:              # cannot happen for a priced cart; never invoice a negative line
        return [_pack_line(1, amount, f"{white_label} + {creative_label}")]
    lines = [_pack_line(white_units, amount - creative_total, white_label)]
    if creative_packs:
        lines.append(_pack_line(creative_packs, creative_total, creative_label))
    return lines


def pack_description(white_units: int, creative_packs: int) -> str:
    """What a pack payment bought, for the local receipt."""
    from app.services.pricing import CREATIVE_TITLE, pack_title

    parts = [f"{pack_title(white_units)} (white background)"] if white_units else []
    if creative_packs:
        parts.append(f"{CREATIVE_TITLE} {pack_title(creative_packs)}")
    return " + ".join(parts)


# ── sending: the Drive link, or the WhatsApp document ────────────────────────────────────────────────────────

def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


async def _invoice_to_drive(recipient_id: str, customer_id: Optional[str], pdf_bytes: bytes,
                            invoice_name: str) -> Optional[Dict[str, str]]:
    """Put the PDF in the customer's Invoices folder and text them the link. {"file_id", "link"}, or None when Drive
    delivery is off, the customer is unknown, or Drive / the text failed (the caller sends the document instead)."""
    from app.services import drive_layout

    if not customer_id or not drive_layout.delivery_enabled():
        return None
    if not await run_io(drive_layout.folder_shared_sync, customer_id):
        return None                      # the customer could not open the link yet: the PDF goes on WhatsApp
    name = f"{datetime.now(timezone.utc).astimezone(drive_layout.IST):%Y-%m-%d} {invoice_name}.pdf"
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "invoice.pdf")
            await run_io(_write, path, pdf_bytes)
            uploaded = await drive_layout.upload_invoice(customer_id, path, name)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Invoice {invoice_name} not saved to Drive ({type(e).__name__}); sending it on WhatsApp")
        return None
    from app.services.meta_whatsapp_service import send_whatsapp_text

    try:
        sent = await send_whatsapp_text(recipient_id, INVOICE_LINK_TEXT.format(name=invoice_name, link=uploaded["link"]))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Invoice link text failed ({type(e).__name__})")
        sent = False
    if not sent:
        logger.warning(f"Invoice {invoice_name} is in Drive but the link text was not sent; sending the document")
        return None
    return uploaded


async def _send_invoice(recipient_id: str, customer_id: Optional[str], pdf_bytes: bytes, invoice_name: str,
                        send_document_fn: Callable[..., Any]) -> Tuple[Any, Optional[Dict[str, str]]]:
    """(send result, Drive ids or None): the Drive link when Drive delivery is on, else the WhatsApp document."""
    drive = await _invoice_to_drive(recipient_id, customer_id, pdf_bytes, invoice_name)
    if drive:
        return True, drive
    sent = await send_document_fn(
        recipient_id=recipient_id,
        document_bytes=pdf_bytes,
        filename=f"{invoice_name}.pdf",
        caption="",
    )
    return sent, None


async def dispatch_payment_invoice(
    recipient_id: str,
    payment_id: str,
    amount: int,
    customer_name: str,
    customer_snapshot: Optional[Dict[str, Any]],
    local_pdf_fn: Callable[..., bytes],
    send_document_fn: Callable[..., Any],
) -> str:
    """Send the invoice PDF for one captured payment. Returns "erpnext", "local" or "failed".

    With ERPNext switched on, ERPNext is the ONLY source of invoice numbers (GST needs one consecutive series): if it
    cannot produce or send the invoice the result is "failed" and the durable outbox retries later (ERPNext reuses
    the invoice it already made for this payment id, so a retry never makes a second one). The customer has already
    been sent the "payment received" message. With ERPNext off, the local payment receipt is sent as before.
    ``local_pdf_fn`` / ``send_document_fn`` are passed in by the caller."""
    try:
        context = await run_io(_payment_context, payment_id, recipient_id)
    except Exception as e:  # noqa: BLE001 -- then billed as a wallet recharge and sent as a document, as before
        logger.warning(f"Payment {payment_id} lookup for the invoice failed ({type(e).__name__})")
        context = {"customer_id": None, "white": 0, "creative": 0}
    is_pack = bool(context["white"] or context["creative"])
    if settings.ERPNEXT_INVOICE_ENABLED:
        try:
            extra = {"lines": pack_invoice_lines(context["white"], context["creative"], amount)} if is_pack else {}
            result = await get_erpnext_service().create_paid_invoice_pdf(
                whatsapp_id=recipient_id,
                customer_name=billing_name(customer_snapshot, recipient_id),
                gstin=billing_gstin(customer_snapshot),
                amount=amount,
                payment_id=payment_id,
                **extra,
            )
            if result:
                pdf_bytes, invoice_name = result
                sent, drive = await _send_invoice(recipient_id, context["customer_id"], pdf_bytes, invoice_name,
                                                  send_document_fn)
                if sent:
                    logger.info(f"ERPNext invoice {invoice_name} sent to {mask_phone(recipient_id)} (payment={payment_id})")
                    await _note_invoice(payment_id, recipient_id, amount, "sent", invoice_name, drive)
                    return "erpnext"
                logger.warning(f"ERPNext invoice {invoice_name} could not be sent; it will be retried")
            else:
                logger.warning(f"ERPNext produced no invoice for {payment_id}; it will be retried")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ERPNext invoice dispatch failed for {payment_id}: {e}; it will be retried")
        await _note_invoice(payment_id, recipient_id, amount, "failed")
        return "failed"

    # Existing local ReportLab receipt (unchanged numbering and content).
    try:
        inv_suffix = payment_id[-4:] if len(payment_id) >= 4 else "1042"
        inv_number = f"Invoice_MoraaStudio_{inv_suffix}"
        extra = {"description": pack_description(context["white"], context["creative"])} if is_pack else {}
        # ReportLab rendering is CPU work: keep it off the event loop.
        pdf_bytes = await run_cpu(local_pdf_fn, customer_name=customer_name, invoice_number=inv_number, amount=amount,
                                  **extra)
        sent, drive = await _send_invoice(recipient_id, context["customer_id"], pdf_bytes, inv_number, send_document_fn)
        if sent is False:                    # the send reports False when WhatsApp refused it: let the outbox retry
            logger.warning(f"Local invoice for {payment_id} could not be sent")
            return "failed"
        await _note_invoice(payment_id, recipient_id, amount, "sent", None, drive)
        return "local"
    except Exception as e:  # noqa: BLE001
        logger.error(f"Local invoice dispatch failed for {payment_id}: {e}")
        return "failed"
