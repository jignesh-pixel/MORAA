"""Earring on Luxury Drape shot (E-Com Pack 1 slot 8): builder, adaptive drape selection and pack wiring."""

import unittest

from app import config
from app.services import meta_whatsapp_service as mws
from app.services import sku_messages
from app.services.earring_ecommerce_prompt import (
    ANTI_SYMMETRY_INSTRUCTION,
    COLOUR_LOCK_INSTRUCTION,
    EARRING_TYPE_PRESERVATION,
    GENERIC_EARRING_PRESERVATION,
    MATERIAL_FIDELITY_INSTRUCTION,
)
from app.services.earring_luxury_drape_shot import (
    DRAPE_INSTRUCTIONS,
    DRAPE_SHOT_NEGATIVES,
    GENERIC_RESTING_RULE,
    REFERENCE_PRIORITY_MARKER,
    RESTING_RULES,
    VALID_DRAPES,
    build_luxury_drape_prompt,
    normalise_metal_tone,
    normalise_stone_family,
    select_drape,
)
from app.services.earring_macro_shot_prompt import METAL_AFFIRMATION_INSTRUCTION
from app.services.earring_scale_reference_prompt import (
    BEAD_CLUSTER_PRESERVATION_INSTRUCTION,
    NEGATIVE_SPACE_INSTRUCTION,
)


class LuxuryDrapeBuilderTests(unittest.TestCase):
    def setUp(self):
        self.prompt = build_luxury_drape_prompt()

    def test_reference_priority_marker_appears_exactly_once(self):
        self.assertEqual(self.prompt.count(REFERENCE_PRIORITY_MARKER), 1)

    def test_zero_argument_call_is_the_adaptive_generic_prompt(self):
        self.assertEqual(self.prompt, build_luxury_drape_prompt(None, metal_tone=None, gemstone=None))
        for block in (DRAPE_INSTRUCTIONS["auto"], GENERIC_EARRING_PRESERVATION, GENERIC_RESTING_RULE,
                      DRAPE_SHOT_NEGATIVES):
            self.assertIn(block, self.prompt)

    def test_shared_fidelity_blocks_are_included(self):
        for block in (ANTI_SYMMETRY_INSTRUCTION, COLOUR_LOCK_INSTRUCTION, MATERIAL_FIDELITY_INSTRUCTION,
                      METAL_AFFIRMATION_INSTRUCTION, NEGATIVE_SPACE_INSTRUCTION,
                      BEAD_CLUSTER_PRESERVATION_INSTRUCTION):
            self.assertIn(block, self.prompt)

    def test_exactly_one_drape_block_is_present(self):
        for metal in (None, "silver", "yellow gold", "rose gold"):
            prompt = build_luxury_drape_prompt(metal_tone=metal)
            present = [key for key in VALID_DRAPES if DRAPE_INSTRUCTIONS[key] in prompt]
            self.assertEqual(len(present), 1, (metal, present))

    def test_earring_type_selects_type_rules_and_unknown_type_falls_back(self):
        for earring_type in ("Hoop", "Stud", "Dangle"):
            prompt = build_luxury_drape_prompt(earring_type)
            self.assertIn(EARRING_TYPE_PRESERVATION[earring_type], prompt)
            self.assertIn(RESTING_RULES[earring_type], prompt)
            self.assertNotIn(GENERIC_RESTING_RULE, prompt)
        self.assertEqual(build_luxury_drape_prompt("Chandelier"), self.prompt)

    def test_prompt_forbids_the_slab_and_covering_the_earring(self):
        self.assertIn("travertine", DRAPE_SHOT_NEGATIVES)
        self.assertIn("Do NOT cover", DRAPE_SHOT_NEGATIVES)
        self.assertIn("hands", DRAPE_SHOT_NEGATIVES)


