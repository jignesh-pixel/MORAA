"""Earring e-commerce prompt v2 (opt-in) + EARRING_PROMPT_VERSION toggle.

Pure string tests: no provider, network or paid generation call is made.
"""

import hashlib
import unittest
from unittest.mock import patch

from app.config import settings
from app.ai.product_fidelity import REFERENCE_PRIORITY_BLOCK
from app.services import earring_ecommerce_prompt as m

# sha256 of build_earring_ecommerce_prompt() output captured BEFORE v2 was
# added. The default (v1) path must stay byte-for-byte identical.
V1_SNAPSHOT = {
    None: "8972f8b9aab977408246791e02557e05f1699101afe9ea8e0e80abca799de239",
    "Hoop": "61c3f6ac1b2460259ac5ad5d3a34267cd67c60e9a42cccdf525e96fba141ea09",
    "Stud": "e457e3d80da476266fc0f14c6e9c078e4d831dffe79c12b5e33700627bf03689",
    "Dangle": "a5b336b58d75ee00d0dd719b7f3948162db9e4e0aca6bec9b1e7612fb02e1e05",
}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _v2(earring_type=None):
    with patch.object(settings, "EARRING_PROMPT_VERSION", "v2"):
        return m.build_earring_ecommerce_prompt(earring_type=earring_type)


class ToggleTests(unittest.TestCase):
    def test_default_setting_is_v1(self):
        self.assertEqual(type(settings).model_fields["EARRING_PROMPT_VERSION"].default, "v1")

    def test_default_output_is_byte_identical_to_pre_v2_prompt(self):
        with patch.object(settings, "EARRING_PROMPT_VERSION", "v1"):
            for earring_type, digest in V1_SNAPSHOT.items():
                self.assertEqual(_sha(m.build_earring_ecommerce_prompt(earring_type=earring_type)), digest, earring_type)
                self.assertEqual(_sha(m.build_earring_ecommerce_prompt_v1(earring_type=earring_type)), digest, earring_type)

    def test_unknown_or_empty_version_falls_back_to_v1(self):
        for value in ("", "v3", "latest", None, "  "):
            with patch.object(settings, "EARRING_PROMPT_VERSION", value):
                self.assertEqual(_sha(m.build_earring_ecommerce_prompt()), V1_SNAPSHOT[None], value)

    def test_version_value_is_case_and_space_insensitive(self):
        with patch.object(settings, "EARRING_PROMPT_VERSION", " V2 "):
            self.assertEqual(m.build_earring_ecommerce_prompt(), m.build_earring_ecommerce_prompt_v2())

    def test_v2_selected_only_when_asked(self):
        self.assertEqual(_v2(), m.build_earring_ecommerce_prompt_v2())
        self.assertNotEqual(_v2(), m.build_earring_ecommerce_prompt_v1())


class V2ContentTests(unittest.TestCase):
    def setUp(self):
        self.p = _v2()
        self.low = self.p.lower()

    def test_reference_priority_marker_present_so_block_is_not_double_appended(self):
        self.assertIn("REFERENCE IMAGE PRIORITY: MAXIMUM", self.p)
        # Same check ImageGenerationManager uses before appending the block.
        self.assertIn("REFERENCE IMAGE PRIORITY", self.p.upper())
        self.assertNotIn(REFERENCE_PRIORITY_BLOCK, self.p)

    def test_must_have_constraints(self):
        must = [
            "#ffffff",                      # pure white background
            "exact 1:1 replica",            # identity
            "count", "prongs", "plating",   # stones / setting / metal
            "hooks, posts, clasps, lever-backs",
            "asymmetry",                    # anti-symmetry
            "do not mirror",                # anti-beautification
            "silver stays silver", "gold stays gold",
            "colour temperature matches the reference",
            "does not make the jewellery white",
            "hands", "display cards", "packaging",          # cleanup
            "never remove a part of the jewellery", "when unsure, keep it",
            "unreconstructed",              # anti-reconstruction
            "orientation and viewpoint",    # angle
            "only object",                  # single product
            "photography, not design",      # anti-redesign
            "fashion jewellery",            # task header
        ]
        for phrase in must:
            self.assertIn(phrase, self.low, phrase)

    def test_earring_type_blocks_reused(self):
        self.assertIn("EARRING TYPE: Preserve the exact earring type", self.p)
        for t, word in (("Hoop", "HOOP"), ("Stud", "STUD"), ("Dangle", "DANGLE")):
            self.assertIn(m.EARRING_TYPE_PRESERVATION[t], _v2(t))
            self.assertIn(word, _v2(t))

    def test_v1_contradictions_and_filler_removed(self):
        self.assertNotIn("amazon", self.low)          # marketplace-independent
        self.assertNotIn("realistic size", self.low)  # conflicted with frame fill
        self.assertNotIn("non-negotiable", self.low)
        self.assertLessEqual(self.p.count("#FFFFFF"), 2)

    def test_v2_is_much_shorter_than_v1(self):
        v1 = m.build_earring_ecommerce_prompt_v1()
        self.assertLess(len(self.p), len(v1) * 0.4)
        self.assertGreater(len(self.p), 1500)

    def test_builders_are_pure_and_make_no_calls(self):
        with patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image") as gen:
            m.build_earring_ecommerce_prompt_v2()
            _v2()
        gen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
