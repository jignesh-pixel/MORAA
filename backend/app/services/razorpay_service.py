from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.utils.executors import run_io
from app.utils.logger import logger

# Fallback link used only if the live Razorpay Payment Links API call fails
# (e.g. credentials missing/invalid, transient API error) -- recharge must
# never break even when personalization can't be created.
ACTIVE_PAYMENT_URL = settings.RECHARGE_PAYMENT_URL

RAZORPAY_PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links"


async def _post_payment_link(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Create a Razorpay Payment Link over async HTTP with a hard timeout (EXT-5).

    The Razorpay SDK has no timeout and ran on the shared thread pool; a slow Razorpay could hold a thread for
    minutes. Here the call is bounded by RAZORPAY_API_TIMEOUT_SECONDS and never touches a worker thread. The
    key and secret are used only for the Authorization header and are never logged. Returns the link object, or
    None on any failure (the caller then uses the static fallback link). Not retried: creating a link twice
    would create two links.
    """
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        logger.warning("Razorpay keys are not configured - using the fallback link")
        return None
    timeout = float(settings.RAZORPAY_API_TIMEOUT_SECONDS or 10.0)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            auth=httpx.BasicAuth(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
        ) as client:
            response = await client.post(RAZORPAY_PAYMENT_LINKS_URL, json=payload)
        if response.status_code not in (200, 201):
            logger.error(f"Razorpay payment link creation failed: status={response.status_code}")
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except httpx.TimeoutException:
        logger.error(f"Razorpay payment link creation timed out after {timeout:.0f}s")
        return None
    except Exception as e:  # noqa: BLE001 -- the recharge flow must never break on this
        logger.error(f"Razorpay payment link creation error: {type(e).__name__}")
        return None


def record_payment_link(link_id: str, customer_phone: str, amount_rupees: int) -> None:
    """Remember a payment link we created (own short session). Never raises: a recharge must not fail on this."""
    try:
        from app.database import SessionLocal
        from app.models.razorpay_payment_link import RazorpayPaymentLink
        from app.utils.phone import normalize_phone

        with SessionLocal() as db:
            if db.query(RazorpayPaymentLink.id).filter(RazorpayPaymentLink.link_id == link_id).first() is None:
                db.add(RazorpayPaymentLink(
                    link_id=link_id[:64],
                    whatsapp_id=normalize_phone(customer_phone)[:100],
                    amount_rupees=int(amount_rupees),
                ))
                db.commit()
    except Exception as e:
        logger.error(f"Could not record Razorpay payment link {link_id}: {e}")


async def create_recharge_payment_link(
    customer_phone: str,
    customer_name: str = "Customer",
    amount: int = 500,
) -> str:
    """Creates a personalized, amount-specific Razorpay Payment Link carrying
    notes.whatsapp_id so the payment webhook can reliably match it back to
    this customer. Falls back to the previously-used static link on any
    failure so the recharge flow never breaks."""
    link = await _post_payment_link(
        {
            "amount": int(amount) * 100,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Moraa GemVision wallet recharge - {customer_name}",
            "customer": {
                "name": customer_name or "Customer",
                "contact": customer_phone,
            },
            "notify": {"sms": False, "email": False},
            "notes": {"whatsapp_id": customer_phone},
        }
    )
    url = (link or {}).get("short_url")
    if url:
        logger.info(f"Created personalized Razorpay payment link for {customer_phone}")
        link_id = str((link or {}).get("id") or "")
        if link_id:
            # So the reconcile sweep can later ask Razorpay whether it was paid (best effort, own I/O pool).
            await run_io(record_payment_link, link_id, customer_phone, int(amount))
        return url
    if link is not None:
        logger.warning("Razorpay payment link response had no short_url - using fallback link")
    return ACTIVE_PAYMENT_URL