class AdaptiveDrapeTests(unittest.TestCase):
    def test_silver_tones_get_champagne_silk(self):
        for metal in ("silver", "Sterling Silver", "white gold", "White-Gold", "platinum", "rhodium plated"):
            self.assertEqual(select_drape(metal), "champagne_silk", metal)

    def test_gold_gets_emerald_velvet_and_burgundy_when_the_stones_are_green(self):
        for metal in ("gold", "Yellow Gold", "brass", "antique gold"):
            self.assertEqual(select_drape(metal), "emerald_velvet", metal)
            self.assertEqual(select_drape(metal, "emerald"), "burgundy_velvet", metal)
            self.assertEqual(select_drape(metal, "green onyx"), "burgundy_velvet", metal)
            self.assertEqual(select_drape(metal, "ruby"), "emerald_velvet", metal)
            self.assertEqual(select_drape(metal, "diamond"), "emerald_velvet", metal)

    def test_rose_gold_gets_blush_satin_and_is_not_mistaken_for_plain_gold(self):
        for metal in ("rose gold", "Rose-Gold", "pink gold", "rose_gold plated", "copper"):
            self.assertEqual(select_drape(metal), "blush_satin", metal)

    def test_silver_ignores_the_gemstone(self):
        self.assertEqual(select_drape("silver", "emerald"), "champagne_silk")
        self.assertEqual(select_drape("silver", "diamond"), "champagne_silk")

    def test_missing_or_unrecognised_metal_falls_back_to_auto(self):
        for metal in (None, "", "   ", "mystery alloy", 42, ["gold"]):
            self.assertEqual(select_drape(metal), "auto", metal)
            self.assertEqual(select_drape(metal, "emerald"), "auto", metal)

    def test_normalisers(self):
        self.assertEqual(normalise_metal_tone("  Yellow  GOLD "), "gold")
        self.assertIsNone(normalise_metal_tone(None))
        self.assertEqual(normalise_stone_family("Cubic Zirconia"), "clear")
        self.assertEqual(normalise_stone_family("diamonds"), "clear")
        self.assertEqual(normalise_stone_family("Emeralds"), "green")
        self.assertEqual(normalise_stone_family("coloured glass"), "other")   # "red" inside a word is not red
        self.assertEqual(normalise_stone_family("amethyst"), "other")
        self.assertIsNone(normalise_stone_family(""))

    def test_selected_drape_text_reaches_the_prompt(self):
        self.assertIn("CHAMPAGNE OYSTER SILK", build_luxury_drape_prompt(metal_tone="silver"))
        self.assertIn("DEEP EMERALD VELVET", build_luxury_drape_prompt(metal_tone="gold"))
        self.assertIn("DEEP BURGUNDY VELVET", build_luxury_drape_prompt(metal_tone="gold", gemstone="emerald"))
        self.assertIn("BLUSH-MAUVE SATIN", build_luxury_drape_prompt(metal_tone="rose gold"))
        self.assertIn("ADAPTIVE TO THE EARRING", build_luxury_drape_prompt(metal_tone="unknown"))


class LuxuryDrapePackWiringTests(unittest.TestCase):
    def test_luxury_drape_is_the_eighth_and_last_pack_shot(self):
        self.assertEqual(mws.CATALOG_PACK_STYLES[-1], ("Luxury Drape", "prompt_luxury_drape"))
        self.assertEqual(len(mws.CATALOG_PACK_STYLES), 8)
        self.assertEqual(len(set(mws.CATALOG_PACK_STYLES)), len(mws.CATALOG_PACK_STYLES))

    def test_pack_size_follows_the_style_list_everywhere(self):
        count = len(mws.CATALOG_PACK_STYLES)
        self.assertEqual(mws.pack_generation_count(), count)
        self.assertEqual(config._PACK_IMAGE_COUNT, count)
        self.assertIn(f"generating all {count} styles", mws.CATALOG_PACK_ACK_TEMPLATE)

    def test_customer_copy_uses_the_pack_size_and_stays_within_whatsapp_limits(self):
        count = len(mws.CATALOG_PACK_STYLES)
        self.assertIn(f"{count} jewellery photoshoot styles", sku_messages.collections_body())
        self.assertIn(f"{count} jewellery photoshoot styles", sku_messages.catalog_pack_body())
        self.assertIn(f"{count} styles", sku_messages.catalog_collection_row())
        self.assertIn(f"of {count} photoshoot styles", sku_messages.catalog_tier_description(5))
        self.assertLessEqual(len(sku_messages.catalog_collection_row()), 72)
        self.assertLessEqual(len(sku_messages.catalog_tier_description(100)), 72)
        self.assertLessEqual(len(sku_messages.collections_body()), 1024)
        self.assertLessEqual(len(sku_messages.catalog_pack_body()), 1024)


if __name__ == "__main__":
    unittest.main()
