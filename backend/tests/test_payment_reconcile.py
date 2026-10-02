"""Phase 2 / Q-3: payment reconcile sweeps.

WhatsApp Pay: backoff (next_check_at), ended orders re-checked for 24 h, never-checked orders first, one short
session per order. Razorpay links: every link we send is recorded and later asked about, so a payment whose
webhook never arrived is credited exactly once.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.api.routes import payment_routes
from app.database import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.razorpay_payment_link import RazorpayPaymentLink
from app.models.wallet_transaction import WalletTransaction
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.services import razorpay_link_reconcile as rlr
from app.services import razorpay_service
from app.services import whatsapp_pay_service as wps
from tests.db_support import make_engine
from tests.test_wallet_ledger import SENDER, assert_ledger_matches_balances, wallet_service  # noqa: F401

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _aware(value):
    """SQLite hands back times without a timezone; they are UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(concurrent=True)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def make_customer(self, balance=0):
        c = Customer(whatsapp_id=SENDER, full_name="T", business_name="B", gst_number="N/A", address="A",
                     wallet_balance=0, is_registered=True)
        self.db.add(c)
        self.db.commit()
        if balance:
            wallet_service.credit_wallet(self.db, SENDER, balance, kind="opening_balance")
        return c

    def balance(self):
        self.db.expire_all()
        return self.db.query(Customer).filter_by(whatsapp_id=SENDER).one().wallet_balance


def _captured(pay_id="pay_wa1", paise=50000):
    return {"status": "captured", "currency": "INR", "amount": {"value": paise, "offset": 100},
            "transactions": [{"status": "success", "pg_transaction_id": pay_id, "id": "order_wa1"}]}


class WhatsAppPaySweepTests(_Base):
    def make_order(self, ref, status="sent", age=timedelta(minutes=10), next_check=None, attempts=0):
        order = WhatsAppPaymentOrder(
            reference_id=ref, whatsapp_id=SENDER, amount_rupees=500, total_paise=50000, status=status,
            configuration_name="cfg", created_at=NOW() - age, next_check_at=next_check, check_attempts=attempts)
        self.db.add(order)
        self.db.commit()
        return order

    def sweep(self, lookup, **kw):
        with patch.object(wps, "lookup_payment", new=AsyncMock(side_effect=lookup)), \
             patch.object(wps, "_send_receipt", new=AsyncMock()):
            import asyncio
            return asyncio.run(wps.reconcile_pending_orders(self.db, session_factory=self.Session, **kw))

    def test_backoff_doubles_and_is_capped(self):
        delays = [wps.next_check_delay(n).total_seconds() for n in range(0, 8)]
        self.assertEqual(delays[:4], [120, 240, 480, 960])
        self.assertEqual(max(delays), 3600)

    def test_an_unpaid_order_is_rescheduled_and_not_looked_at_again_until_due(self):
        order = self.make_order("ref-a")
        calls = []

        async def lookup(cfg, ref):
            calls.append(ref)
            return {"status": "pending"}

        self.assertEqual(self.sweep(lookup), {"pending": 1})
        self.db.expire_all()
        row = self.db.get(WhatsAppPaymentOrder, order.id)
        self.assertEqual(row.check_attempts, 1)
        self.assertGreater(_aware(row.next_check_at), NOW())
        self.assertEqual(self.sweep(lookup), {})                  # not due yet: Meta is not asked again
        self.assertEqual(calls, ["ref-a"])

    def test_ended_orders_are_rechecked_for_24_hours_and_credited_if_paid(self):
        self.make_customer(0)
        for i, status in enumerate(("dispatch_failed", "failed", "expired")):
            self.make_order(f"ref-{status}", status=status)

        async def lookup(cfg, ref):
            return _captured(f"pay_{ref}") if ref == "ref-failed" else {"status": "pending"}

        self.sweep(lookup)
        self.assertEqual(self.balance(), 500)                      # only the failed order had really been paid
        self.db.expire_all()
        statuses = {o.reference_id: o.status for o in self.db.query(WhatsAppPaymentOrder).all()}
        self.assertEqual(statuses, {"ref-dispatch_failed": "dispatch_failed", "ref-failed": "captured",
                                    "ref-expired": "expired"})   # unpaid ended orders keep their status
        assert_ledger_matches_balances(self, self.db)

    def test_an_order_stuck_in_created_is_rechecked_and_credited_if_paid(self):
        self.make_customer(0)
        self.make_order("ref-created", status="created")

        async def lookup(cfg, ref):
            return _captured()

        self.sweep(lookup)
        self.assertEqual(self.balance(), 500)

    def test_an_unpaid_created_order_stays_created(self):
        order = self.make_order("ref-created2", status="created")
        self.sweep(AsyncMock(return_value={"status": "pending"}))
        self.db.expire_all()
        self.assertEqual(self.db.get(WhatsAppPaymentOrder, order.id).status, "created")

    def test_crediting_a_payment_that_was_parked_for_review_closes_the_review_row(self):
        from app.models.pending_payment import PendingPayment

        self.make_customer(0)
        self.make_order("ref-parked", status="failed")
        self.db.add(PendingPayment(payment_id="pay_wa1", amount_rupees=500, currency="INR",
                                   reason="whatsapp_pay_not_credited"))
        self.db.commit()

        async def lookup(cfg, ref):
            return _captured("pay_wa1")

        self.sweep(lookup)
        self.assertEqual(self.balance(), 500)
        self.db.expire_all()
        self.assertEqual(self.db.query(PendingPayment).one().status, "credited")   # nobody can credit it twice by hand

    def test_ended_orders_older_than_24_hours_are_left_alone(self):
        self.make_order("ref-old", status="failed", age=timedelta(hours=30))
        self.assertEqual(self.sweep(AsyncMock(return_value={"status": "pending"})), {})

    def test_never_checked_orders_go_before_orders_that_were_checked_before(self):
        self.make_order("ref-old-checked", age=timedelta(hours=3), next_check=NOW() - timedelta(minutes=1), attempts=3)
        self.make_order("ref-new", age=timedelta(minutes=5))
        seen = []

        async def lookup(cfg, ref):
            seen.append(ref)
            return {"status": "pending"}

        self.sweep(lookup, limit=1)
        self.assertEqual(seen, ["ref-new"])            # old failures can no longer starve new orders

    def test_a_paid_order_is_credited_once_even_if_swept_twice(self):
        self.make_customer(0)
        self.make_order("ref-p")
        async def paid(cfg, ref):
            return _captured()

        self.sweep(paid)
        self.assertEqual(self.balance(), 500)
        self.sweep(paid)
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.ref == "pay_wa1").count(), 1)


