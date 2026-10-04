import unittest

from app.services.gst_places import is_inter_state, place_of_supply, state_code_from_gstin


class GstPlaceTests(unittest.TestCase):
    def test_state_from_gstin(self):
        self.assertEqual(state_code_from_gstin("27AAAPS1234C1Z5"), "27")
        self.assertIsNone(state_code_from_gstin("99AAAPS1234C1Z5"))
        self.assertIsNone(state_code_from_gstin(None))
        self.assertEqual(place_of_supply("29AAAPS1234C1Z5"), "29-Karnataka")

    def test_inter_state(self):
        self.assertTrue(is_inter_state("27", "24AAAPS1234C1Z5"))
        self.assertFalse(is_inter_state("27", "27AAAPS1234C1Z5"))
        self.assertFalse(is_inter_state("", "24AAAPS1234C1Z5"))
        self.assertFalse(is_inter_state("27", None))


if __name__ == "__main__":
    unittest.main()
