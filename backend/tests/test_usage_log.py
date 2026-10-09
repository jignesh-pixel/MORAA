"""Phase 8 Usage Logs sheet: a view of customer_sku_credits, appended in one call and rebuilt on demand."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.config import settings
from app.models.sku_credit import (
    ACTION_CONSUME,
    ACTION_PURCHASE,
    ACTION_REFUND,
    SKU_WHITE_BG,
    CustomerSkuCredit,
)
from app.services import drive_layout, usage_log
from tests.test_google_drive import DriveDbTestBase

START = datetime(2026, 10, 7, 4, 30, tzinfo=timezone.utc)          # 10:00 IST


class UsageLogTests(DriveDbTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.customer_id = self.make_customer()
        self.minute = 0

    def ledger(self, action: str, quantity: int, balance_after: int, reference: str) -> None:
        with self.Session() as db:
            db.add(CustomerSkuCredit(customer_id=self.customer_id, sku=SKU_WHITE_BG, action=action, quantity=quantity,
                                     balance_after=balance_after, reference_id=reference,
                                     created_at=START + timedelta(minutes=self.minute)))
            db.commit()
        self.minute += 1

    def sheet(self) -> list:
        return self.google.sheets[self.customer(self.customer_id).drive_sheet_id]

    def appends(self) -> int:
        return self.google.count("POST", ":append")

    def test_rows_show_the_event_images_used_and_balance_from_the_ledger(self):
        self.ledger(ACTION_PURCHASE, 5, 5, "pay_1")
        self.ledger(ACTION_CONSUME, -1, 4, "ing_1")
        self.ledger(ACTION_CONSUME, -1, 3, "ing_2")
        self.ledger(ACTION_REFUND, 1, 4, "ing_2")
        with self.Session() as db:
            rows = usage_log.rows_for(db, self.customer_id)
        self.assertEqual(rows[0][1:], ["Account registered", 0, 0])
        self.assertEqual(rows[1:], [
            ["07 Oct 2026 10:00", "Pack purchased (5 SKUs)", 0, 5],
            ["07 Oct 2026 10:01", "Image created", 1, 4],
            ["07 Oct 2026 10:02", "Image created", 1, 3],
            ["07 Oct 2026 10:03", "Credit returned (failed image)", -1, 4],
        ])

    def test_sync_appends_only_the_missing_rows_in_one_call(self):
        self.ledger(ACTION_PURCHASE, 5, 5, "pay_1")
        asyncio.run(drive_layout.ensure_customer_folders(self.customer_id))      # writes header + rows so far
        self.assertEqual(self.sheet()[0], drive_layout.USAGE_LOG_HEADER)
        self.assertEqual([r[1] for r in self.sheet()[1:]], ["Account registered", "Pack purchased (5 SKUs)"])
        before = self.appends()
        self.assertEqual(asyncio.run(usage_log.sync(self.customer_id)), 0)   # nothing written twice
        self.assertEqual(self.appends(), before)
        self.ledger(ACTION_CONSUME, -1, 4, "ing_1")
        self.assertEqual(asyncio.run(usage_log.sync(self.customer_id)), 1)
        self.assertEqual(self.appends(), before + 1)
        self.assertEqual(len(self.sheet()), 4)

        self.ledger(ACTION_CONSUME, -1, 3, "ing_2")
        self.assertEqual(asyncio.run(usage_log.sync(self.customer_id)), 1)
        self.assertEqual(self.appends(), before + 2)
        self.assertEqual(self.sheet()[-1][1:], ["Image created", 1, 3])

        self.assertEqual(asyncio.run(usage_log.sync(self.customer_id)), 0)   # up to date: nothing appended
        self.assertEqual(self.appends(), before + 2)
        self.assertEqual(len(self.sheet()), 5)

    def test_rebuild_rewrites_the_header_and_every_row(self):
        self.ledger(ACTION_PURCHASE, 5, 5, "pay_1")
        self.ledger(ACTION_CONSUME, -1, 4, "ing_1")
        asyncio.run(usage_log.sync(self.customer_id))
        self.sheet()[1] = ["someone typed here"]
        self.sheet().append(["and here"])
        self.assertEqual(asyncio.run(usage_log.rebuild(self.customer_id)), 3)
        with self.Session() as db:
            expected = [drive_layout.USAGE_LOG_HEADER] + usage_log.rows_for(db, self.customer_id)
        self.assertEqual(self.sheet(), expected)

    def test_queue_sync_does_nothing_when_drive_delivery_is_off(self):
        with patch.object(settings, "DRIVE_DELIVERY_ENABLED", False), \
             patch("app.services.outbox.enqueue_job") as enqueue:
            usage_log.queue_sync(self.customer_id)
        enqueue.assert_not_called()
        with patch("app.services.outbox.enqueue_job") as enqueue:
            usage_log.queue_sync(self.customer_id)
        self.assertEqual(enqueue.call_args.args[:2], (usage_log.OUTBOX_KIND, {"customer_id": self.customer_id}))


if __name__ == "__main__":
    unittest.main()
