"""Tiered access: ADMIN / TRIAL / STANDARD customers.

Managed from the Supabase table editor (``customers`` table):

* ADMIN -- ``tier = 'ADMIN'`` or ``bypass_payment = true``. Never asked to
  pay, never charged, not limited by MAX_GENERATIONS_PER_DAY (the
  GENERATION_ENABLED kill switch still applies).
* TRIAL -- ``tier = 'TRIAL'`` with ``trial_credits_total`` complimentary
  orders. One credit is used per successfully delivered order (Clean Studio
  Shot or E-Com Pack 1; costs in settings). ``allowed_shots`` limits which
  products the credits cover: "all", "white_bg", "pack_1". When the credits
  run out the customer is treated exactly like STANDARD.
* STANDARD -- pay-as-you-go wallet (unchanged).

Wallet money is never touched here.
"""

import json
from typing import Any, Iterable, List, Optional

from app.config import settings
from app.utils.logger import logger

TIER_ADMIN = "ADMIN"
TIER_TRIAL = "TRIAL"
TIER_STANDARD = "STANDARD"

SHOT_ALL = "all"
PRODUCT_SHOT_KEYS = {"WHITE_BG": "white_bg", "PACK_1": "pack_1"}
PRODUCT_LABELS = {"WHITE_BG": "Clean Studio Shot", "PACK_1": "Full Catalog Pack"}

TRIAL_CREDIT_AUDIT_ACTION = "trial_credit_used"
# Orders that are paid by a trial credit but not finished yet.
_PENDING_STATUSES = ("white_queued", "pack_queued", "processing", "generated")

EXHAUSTED_TEMPLATE = (
    "You have used all your complimentary test credits ({used}/{total}). To continue "
    "generating studio-quality renders, recharge your wallet below:"
)


def _tier(customer: Any) -> str:
    return str(getattr(customer, "tier", None) or TIER_STANDARD).strip().upper()


def is_admin(customer: Any) -> bool:
    if customer is None:
        return False
    return _tier(customer) == TIER_ADMIN or bool(getattr(customer, "bypass_payment", False))


def is_trial(customer: Any) -> bool:
    return customer is not None and not is_admin(customer) and _tier(customer) == TIER_TRIAL


def trial_remaining(customer: Any) -> int:
    total = int(getattr(customer, "trial_credits_total", 0) or 0)
    used = int(getattr(customer, "trial_credits_used", 0) or 0)
    return max(total - used, 0)


def credit_cost(product_code: str) -> int:
    if product_code == "PACK_1":
        return max(int(settings.TRIAL_CREDITS_PER_PACK_1), 1)
    return max(int(settings.TRIAL_CREDITS_PER_WHITE_BG), 1)


def _shots(customer: Any) -> List[str]:
    raw = getattr(customer, "allowed_shots", None)
    if isinstance(raw, str):  # tolerate a JSON string typed into the table editor
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    if not raw:
        return [SHOT_ALL]
    if not isinstance(raw, Iterable):
        return [SHOT_ALL]
    return [str(s).strip().lower() for s in raw if str(s).strip()] or [SHOT_ALL]


def trial_shot_allowed(customer: Any, product_code: str) -> bool:
    shots = _shots(customer)
    return SHOT_ALL in shots or PRODUCT_SHOT_KEYS.get(product_code, "") in shots


def allowed_product_labels(customer: Any) -> List[str]:
    shots = _shots(customer)
    if SHOT_ALL in shots:
        return list(PRODUCT_LABELS.values())
    return [PRODUCT_LABELS[p] for p, key in PRODUCT_SHOT_KEYS.items() if key in shots]


def trial_can_use(customer: Any, product_code: str) -> bool:
    return (
        is_trial(customer)
        and trial_shot_allowed(customer, product_code)
        and trial_remaining(customer) >= credit_cost(product_code)
    )


def has_trial_credits(customer: Any) -> bool:
    return is_trial(customer) and trial_remaining(customer) >= 1


def payment_exempt(customer: Any) -> bool:
    """No recharge prompt / wallet check: team members and trials with credits."""
    return is_admin(customer) or has_trial_credits(customer)


