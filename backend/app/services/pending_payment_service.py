"""Parking and resolving Razorpay payments that have no usable payer phone (MON-11).

See ``app/models/pending_payment.py``. Money moves only in ``credit_pending_payment``, and there it moves
through the same once-only claim (audit row ``razorpay_payment_captured`` + ledger ``credit_payment``) as a
normal payment, so a payment can never be credited twice, whichever path reaches it first.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.pending_payment import STATUS_CREDITED, STATUS_PENDING, PendingPayment
from app.services.wallet_service import credit_wallet, find_customer_by_phone
from app.utils.logger import logger, mask_phone

AUDIT_ACTION_PAYMENT_CAPTURED = "razorpay_payment_captured"      # must equal payment_routes' constant
AUDIT_ACTION_PAYMENT_UNMATCHED = "razorpay_payment_unmatched"
AUDIT_RESOURCE_TYPE = "razorpay_payment"


def record_pending_payment(
    db: Session,
    *,
    payment_id: str,
    amount_rupees: int,
    currency: str,
    event: str,
    payer_hint: str,
    reason: str,
) -> bool:
    """Park a payment for review, once per ``payment_id``. True when a new row was written. Never raises."""
    try:
        if db.query(PendingPayment.id).filter(PendingPayment.payment_id == payment_id).first() is not None:
            return False
        db.add(
            PendingPayment(
                payment_id=payment_id,
                amount_rupees=int(amount_rupees),
                currency=(currency or "INR")[:8],
                event=(event or "")[:40],
                payer_hint=(payer_hint or "")[:100] or None,
                reason=reason,
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    except Exception as e:
        db.rollback()
        logger.error(f"Could not park pending payment {payment_id}: {e}")
        return False


def mark_pending_credited(db: Session, payment_id: str, whatsapp_id: str) -> None:
    """Close a parked payment because another event credited it. Runs in the caller's transaction (no commit)."""
    db.query(PendingPayment).filter(
        PendingPayment.payment_id == payment_id, PendingPayment.status == STATUS_PENDING
    ).update(
        {
            PendingPayment.status: STATUS_CREDITED,
            PendingPayment.credited_whatsapp_id: whatsapp_id,
            PendingPayment.resolved_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )


def credit_pending_payment(db: Session, payment_id: str, phone: str) -> Dict[str, Any]:
    """Credit a parked payment to the customer with ``phone`` (must already exist). Once only.

    Returns ``{"status": "credited" | "already_credited" | "unknown_payment" | "unknown_customer", ...}``.
    """
    pending: Optional[PendingPayment] = db.query(PendingPayment).filter(PendingPayment.payment_id == payment_id).first()
    if pending is None:
        return {"status": "unknown_payment"}
    if pending.status == STATUS_CREDITED:
        return {"status": "already_credited"}
    if pending.status != STATUS_PENDING or (pending.currency or "").upper() != "INR":
        return {"status": "not_creditable", "state": pending.status, "currency": pending.currency}
    customer = find_customer_by_phone(db, phone)
    if customer is None:
        return {"status": "unknown_customer"}

    amount = int(pending.amount_rupees)
    try:
        # Claim first, in the same transaction as the credit: a repeat or a concurrent payment event fails here.
        db.add(
            AuditLog(
                user_id=None,
                action=AUDIT_ACTION_PAYMENT_CAPTURED,
                resource_id=payment_id,
                resource_type=AUDIT_RESOURCE_TYPE,
                status="success",
                details=json.dumps({"sender_id": customer.whatsapp_id, "amount_paid": amount, "currency": "INR",
                                    "manual_from_pending": True}),
            )
        )
        db.flush()
        if credit_wallet(db, customer.whatsapp_id, amount, commit=False, ref=payment_id) != 1:
            raise RuntimeError("customer row not updated")
        mark_pending_credited(db, payment_id, customer.whatsapp_id)
        db.query(AuditLog).filter(
            AuditLog.action == AUDIT_ACTION_PAYMENT_UNMATCHED,
            AuditLog.resource_id == payment_id,
            AuditLog.status == "pending",
        ).update({AuditLog.status: "resolved"}, synchronize_session=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.query(AuditLog.id).filter(
            AuditLog.action == AUDIT_ACTION_PAYMENT_CAPTURED, AuditLog.resource_id == payment_id
        ).first() is None:
            raise        # not a repeat of this payment: some other constraint refused it, so nothing is closed
        logger.warning(f"Pending payment {payment_id} was already credited by another path.")
        db.query(PendingPayment).filter(
            PendingPayment.payment_id == payment_id, PendingPayment.status == STATUS_PENDING
        ).update({PendingPayment.status: STATUS_CREDITED, PendingPayment.resolved_at: datetime.now(timezone.utc),
                  PendingPayment.note: "credited by another path"}, synchronize_session=False)
        db.commit()
        return {"status": "already_credited"}
    except Exception:
        db.rollback()
        raise
    logger.info(f"Pending payment {payment_id} (₹{amount}) credited to {mask_phone(customer.whatsapp_id)}")
    return {"status": "credited", "amount": amount, "whatsapp_id": customer.whatsapp_id}
