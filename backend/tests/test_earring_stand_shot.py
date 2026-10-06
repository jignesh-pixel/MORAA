"""Earring on Stand Display shot (Prompt 8, E-Com Pack 1 slot 7): builder, API route and pack wiring."""

import asyncio
import unittest

from fastapi import HTTPException

from app import config
from app.api.routes.earring_stand_shot import (
    StandShotPromptRequest,
    generate_stand_shot_prompt,
    stand_shot_health,
)
from app.services import meta_whatsapp_service as mws
from app.services.earring_ecommerce_prompt import (
    EARRING_TYPE_PRESERVATION,
    GENERIC_EARRING_PRESERVATION,
    PURE_WHITE_BACKGROUND_INSTRUCTION,
)
from app.services.earring_macro_shot_prompt import METAL_AFFIRMATION_INSTRUCTION
from app.services.earring_on_stand_shot import (
    BACKDROP_INSTRUCTIONS,
    BASE_INSTRUCTIONS,
    GENERIC_MOUNTING_RULE,
    MOUNTING_RULES,
    PAIR_AND_SINGLE_PLACEMENT_RULES,
    REFERENCE_PRIORITY_MARKER,
    STAND_INSTRUCTIONS,
    STAND_TONE_INSTRUCTIONS,
    VALID_BACKDROPS,
    VALID_BASE_FINISHES,
    VALID_STAND_STYLES,
    VALID_STAND_TONES,
    build_stand_shot_prompt,
)


class StandShotBuilderTests(unittest.TestCase):
    def setUp(self):
        self.prompt = build_stand_shot_prompt()

    def test_reference_priority_marker_appears_exactly_once(self):
        self.assertEqual(self.prompt.count(REFERENCE_PRIORITY_MARKER), 1)

    def test_zero_argument_call_uses_the_production_defaults(self):
        self.assertEqual(
            self.prompt,
            build_stand_shot_prompt(None, stand_style="t_bar", base_finish="leatherette", stand_tone="auto",
                                    backdrop="soft_grey"),
        )
        for block in (STAND_INSTRUCTIONS["t_bar"], BASE_INSTRUCTIONS["leatherette"], STAND_TONE_INSTRUCTIONS["auto"],
                      BACKDROP_INSTRUCTIONS["soft_grey"], GENERIC_EARRING_PRESERVATION, GENERIC_MOUNTING_RULE):
            self.assertIn(block, self.prompt)

    def test_unknown_option_values_fall_back_to_the_defaults(self):
        self.assertEqual(
            build_stand_shot_prompt(stand_style="tree", base_finish="marble", stand_tone="neon", backdrop="beach"),
            self.prompt,
        )

    def test_each_option_value_selects_its_own_block(self):
        for style in VALID_STAND_STYLES:
            self.assertIn(STAND_INSTRUCTIONS[style], build_stand_shot_prompt(stand_style=style))
        for finish in VALID_BASE_FINISHES:
            self.assertIn(BASE_INSTRUCTIONS[finish], build_stand_shot_prompt(base_finish=finish))
        for tone in VALID_STAND_TONES:
            self.assertIn(STAND_TONE_INSTRUCTIONS[tone], build_stand_shot_prompt(stand_tone=tone))
        for backdrop in VALID_BACKDROPS:
            self.assertIn(BACKDROP_INSTRUCTIONS[backdrop], build_stand_shot_prompt(backdrop=backdrop))

    def test_each_earring_type_pulls_in_its_preservation_and_mounting_blocks(self):
        for earring_type in ("Hoop", "Stud", "Dangle"):
            prompt = build_stand_shot_prompt(earring_type=earring_type)
            self.assertIn(EARRING_TYPE_PRESERVATION[earring_type], prompt)
            self.assertIn(MOUNTING_RULES[earring_type], prompt)
            self.assertNotIn(GENERIC_MOUNTING_RULE, prompt)

    def test_single_and_pair_placement_clause_is_present(self):
        self.assertIn(PAIR_AND_SINGLE_PLACEMENT_RULES, self.prompt)
        self.assertIn("NEVER invent or duplicate a second earring", self.prompt)

    def test_product_fidelity_comes_before_the_stand_presentation(self):
        fidelity = self.prompt.find("PRODUCT FIDELITY CORE")
        stand = self.prompt.find("DISPLAY STAND SETUP")
        self.assertGreaterEqual(fidelity, 0)
        self.assertGreater(stand, fidelity)

    def test_shared_metal_affirmation_block_is_the_macro_shot_one(self):
        # The builder imports the shared block directly; a silent local fallback copy would drift from it.
        self.assertIn(METAL_AFFIRMATION_INSTRUCTION, self.prompt)

    def test_white_background_instruction_is_not_included(self):
        self.assertNotIn(PURE_WHITE_BACKGROUND_INSTRUCTION, self.prompt)


