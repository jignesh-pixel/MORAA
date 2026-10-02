import asyncio

import razorpay
from app.config import settings
from app.utils.logger import logger

# Fallback link used only if the live Razorpay Payment Links API call fails
# (e.g. credentials missing/invalid, transient API error) -- recharge must
# never break even when personalization can't be created.
ACTIVE_PAYMENT_URL = settings.RECHARGE_PAYMENT_URL

_client = None


def get_razorpay_client():
    global _client
    if _client is None:
        _client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    return _client


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
    try:
        client = get_razorpay_client()
        link = await asyncio.to_thread(
            client.payment_link.create,
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
            },
        )
        url = (link or {}).get("short_url")
        if url:
            logger.info(f"Created personalized Razorpay payment link for {customer_phone}")
            link_id = str((link or {}).get("id") or "")
            if link_id:
                # So the reconcile sweep can later ask Razorpay whether it was paid (best effort).
                await asyncio.to_thread(record_payment_link, link_id, customer_phone, int(amount))
            return url
        logger.warning("Razorpay payment_link.create returned no short_url - using fallback link")
    except Exception as e:
        logger.error(f"Razorpay payment_link.create failed: {e} - using fallback link")

    return ACTIVE_PAYMENT_URL