def _paid_link(link_id="plink_1", pay_id="pay_link1", paise=50000, currency="INR", status="paid"):
    return {"id": link_id, "status": status, "amount": paise, "currency": currency,
            "notes": {"whatsapp_id": SENDER},
            "payments": [{"payment_id": pay_id, "amount": paise, "status": "captured"}]}


class RazorpayLinkSweepTests(_Base):
    def setUp(self):
        super().setUp()
        patches = [
            patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"),
            patch.object(payment_routes, "dispatch_payment_invoice", new=AsyncMock()),
            patch.object(payment_routes.settings, "RAZORPAY_WEBHOOK_SECRET", ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_link(self, link_id="plink_1", age=timedelta(minutes=10), next_check=None, status="created"):
        row = RazorpayPaymentLink(link_id=link_id, whatsapp_id=SENDER, amount_rupees=500, status=status,
                                  created_at=NOW() - age, next_check_at=next_check, check_attempts=0)
        self.db.add(row)
        self.db.commit()
        return row

    def sweep(self, link):
        import asyncio
        with patch.object(rlr, "fetch_payment_link", new=AsyncMock(return_value=link)):
            return asyncio.run(rlr.reconcile_payment_links(self.Session))

    def row(self, link_id="plink_1"):
        self.db.expire_all()
        return self.db.query(RazorpayPaymentLink).filter_by(link_id=link_id).one()

    def test_creating_a_link_records_it(self):
        import asyncio
        link = {"id": "plink_new", "short_url": "https://rzp.io/x"}
        client = type("C", (), {"payment_link": type("P", (), {"create": staticmethod(lambda payload: link)})})()
        with patch.object(razorpay_service, "get_razorpay_client", return_value=client), \
             patch("app.database.SessionLocal", self.Session):
            url = asyncio.run(razorpay_service.create_recharge_payment_link("+91 98123 45678", "T", 500))
        self.assertEqual(url, "https://rzp.io/x")
        row = self.row("plink_new")
        self.assertEqual((row.whatsapp_id, row.amount_rupees, row.status), (SENDER, 500, "created"))

    def test_a_paid_link_whose_webhook_was_missed_is_credited_once(self):
        self.make_customer(0)
        self.make_link()
        self.assertEqual(self.sweep(_paid_link()), {"paid": 1})
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.row().status, "paid")
        self.assertEqual(self.sweep(_paid_link()), {})              # finished links are not asked about again
        self.assertEqual(self.balance(), 500)
        assert_ledger_matches_balances(self, self.db)

    def test_a_link_the_webhook_already_credited_is_not_credited_again(self):
        self.make_customer(500)
        wallet_service  # noqa: B018
        self.db.add(AuditLog(user_id=None, action="razorpay_payment_captured", resource_id="pay_link1",
                             resource_type="razorpay_payment", status="success"))
        self.db.commit()
        self.make_link()
        self.sweep(_paid_link())
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.row().status, "paid")

    def test_an_unpaid_link_is_rescheduled_with_backoff(self):
        self.make_link()
        self.assertEqual(self.sweep({"id": "plink_1", "status": "created", "payments": []}), {"unpaid": 1})
        row = self.row()
        self.assertEqual((row.status, row.check_attempts), ("created", 1))
        self.assertGreater(_aware(row.next_check_at), NOW())
        self.assertEqual(self.sweep({"id": "plink_1", "status": "created", "payments": []}), {})   # not due yet

    def test_a_failed_lookup_is_retried_later(self):
        self.make_link()
        self.assertEqual(self.sweep(None), {"lookup_failed": 1})
        self.assertEqual(self.row().check_attempts, 1)

    def test_expired_and_cancelled_links_stop_being_checked(self):
        self.make_link("plink_e")
        self.assertEqual(self.sweep({"id": "plink_e", "status": "expired", "payments": []}), {"closed": 1})
        self.assertEqual(self.row("plink_e").status, "expired")

    def test_a_link_in_another_currency_is_never_credited(self):
        self.make_customer(0)
        self.make_link()
        self.sweep(_paid_link(currency="USD"))
        self.assertEqual(self.balance(), 0)

    def test_a_link_that_could_not_be_credited_stays_open_for_another_try(self):
        self.make_customer(0)
        self.make_link()
        with patch.object(payment_routes, "process_razorpay_event", new=AsyncMock(return_value={"status": "credit_failed"})):
            self.assertEqual(self.sweep(_paid_link()), {"retry_later": 1})
        row = self.row()
        self.assertEqual((row.status, row.check_attempts), ("created", 1))

    def test_a_paid_link_with_no_payer_phone_is_parked_for_review_and_finished(self):
        from app.models.pending_payment import PendingPayment

        self.make_link()
        link = _paid_link()
        link["notes"] = {}
        self.assertEqual(self.sweep(link), {"paid": 1})
        self.db.expire_all()
        self.assertEqual([p.payment_id for p in self.db.query(PendingPayment).all()], ["pay_link1"])
        self.assertEqual(self.row().status, "paid")

    def test_a_failing_receipt_never_undoes_or_blocks_the_credit(self):
        self.make_customer(0)
        self.make_link()
        with patch.object(payment_routes, "dispatch_payment_invoice", new=AsyncMock(side_effect=RuntimeError("erp down"))):
            self.assertEqual(self.sweep(_paid_link()), {"paid": 1})
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.row().status, "paid")

    def test_links_older_than_the_window_are_closed_not_checked(self):
        self.make_link("plink_old", age=timedelta(days=5))
        self.assertEqual(self.sweep(_paid_link("plink_old")), {})
        self.assertEqual(self.row("plink_old").status, "expired")

    def test_brand_new_links_are_left_for_the_webhook(self):
        self.make_link(age=timedelta(seconds=30))
        self.assertEqual(self.sweep(_paid_link()), {})

    def test_fetch_needs_keys_and_never_raises(self):
        import asyncio
        with patch.object(rlr.settings, "RAZORPAY_KEY_ID", ""), patch.object(rlr.settings, "RAZORPAY_KEY_SECRET", ""):
            self.assertIsNone(asyncio.run(rlr.fetch_payment_link("plink_1")))

    def test_events_are_shaped_like_the_real_webhook(self):
        events = rlr.build_paid_events(_paid_link())
        self.assertEqual(len(events), 1)
        extracted = payment_routes.extract_payment_data(events[0])
        self.assertEqual(extracted, (SENDER, 500, "pay_link1"))
        self.assertEqual(payment_routes.extract_currency(events[0]), "INR")


if __name__ == "__main__":
    unittest.main()
