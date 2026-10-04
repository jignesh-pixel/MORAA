"""UX-2: honest delivery estimates."""

import unittest

from app.services import eta_service

DEFAULT = "Please allow 20-30 seconds."


class EtaTests(unittest.TestCase):
    def test_before_enough_orders_are_measured_the_original_wording_is_kept(self):
        eta_service.record_duration("pack", 100)
        eta_service.record_duration("pack", 100)
        self.assertEqual(eta_service.ack_suffix("pack", 0, None, DEFAULT), DEFAULT)

    def test_the_estimate_follows_the_median_of_recent_orders(self):
        for seconds in (60, 70, 65, 400, 62):             # one slow outlier does not move the median
            eta_service.record_duration("pack", seconds)
        self.assertEqual(eta_service.typical_seconds("pack"), 65)
        self.assertEqual(eta_service.ack_suffix("pack", 0, None, DEFAULT), "Usually ready in about 2 minutes.")

    def test_a_quick_product_says_under_a_minute(self):
        for seconds in (15, 20, 18):
            eta_service.record_duration("white_bg", seconds)
        self.assertEqual(eta_service.ack_suffix("white_bg", 0, None, DEFAULT), "Usually ready in under a minute.")

    def test_orders_ahead_lengthen_the_estimate_only_when_the_server_runs_a_limited_number_at_once(self):
        for seconds in (60, 60, 60):
            eta_service.record_duration("pack", seconds)
        self.assertEqual(eta_service.estimate_seconds("pack", 10, None), 60)          # no limit: nobody waits
        self.assertEqual(eta_service.estimate_seconds("pack", 2, 2), 120)             # two ahead, two at a time
        self.assertEqual(eta_service.estimate_seconds("pack", 1, 2), 60)              # still a free slot

    def test_a_long_queue_is_mentioned(self):
        for seconds in (60, 60, 60):
            eta_service.record_duration("pack", seconds)
        self.assertIn("5 orders ahead", eta_service.ack_suffix("pack", 5, 5, DEFAULT))

    def test_nonsense_durations_are_ignored(self):
        for seconds in (-1, 0, 99999):
            eta_service.record_duration("pack", seconds)
        self.assertIsNone(eta_service.typical_seconds("pack"))

    def test_products_are_measured_separately(self):
        for seconds in (200, 200, 200):
            eta_service.record_duration("pack", seconds)
        self.assertIsNone(eta_service.typical_seconds("white_bg"))


if __name__ == "__main__":
    unittest.main()