class StandShotRouteTests(unittest.TestCase):
    def test_default_request_returns_the_pack_prompt(self):
        response = asyncio.run(generate_stand_shot_prompt(StandShotPromptRequest()))
        self.assertTrue(response.success)
        self.assertEqual(response.prompt, build_stand_shot_prompt())
        self.assertEqual((response.stand_style, response.base_finish, response.stand_tone, response.backdrop),
                         ("t_bar", "leatherette", "auto", "soft_grey"))

    def test_options_are_passed_to_the_builder(self):
        request = StandShotPromptRequest(earring_type="Stud", stand_style="ladder_bar", base_finish="velvet",
                                         stand_tone="black", backdrop="charcoal")
        response = asyncio.run(generate_stand_shot_prompt(request))
        self.assertTrue(response.success)
        self.assertEqual(response.prompt, build_stand_shot_prompt("Stud", "ladder_bar", "velvet", "black", "charcoal"))
        self.assertEqual(response.earring_type, "Stud")

    def test_invalid_values_are_rejected_with_400_instead_of_silently_replaced(self):
        for field, value in (("earring_type", "Chandelier"), ("stand_style", "tree"), ("base_finish", "marble"),
                             ("stand_tone", "neon"), ("backdrop", "beach")):
            with self.subTest(field=field):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(generate_stand_shot_prompt(StandShotPromptRequest(**{field: value})))
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIn(field, caught.exception.detail)

    def test_health_lists_every_option(self):
        body = asyncio.run(stand_shot_health())
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["stand_styles"], list(VALID_STAND_STYLES))
        self.assertEqual(body["base_finishes"], list(VALID_BASE_FINISHES))
        self.assertEqual(body["stand_tones"], list(VALID_STAND_TONES))
        self.assertEqual(body["backdrops"], list(VALID_BACKDROPS))
        self.assertEqual(body["pack_slot"], 7)

    def test_routes_are_registered_on_the_app(self):
        from app.main import app

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/api/earring-stand-shot/prompt", paths)
        self.assertIn("/api/earring-stand-shot/prompt/health", paths)


class StandShotPackWiringTests(unittest.TestCase):
    def test_stand_display_is_the_seventh_and_last_pack_shot(self):
        self.assertEqual(mws.CATALOG_PACK_STYLES[-1], ("Stand Display", "prompt_stand"))
        self.assertEqual(len(mws.CATALOG_PACK_STYLES), 7)

    def test_pack_size_constant_in_config_matches_the_pack(self):
        # config.py cannot import the service (circular); this keeps its "cap below one Pack" warning honest.
        # Strict single-call policy: a Pack is one image call, whatever the number of defined styles.
        self.assertEqual(mws.pack_generation_count(), mws.MAX_IMAGE_CALLS_PER_ORDER)
        self.assertEqual(config._PACK_IMAGE_COUNT, mws.pack_generation_count())


if __name__ == "__main__":
    unittest.main()