def trial_credits_available(db, customer: Any, product_code: str, exclude_ingestion_id: Optional[str] = None) -> bool:
    """trial_can_use, minus credits already committed to unfinished trial orders
    (so two quick taps cannot spend the same last credit)."""
    if not trial_can_use(customer, product_code):
        return False
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    query = db.query(WhatsAppIngestion.product_code).filter(
        WhatsAppIngestion.external_user_id == customer.whatsapp_id,
        WhatsAppIngestion.amount_charged == 0,
        WhatsAppIngestion.status.in_(_PENDING_STATUSES),
    )
    if exclude_ingestion_id:
        query = query.filter(WhatsAppIngestion.id != exclude_ingestion_id)
    pending = sum(credit_cost(code or "") for (code,) in query.all())
    return trial_remaining(customer) - pending >= credit_cost(product_code)


async def record_trial_success(db, ingestion: Any) -> None:
    """Meter one delivered trial order (amount_charged == 0). Exactly once per
    order (audit row). On the last credit, send the exhaustion recharge card.
    Never raises."""
    try:
        if ingestion is None or getattr(ingestion, "amount_charged", None) != 0:
            return
        from app.models.audit_log import AuditLog
        from app.models.customer import Customer
        from app.services.wallet_service import find_customer_by_phone

        customer = find_customer_by_phone(db, ingestion.external_user_id)
        if not is_trial(customer):
            return
        already = (
            db.query(AuditLog.id)
            .filter(AuditLog.action == TRIAL_CREDIT_AUDIT_ACTION, AuditLog.resource_id == ingestion.id)
            .first()
        )
        if already:
            return
        cost = credit_cost(ingestion.product_code or "")
        db.query(Customer).filter(Customer.id == customer.id).update(
            {Customer.trial_credits_used: Customer.trial_credits_used + cost},
            synchronize_session=False,
        )
        db.add(AuditLog(
            action=TRIAL_CREDIT_AUDIT_ACTION,
            resource_id=ingestion.id,
            resource_type="whatsapp_ingestion",
            status="success",
            details=json.dumps({"whatsapp_id": customer.whatsapp_id, "credits": cost,
                                "product": ingestion.product_code}),
        ))
        db.commit()
        db.refresh(customer)
        logger.info(
            f"Trial credit used: whatsapp_id={customer.whatsapp_id} "
            f"used={customer.trial_credits_used}/{customer.trial_credits_total}"
        )
        if trial_remaining(customer) <= 0:
            await send_trial_exhausted_card(db, customer, reply_to_message_id=ingestion.external_message_id)
    except Exception as e:  # noqa: BLE001 -- metering must never break delivery
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Trial metering failed: ingestion_id={getattr(ingestion, 'id', None)} error={e}")


async def send_trial_exhausted_card(db, customer: Any, reply_to_message_id: Optional[str] = None) -> None:
    """Exhaustion alert + the standard recharge card (native WhatsApp Pay if
    enabled, else the Razorpay link CTA)."""
    from app.services import meta_whatsapp_service as mws
    from app.services import razorpay_service
    from app.services.whatsapp_pay_service import try_send_native_recharge

    total = int(customer.trial_credits_total or 0)
    body = EXHAUSTED_TEMPLATE.format(used=total, total=total)
    amount = 500
    try:
        if await try_send_native_recharge(db, customer.whatsapp_id, amount, body,
                                          reply_to_message_id=reply_to_message_id):
            return
    except Exception as e:  # noqa: BLE001
        logger.error(f"Native recharge for trial exhaustion failed: {e}")
    try:
        url = await razorpay_service.create_recharge_payment_link(
            customer_phone=customer.whatsapp_id,
            customer_name=customer.full_name or "Customer",
            amount=amount,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Trial exhaustion recharge link failed: {e}")
        url = settings.RECHARGE_PAYMENT_URL
    await mws.send_whatsapp_cta_url_button(
        recipient_id=customer.whatsapp_id,
        body_text=body,
        button_label="Recharge Wallet",
        url=url or settings.RECHARGE_PAYMENT_URL,
    )
