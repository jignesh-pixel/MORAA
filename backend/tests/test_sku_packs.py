"""Phase 8: SKU pack credits -- ledger, pack payments, reconcile, claw-back and pack links.

The credit ledger (customer_sku_credits) is separate from the rupee wallet: a pack payment grants credits once and
never changes wallet_balance, each order uses one credit, a failed order gives it back once, unused credits expire,
and a refunded pack payment takes its credits back (shortfall flagged). Runs on SQLite and PostgreSQL; the
concurrency test is PostgreSQL-only.
"""

import asyncio
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.api.routes import payment_routes
from app.config import settings
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.pending_payment import PendingPayment
from app.models.razorpay_payment_link import RazorpayPaymentLink
from app.models.sku_credit import ACTION_CLAWBACK, ACTION_CONSUME, ACTION_EXPIRE, ACTION_PURCHASE, CustomerSkuCredit
from app.models.wallet_transaction import WalletTransaction
from app.services import razorpay_link_reconcile as rlr
from app.services import razorpay_service, sku_packs
from tests.db_support import postgres, requires_postgres
from tests.test_razorpay_clawback import _dispute_event, _payment_event, _refund_event
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances

PACK_PAYMENT = "pay_Qpack0001"
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _pack_event(payment_id=PACK_PAYMENT, units="40", paise=80000, event="payment_link.paid", on_payment=False):
    """A pack payment as Razorpay sends it: our notes on the link (or, for payment.captured, on the payment)."""
    notes = {"whatsapp_id": SENDER, "purpose": "sku_pack", "units": units}
    payment = {"id": payment_id, "amount": paise, "currency": "INR", "status": "captured"}
    if on_payment:
        payment["notes"] = notes
        return {"event": event, "payload": {"payment": {"entity": payment}}}
    link = {"id": f"plink_pack{units}", "amount": paise, "currency": "INR", "status": "paid", "notes": notes}
    return {"event": event, "payload": {"payment": {"entity": payment}, "payment_link": {"entity": link}}}


class _SkuBase(LedgerTestBase):
    concurrent = True             # the reconcile sweep and expiry open their own sessions

    def customer(self, wallet=0, email=None):
        customer = self.make_customer(wallet)
        if email:
            customer.email = email
            self.db.commit()
        return customer

    def credits(self, customer):
        self.db.expire_all()
        return sku_packs.balance(self.db, customer.id)

    def wallet(self, customer):
        self.db.expire_all()
        return self.db.get(Customer, customer.id).wallet_balance

    def row(self, action, reference):
        self.db.expire_all()
        return self.db.query(CustomerSkuCredit).filter_by(action=action, reference_id=reference).one()

    def grant(self, customer, units, payment_id=PACK_PAYMENT, now=None):
        balance = sku_packs.grant_pack_credits(self.db, customer, units, payment_id, now=now)
        self.db.commit()
        return balance

    def consume(self, customer, ingestion_id):
        used = sku_packs.consume_credit(self.db, customer, ingestion_id)
        self.db.commit()
        return used


