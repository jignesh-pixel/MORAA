"""Post-payment invoice dispatch: ERPNext Sales Invoice PDF, local fallback.

Runs as a FastAPI BackgroundTask AFTER the wallet credit is committed and
the "Payment Received" text is sent, so it never adds webhook latency and
never touches money. Never raises.

    ERPNEXT_INVOICE_ENABLED true + configured + ERPNext succeeds
        -> submitted Sales Invoice (+ Payment Entry) PDF sent on WhatsApp
    anything else (disabled, not configured, error, timeout, send failure)
        -> the existing local ReportLab receipt, exactly as before
"""

import re
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.services.erpnext_service import get_erpnext_service
from app.utils.logger import logger

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
    return value if _GSTIN_RE.match(value) else None


async def dispatch_payment_invoice(
    recipient_id: str,
    payment_id: str,
    amount: int,
    customer_name: str,
    customer_snapshot: Optional[Dict[str, Any]],
    local_pdf_fn: Callable[..., bytes],
    send_document_fn: Callable[..., Any],
) -> str:
    """Send the invoice PDF for one captured payment. Returns "erpnext",
    "local" or "failed". ``local_pdf_fn`` / ``send_document_fn`` are passed in
    by the caller so its existing receipt behaviour is reused unchanged."""
    if settings.ERPNEXT_INVOICE_ENABLED:
        try:
            result = await get_erpnext_service().create_paid_invoice_pdf(
                whatsapp_id=recipient_id,
                customer_name=billing_name(customer_snapshot, recipient_id),
                gstin=billing_gstin(customer_snapshot),
                amount=amount,
                payment_id=payment_id,
            )
            if result:
                pdf_bytes, invoice_name = result
                if await send_document_fn(
                    recipient_id=recipient_id,
                    document_bytes=pdf_bytes,
                    filename=f"{invoice_name}.pdf",
                    caption="",
                ):
                    logger.info(f"ERPNext invoice {invoice_name} sent to {recipient_id} (payment={payment_id})")
                    return "erpnext"
                logger.warning(f"ERPNext invoice {invoice_name} could not be sent; sending local receipt")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ERPNext invoice dispatch failed for {payment_id}: {e}; sending local receipt")

    # Existing local ReportLab receipt (unchanged numbering and content).
    try:
        inv_suffix = payment_id[-4:] if len(payment_id) >= 4 else "1042"
        inv_number = f"Invoice_MoraaStudio_{inv_suffix}"
        pdf_bytes = local_pdf_fn(customer_name=customer_name, invoice_number=inv_number, amount=amount)
        await send_document_fn(
            recipient_id=recipient_id,
            document_bytes=pdf_bytes,
            filename=f"{inv_number}.pdf",
            caption="",
        )
        return "local"
    except Exception as e:  # noqa: BLE001
        logger.error(f"Local invoice dispatch failed for {payment_id}: {e}")
        return "failed"
