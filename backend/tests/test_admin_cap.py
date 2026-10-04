"""COST-2: team (ADMIN) orders have a daily ceiling of their own."""

import unittest
from unittest.mock import patch

from app.ai import image_generation_manager as igm
from app.config import settings


class AdminCapTests(unittest.TestCase):
    def setUp(self):
        igm._admin_day, igm._admin_count = None, 0
        self.addCleanup(lambda: (setattr(igm, "_admin_day", None), setattr(igm, "_admin_count", 0)))

    def test_slots_are_taken_until_the_team_ceiling_is_reached(self):
        with patch.object(settings, "MAX_ADMIN_GENERATIONS_PER_DAY", 7):
            self.assertIsNone(igm.reserve_admin_slots(6))
            self.assertIsNone(igm.reserve_admin_slots(1))
            self.assertIn("Team daily image limit", igm.reserve_admin_slots(1))

    def test_a_pack_that_does_not_fit_is_refused_whole(self):
        with patch.object(settings, "MAX_ADMIN_GENERATIONS_PER_DAY", 5):
            self.assertIsNotNone(igm.reserve_admin_slots(6))
            self.assertIsNone(igm.reserve_admin_slots(5))          # nothing was consumed by the refused try

    def test_released_slots_can_be_used_again(self):
        with patch.object(settings, "MAX_ADMIN_GENERATIONS_PER_DAY", 6):
            self.assertIsNone(igm.reserve_admin_slots(6))
            igm.release_generation_slots(6, igm.admin_spend_key())     # the routing the pack worker uses
            self.assertIsNone(igm.reserve_admin_slots(6))

    def test_zero_means_no_team_ceiling(self):
        with patch.object(settings, "MAX_ADMIN_GENERATIONS_PER_DAY", 0):
            for _ in range(50):
                self.assertIsNone(igm.reserve_admin_slots(6))

    def test_the_kill_switch_still_applies(self):
        with patch.object(settings, "GENERATION_ENABLED", False):
            self.assertIn("disabled", igm.reserve_admin_slots(1))

    def test_team_orders_never_touch_the_customer_counter(self):
        igm._spend_day, igm._spend_count = None, 0
        with patch.object(settings, "MAX_ADMIN_GENERATIONS_PER_DAY", 100):
            igm.reserve_admin_slots(6)
        self.assertEqual(igm._spend_count, 0)

    def test_the_key_fits_the_counters_day_column(self):
        self.assertLessEqual(len(igm.admin_spend_key()), 10)
        self.assertNotEqual(igm.admin_spend_key(), igm.current_spend_day())


if __name__ == "__main__":
    unittest.main()