class CreditLedgerTests(_SkuBase):
    def test_grant_consume_refund_keep_balance_and_balance_after(self):
        customer = self.customer(wallet=300)
        self.assertEqual(self.grant(customer, 3), 3)
        self.assertTrue(self.consume(customer, "ing-a"))
        self.assertEqual(self.credits(customer), 2)
        self.assertTrue(sku_packs.refund_credit(self.db, "ing-a"))
        self.assertEqual(self.credits(customer), 3)
        self.assertEqual(self.row(ACTION_PURCHASE, PACK_PAYMENT).balance_after, 3)
        self.assertEqual(self.row(ACTION_CONSUME, "ing-a").balance_after, 2)
        self.assertEqual(self.row("refund", "ing-a").balance_after, 3)
        self.assertEqual(self.wallet(customer), 300)                     # credits never touch the wallet
        assert_ledger_matches_balances(self, self.db)

    def test_purchase_row_holds_the_validity(self):
        customer = self.customer()
        now = NOW()
        self.grant(customer, 5, now=now)
        row = self.row(ACTION_PURCHASE, PACK_PAYMENT)
        self.assertEqual((row.quantity, row.sku), (5, "white_bg"))
        self.assertEqual(sku_packs.valid_until(self.db, customer.id), now + timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS))

    def test_consume_stops_at_zero(self):
        customer = self.customer()
        self.grant(customer, 2)
        self.assertEqual([self.consume(customer, f"ing-{i}") for i in range(4)], [True, True, False, False])
        self.assertEqual(self.credits(customer), 0)
        self.assertEqual(self.db.query(CustomerSkuCredit).filter_by(action=ACTION_CONSUME).count(), 2)

    def test_customer_without_credits_cannot_consume(self):
        customer = self.customer(wallet=1000)
        self.assertFalse(self.consume(customer, "ing-a"))
        self.assertEqual(self.db.query(CustomerSkuCredit).count(), 0)

    def test_same_order_is_consumed_once(self):
        customer = self.customer()
        self.grant(customer, 5)
        self.assertTrue(self.consume(customer, "ing-a"))
        with self.assertRaises(IntegrityError):
            sku_packs.consume_credit(self.db, customer, "ing-a")
        self.db.rollback()
        self.assertEqual(self.credits(customer), 4)

    def test_same_payment_grants_once(self):
        customer = self.customer()
        self.grant(customer, 5)
        with self.assertRaises(IntegrityError):
            sku_packs.grant_pack_credits(self.db, customer, 5, PACK_PAYMENT)
        self.db.rollback()
        self.assertEqual(self.credits(customer), 5)

    def test_a_pack_must_add_credits(self):
        customer = self.customer()
        for units in (0, -3):
            with self.assertRaises(ValueError):
                sku_packs.grant_pack_credits(self.db, customer, units, f"pay_{units}")
        self.db.rollback()
        self.assertEqual(self.credits(customer), 0)

    def test_refund_only_after_a_consume_and_only_once(self):
        customer = self.customer()
        self.assertFalse(sku_packs.refund_credit(self.db, "ing-never"))
        self.grant(customer, 1)
        self.assertFalse(sku_packs.refund_credit(self.db, "ing-a"))      # not consumed yet
        self.assertTrue(self.consume(customer, "ing-a"))
        self.assertEqual(
            [sku_packs.refund_credit(self.db, "ing-a") for _ in range(3)], [True, False, False])
        self.assertEqual(self.credits(customer), 1)
        self.assertEqual(self.db.query(CustomerSkuCredit).filter_by(action="refund").count(), 1)

    def test_expired_credits_cannot_be_consumed(self):
        customer = self.customer()
        self.grant(customer, 3, now=NOW() - timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS + 1))
        self.assertFalse(self.consume(customer, "ing-a"))
        self.assertEqual(self.credits(customer), 3)

    def test_a_new_pack_extends_every_unused_credit(self):
        customer = self.customer()
        now = NOW()
        self.grant(customer, 3, payment_id="pay_old", now=now - timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS + 5))
        self.grant(customer, 1, payment_id="pay_new", now=now)
        self.assertEqual(sku_packs.valid_until(self.db, customer.id), now + timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS))
        self.assertTrue(self.consume(customer, "ing-a"))
        self.assertEqual(self.credits(customer), 3)

    def test_valid_until_is_none_without_a_purchase(self):
        customer = self.customer()
        self.assertIsNone(sku_packs.valid_until(self.db, customer.id))
        self.assertEqual(sku_packs.balance(self.db, customer.id), 0)

    def test_dates_are_shown_in_india_time(self):
        self.assertEqual(sku_packs.ist_date(datetime(2027, 1, 5, 20, 0, tzinfo=timezone.utc)), "6 Jan 2027")
        self.assertEqual(sku_packs.ist_date(datetime(2027, 1, 5, 12, 0)), "5 Jan 2027")


class ExpiryTests(_SkuBase):
    def expire(self, now=None):
        with patch("app.database.SessionLocal", self.Session):
            return sku_packs.expire_due_credits(now)

    def test_expired_credits_are_written_off_once(self):
        old = self.customer()
        fresh = self.make_customer(0, whatsapp_id="919800000002")
        bought = NOW() - timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS + 2)
        self.grant(old, 5, payment_id="pay_old", now=bought)
        self.consume_while_valid(old, "ing-a", bought)
        self.grant(fresh, 4, payment_id="pay_fresh")
        self.assertEqual(self.expire(), 1)
        self.assertEqual((self.credits(old), self.credits(fresh)), (0, 4))
        until = sku_packs.valid_until(self.db, old.id)
        row = self.row(ACTION_EXPIRE, f"exp:{old.id}:{until:%Y%m%d}")
        self.assertEqual((row.quantity, row.balance_after), (-4, 0))
        self.assertEqual(self.expire(), 0)                               # nothing left to expire
        self.assertEqual(self.db.query(CustomerSkuCredit).filter_by(action=ACTION_EXPIRE).count(), 1)

    def test_a_credit_given_back_after_expiry_expires_too(self):
        customer = self.customer()
        bought = NOW() - timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS + 2)
        self.grant(customer, 2, now=bought)
        self.consume_while_valid(customer, "ing-a", bought)
        self.assertEqual(self.expire(), 1)
        self.assertTrue(sku_packs.refund_credit(self.db, "ing-a"))      # the order failed after the expiry
        self.assertEqual(self.credits(customer), 1)
        self.assertEqual(self.expire(), 1)
        self.assertEqual(self.credits(customer), 0)

    def test_credits_still_valid_are_kept(self):
        customer = self.customer()
        self.grant(customer, 3)
        self.assertEqual(self.expire(), 0)
        self.assertEqual(self.expire(NOW() + timedelta(days=settings.SKU_CREDIT_VALIDITY_DAYS + 1)), 1)
        self.assertEqual(self.credits(customer), 0)

    def consume_while_valid(self, customer, ingestion_id, bought):
        with patch.object(sku_packs, "datetime") as fake:
            fake.now.return_value = bought + timedelta(days=1)
            self.assertTrue(self.consume(customer, ingestion_id))


class PackWebhookTests(_SkuBase):
    """The webhook (and process_razorpay_event): a pack payment grants credits once and leaves the wallet alone."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from app.main import app

        self.app = app
        app.dependency_overrides.clear()

        def _override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)
        self.texts = []

        async def capture_text(recipient_id, message_text, reply_to_message_id=None):
            self.texts.append(message_text)
            return True

        self.invoice = AsyncMock()
        self.followups = MagicMock()
        patches = [
            patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
            patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(side_effect=capture_text)),
            patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"),
            patch.object(payment_routes, "dispatch_payment_invoice", new=self.invoice),
            patch.object(sku_packs, "queue_followups", new=self.followups),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(app.dependency_overrides.clear)
        # The pack links this test "created" (only payments of our own links grant credits).
        from app.models.razorpay_payment_link import RazorpayPaymentLink

        for units, rupees in ((40, 800), (20, 400)):
            self.db.add(RazorpayPaymentLink(link_id=f"plink_pack{units}", whatsapp_id=SENDER, amount_rupees=rupees,
                                            purpose="sku_pack", units=units))
        self.db.commit()

    def post(self, payload):
        response = self.client.post("/api/payments/razorpay/webhook", content=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["status"]

    def payer(self):
        self.db.expire_all()
        return self.db.query(Customer).filter_by(whatsapp_id=SENDER).one()

    def test_duplicate_pack_webhook_grants_once_and_leaves_the_wallet_alone(self):
        customer = self.customer(wallet=300)
        self.assertEqual(self.post(_pack_event()), "ok")
        self.assertEqual(self.post(_pack_event()), "already_processed")
        self.assertEqual(self.post(_pack_event(event="payment.captured", on_payment=True)), "already_processed")
        self.assertEqual(self.credits(customer), 40)
        self.assertEqual(self.wallet(customer), 300)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.ref == PACK_PAYMENT).count(), 0)
        assert_ledger_matches_balances(self, self.db)
        details = json.loads(self.db.query(AuditLog).filter_by(action="razorpay_payment_captured").one().details)
        self.assertEqual((details["amount_paid"], details["purpose"], details["units"]), (800, "sku_pack", 40))
        self.followups.assert_called_once_with(customer.id)
        self.assertEqual(self.invoice.await_count, 1)
        self.assertEqual(self.invoice.await_args.kwargs["amount"], 800)

    def test_pack_receipt_shows_credits_validity_and_asks_for_the_email(self):
        customer = self.customer()
        self.grant(customer, 5, payment_id="pay_earlier")
        self.post(_pack_event(units="20", paise=40000))
        until = sku_packs.valid_until(self.db, customer.id)
        self.assertEqual(len(self.texts), 1)
        receipt = self.texts[0]
        self.assertIn("20 SKUs added to your account.", receipt)
        self.assertIn("SKUs available: 25", receipt)
        self.assertIn(f"Valid till: {sku_packs.ist_date(until)}", receipt)
        self.assertTrue(receipt.endswith(payment_routes.PACK_EMAIL_REQUEST_LINE))

    def test_customer_with_an_email_is_not_asked_for_it(self):
        self.customer(email="shop@example.com")
        self.post(_pack_event())
        self.assertNotIn(payment_routes.PACK_EMAIL_REQUEST_LINE, self.texts[0])

    def test_pack_notes_on_a_payment_that_is_not_our_link_are_parked_not_granted(self):
        customer = self.customer()
        self.assertEqual(self.post(_pack_event(payment_id="pay_x1", on_payment=True, event="payment.captured")),
                         "pack_unverified")
        self.assertEqual(self.post(_pack_event(payment_id="pay_x2", paise=100)), "pack_unverified")   # amount differs
        self.assertEqual(self.credits(customer), 0)

    def test_unknown_payer_gets_a_customer_row_with_an_empty_wallet(self):
        self.assertEqual(self.post(_pack_event()), "ok")
        payer = self.payer()
        self.assertEqual((payer.wallet_balance, payer.is_registered), (0, False))
        self.assertEqual(self.credits(payer), 40)
        self.assertEqual(self.db.query(WalletTransaction).count(), 0)    # no wallet ledger row for a pack

    def test_pack_with_unusable_units_is_parked_not_credited(self):
        self.customer()
        for i, units in enumerate(("abc", "0", "-5", "20000", "", "²")):
            self.assertEqual(self.post(_pack_event(payment_id=f"pay_bad{i}", units=units)), "pack_units_invalid")
        self.db.expire_all()
        self.assertEqual({p.reason for p in self.db.query(PendingPayment).all()}, {"bad_pack_units"})
        self.assertEqual(self.db.query(PendingPayment).count(), 6)
        self.assertEqual(self.db.query(CustomerSkuCredit).count(), 0)
        self.assertEqual(self.db.query(AuditLog).filter_by(action="razorpay_payment_captured").count(), 0)
        self.assertEqual(self.payer().wallet_balance, 0)

    def test_wallet_recharge_is_unchanged(self):
        customer = self.customer(wallet=100)
        self.assertEqual(self.post(_payment_event()), "ok")
        self.assertEqual(self.wallet(customer), 600)
        self.assertEqual(self.db.query(CustomerSkuCredit).count(), 0)
        self.assertEqual(self.texts, [payment_routes.PAYMENT_TIPS_MESSAGE.format(paid="500", balance="600")])
        details = json.loads(self.db.query(AuditLog).filter_by(action="razorpay_payment_captured").one().details)
        self.assertEqual(set(details), {"sender_id", "amount_paid", "currency", "auto_provisioned"})
        self.followups.assert_not_called()
        assert_ledger_matches_balances(self, self.db)

    # -- refunds and disputes of a pack payment ----------------------------------------------------------------

    def test_full_refund_takes_the_credits_back_once(self):
        customer = self.customer(wallet=200)
        self.post(_pack_event())
        self.assertEqual(self.post(_refund_event(payment_id=PACK_PAYMENT, amount=80000)), "clawed_back")
        self.assertEqual(self.post(_refund_event(payment_id=PACK_PAYMENT, amount=80000)), "already_processed")
        self.assertEqual(self.credits(customer), 0)
        self.assertEqual(self.wallet(customer), 200)
        row = self.row(ACTION_CLAWBACK, "rfnd_1")
        self.assertEqual((row.quantity, row.balance_after), (-40, 0))
        self.assertEqual(self.db.query(AuditLog).filter_by(action="razorpay_clawback").one().status, "success")

    def test_partial_refund_takes_its_share_rounded_up(self):
        customer = self.customer()
        self.post(_pack_event())
        self.assertEqual(self.post(_refund_event(payment_id=PACK_PAYMENT, amount=3000)), "clawed_back")   # ₹30 = 1.5 SKUs
        self.assertEqual(self.credits(customer), 38)

    def test_refund_of_used_credits_takes_what_is_left_and_flags_the_rest(self):
        customer = self.customer()
        self.post(_pack_event())
        for i in range(30):
            self.assertTrue(self.consume(customer, f"ing-{i}"))
        self.assertEqual(self.post(_refund_event(payment_id=PACK_PAYMENT, amount=80000)), "clawed_back_partial")
        self.assertEqual(self.credits(customer), 0)
        audit = self.db.query(AuditLog).filter_by(action="razorpay_clawback").one()
        self.assertEqual(audit.status, "pending")
        self.assertEqual((json.loads(audit.details)["taken"], json.loads(audit.details)["shortfall"]), (10, 30))

    def test_refunds_and_a_dispute_never_take_more_than_the_pack(self):
        customer = self.customer()
        self.grant(customer, 100, payment_id="pay_other")
        self.post(_pack_event())
        self.post(_refund_event("rfnd_1", payment_id=PACK_PAYMENT, amount=60000))       # 30 SKUs
        self.post(_dispute_event("disp_1", payment_id=PACK_PAYMENT, amount=80000))      # only 10 of the pack are left
        self.assertEqual(self.credits(customer), 100)
        self.db.expire_all()
        dispute = self.db.query(AuditLog).filter_by(action="razorpay_clawback", resource_id="disp_1").one()
        self.assertEqual(dispute.status, "pending")                     # asked for more than the pack
        self.assertEqual(json.loads(dispute.details)["taken"], 10)

    def test_wallet_refund_still_takes_rupees(self):
        customer = self.customer()
        self.post(_payment_event())
        self.assertEqual(self.post(_refund_event(amount=20000)), "clawed_back")
        self.assertEqual(self.wallet(customer), 300)
        self.assertEqual(self.db.query(CustomerSkuCredit).count(), 0)
        assert_ledger_matches_balances(self, self.db)


class PackReconcileTests(_SkuBase):
    """A pack link whose webhook never arrived is granted by the sweep, through the same once-only claim."""

    def setUp(self):
        super().setUp()
        patches = [
            patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"),
            patch.object(payment_routes, "dispatch_payment_invoice", new=AsyncMock()),
            patch.object(sku_packs, "queue_followups", new=MagicMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_link(self):
        self.db.add(RazorpayPaymentLink(link_id="plink_pack40", whatsapp_id=SENDER, amount_rupees=800, status="created",
                                        purpose="sku_pack", units=40, created_at=NOW() - timedelta(minutes=10)))
        self.db.commit()

    def paid_link(self, units="40"):
        return {"id": "plink_pack40", "status": "paid", "amount": 80000, "currency": "INR",
                "notes": {"whatsapp_id": SENDER, "purpose": "sku_pack", "units": units},
                "payments": [{"payment_id": PACK_PAYMENT, "amount": 80000, "status": "captured"}]}

    def sweep(self, link):
        with patch.object(rlr, "fetch_payment_link", new=AsyncMock(return_value=link)):
            return asyncio.run(rlr.reconcile_payment_links(self.Session))

    def link_status(self):
        self.db.expire_all()
        return self.db.query(RazorpayPaymentLink).filter_by(link_id="plink_pack40").one().status

    def test_missed_pack_webhook_is_granted_once_by_the_sweep(self):
        customer = self.customer()
        self.make_link()
        self.assertEqual(self.sweep(self.paid_link()), {"paid": 1})
        self.assertEqual((self.credits(customer), self.wallet(customer)), (40, 0))
        self.assertEqual(self.link_status(), "paid")
        self.assertEqual(self.sweep(self.paid_link()), {})
        self.assertEqual(self.credits(customer), 40)

    def test_sweep_after_the_webhook_grants_nothing_more(self):
        customer = self.customer()
        self.make_link()
        result = asyncio.run(payment_routes.process_razorpay_event(self.db, _pack_event(), BackgroundTasks()))
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(self.sweep(self.paid_link()), {"paid": 1})
        self.assertEqual(self.credits(customer), 40)
        self.assertEqual(self.db.query(CustomerSkuCredit).count(), 1)
        self.assertEqual(self.link_status(), "paid")

    def test_reconciled_link_with_bad_units_is_parked_and_finished(self):
        self.customer()
        self.make_link()
        self.assertEqual(self.sweep(self.paid_link(units="lots")), {"paid": 1})
        self.db.expire_all()
        self.assertEqual([p.reason for p in self.db.query(PendingPayment).all()], ["bad_pack_units"])
        self.assertEqual(self.link_status(), "paid")


class PackLinkTests(_SkuBase):
    def create(self, units, link=None):
        post = AsyncMock(return_value=link if link is not None else {"id": "plink_new", "short_url": "https://rzp.io/p"})
        with patch.object(razorpay_service, "_post_payment_link", new=post), patch("app.database.SessionLocal", self.Session):
            url = asyncio.run(razorpay_service.create_pack_payment_link(SENDER, "T", units))
        return url, post

    def test_40_skus_is_one_800_rupee_link_with_pack_notes_and_is_recorded(self):
        url, post = self.create(40)
        self.assertEqual(url, "https://rzp.io/p")
        payload = post.await_args.args[0]
        self.assertEqual(payload["amount"], 80000)
        self.assertEqual(payload["notes"], {
            "whatsapp_id": SENDER, "phone": SENDER, "purpose": "sku_pack", "units": "40", "total_skus": "40",
            "creative_packs": "0", "cart_summary": '{"white_bg":40,"creative_pack":0,"total":800}',
        })
        self.assertEqual(payload["description"], "Moraa Studio 40 SKUs")
        self.assertFalse(payload["accept_partial"])
        row = self.db.query(RazorpayPaymentLink).filter_by(link_id="plink_new").one()
        self.assertEqual((row.amount_rupees, row.purpose, row.units, row.whatsapp_id), (800, "sku_pack", 40, SENDER))

    def test_the_stored_price_is_used(self):
        from app.services import pricing

        pricing.set_sku_price(self.db, 25, "test")
        _, post = self.create(4)
        self.assertEqual(post.await_args.args[0]["amount"], 10000)

    def test_bad_units_create_no_link(self):
        for units in (0, -1, 10001, True, "5", 2.5):
            url, post = self.create(units)
            self.assertIsNone(url)
            post.assert_not_awaited()

    def test_no_static_fallback_when_razorpay_fails(self):
        url, _ = self.create(5, link={})
        self.assertIsNone(url)
        self.assertEqual(self.db.query(RazorpayPaymentLink).count(), 0)

    def test_recharge_link_defaults_to_the_minimum_recharge(self):
        post = AsyncMock(return_value={"id": "plink_r", "short_url": "https://rzp.io/r"})
        with patch.object(razorpay_service, "_post_payment_link", new=post), patch("app.database.SessionLocal", self.Session):
            asyncio.run(razorpay_service.create_recharge_payment_link(SENDER, "T"))
        self.assertEqual(post.await_args.args[0]["amount"], settings.MIN_RECHARGE_RUPEES * 100)
        self.assertNotIn("purpose", post.await_args.args[0]["notes"])
        row = self.db.query(RazorpayPaymentLink).filter_by(link_id="plink_r").one()
        self.assertEqual((row.purpose, row.units), (None, None))


@postgres
@requires_postgres
class CreditsUnderRealLocksTests(_SkuBase):
    def test_ten_orders_racing_for_three_credits_use_exactly_three(self):
        customer = self.customer()
        self.grant(customer, 3)
        results, errors = [], []
        barrier = threading.Barrier(10, timeout=120)

        def order(i):
            try:
                barrier.wait()
                with self.Session() as db:
                    used = sku_packs.consume_credit(db, db.get(Customer, customer.id), f"ing-{i}")
                    db.commit()
                    results.append(used)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=order, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual((results.count(True), results.count(False)), (3, 7))
        self.assertEqual(self.credits(customer), 0)
        after = sorted(r.balance_after for r in self.db.query(CustomerSkuCredit).filter_by(action=ACTION_CONSUME))
        self.assertEqual(after, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
