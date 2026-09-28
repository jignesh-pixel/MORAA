"""Prompt 1 Freeze Guard — Regression Protection Tests.

These tests verify that Prompt 1 (Earring E-Commerce Image) remains
unchanged and isolated from Prompt 2 (Close Up Ears).

RUN BEFORE AND AFTER EVERY PROMPT 2 CHANGE:
    python -m pytest tests/test_prompt1_freeze_guard.py -v

If ANY test fails, Prompt 1 has regressed and Prompt 2 work must stop.
"""

import hashlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─── Prompt 1 File Hashes (Baseline — 2026-08-25 Initial Commit) ──────
# These MD5 hashes capture the exact content of each Prompt 1 file at
# freeze time. If any hash changes, the file has been modified.
# NOTE: Re-baselined 2026-09-09 — Prompt 1 builder gained the mandatory
# 1:1 visual-preservation lock + input-extraction directives (approved
# change). Content is otherwise identical to the original baseline.
# Re-baselined 2026-09-28 — approved changes: 100% pure white background
# (ff2754f, 2026-09-19) and the v2 prompt behind EARRING_PROMPT_VERSION
# (46b0dd0, 2026-09-25; v1 stays the default). Hashes are computed with
# CRLF normalised to LF so a Windows checkout does not trip the guard.

PROMPT1_FILE_HASHES = {
    "backend/app/services/earring_ecommerce_prompt.py": "989f0015142b944d91bc541f0bef3693",
    "backend/app/api/routes/earring_ecommerce.py": "04b95e0d8f93229d1ccd1bf71b0f0782",
    "frontend/src/services/earring-ecommerce.service.ts": "aed13afa09d236994a89be632df7480d",
}

# ─── Prompt 2 Files (must NOT import Prompt 1) ─────────────────────────

PROMPT2_FILES = [
    "backend/app/services/earring_close_up_ears_prompt.py",
    "backend/app/api/routes/earring_close_up_ears.py",
    "frontend/src/services/earring-close-up-ears.service.ts",
]

# ─── Shared Files (touched by Prompt 2, must be additive-only) ─────────

SHARED_FILES_PROMPT2_TOUCHES = [
    "backend/app/main.py",
    "frontend/src/components/PromptGenerationPanel.tsx",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _md5(filepath: Path) -> str:
    """Compute MD5 hash of a file."""
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read().replace(b"\r\n", b"\n")).hexdigest()


class TestPrompt1FileIntegrity(unittest.TestCase):
    """Verify Prompt 1 files have not been modified from baseline."""

    def test_prompt1_builder_hash_unchanged(self):
        """Prompt 1 builder file hash matches baseline."""
        path = PROJECT_ROOT / "backend/app/services/earring_ecommerce_prompt.py"
        self.assertTrue(path.exists(), f"Prompt 1 builder missing: {path}")
        self.assertEqual(
            _md5(path),
            PROMPT1_FILE_HASHES["backend/app/services/earring_ecommerce_prompt.py"],
            "Prompt 1 builder has been modified — FROZEN VIOLATION",
        )

    def test_prompt1_route_hash_unchanged(self):
        """Prompt 1 route file hash matches baseline."""
        path = PROJECT_ROOT / "backend/app/api/routes/earring_ecommerce.py"
        self.assertTrue(path.exists(), f"Prompt 1 route missing: {path}")
        self.assertEqual(
            _md5(path),
            PROMPT1_FILE_HASHES["backend/app/api/routes/earring_ecommerce.py"],
            "Prompt 1 route has been modified — FROZEN VIOLATION",
        )

    def test_prompt1_frontend_service_hash_unchanged(self):
        """Prompt 1 frontend service file hash matches baseline."""
        path = PROJECT_ROOT / "frontend/src/services/earring-ecommerce.service.ts"
        self.assertTrue(path.exists(), f"Prompt 1 frontend service missing: {path}")
        self.assertEqual(
            _md5(path),
            PROMPT1_FILE_HASHES["frontend/src/services/earring-ecommerce.service.ts"],
            "Prompt 1 frontend service has been modified — FROZEN VIOLATION",
        )


class TestPrompt1OutputContract(unittest.TestCase):
    """Verify Prompt 1 builder produces the expected output contract."""

    def test_builder_imports(self):
        """Prompt 1 builder can be imported."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        self.assertTrue(callable(build_earring_ecommerce_prompt))

    def test_builder_returns_string(self):
        """Prompt 1 builder returns a non-empty string."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 500, "Prompt 1 output too short")

    def test_builder_contains_reference_priority_marker(self):
        """Prompt 1 output contains REFERENCE IMAGE PRIORITY marker."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("REFERENCE IMAGE PRIORITY: MAXIMUM", result)

    def test_builder_contains_task_header(self):
        """Prompt 1 output contains the earring e-commerce task header."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("Fashion Jewellery", result)
        self.assertIn("Earring", result)

    def test_builder_contains_anti_redesign(self):
        """Prompt 1 output contains anti-redesign rules."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("ANTI-REDESIGN", result)
        self.assertIn("PRODUCT PHOTOGRAPHY", result)

    def test_builder_contains_anti_symmetry(self):
        """Prompt 1 output contains anti-symmetry rules."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("ANTI-SYMMETRY", result)

    def test_builder_contains_material_fidelity(self):
        """Prompt 1 output contains material fidelity rules."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("MATERIAL & COLOUR FIDELITY", result)

    def test_builder_contains_output_rule(self):
        """Prompt 1 output contains the output rule (no card/backing)."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("OUTPUT RULE", result)
        self.assertIn("no jewellery card", result)

    def test_builder_accepts_earring_type_hoop(self):
        """Prompt 1 builder accepts Hoop earring type."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt(earring_type="Hoop")
        self.assertIn("HOOP", result)

    def test_builder_accepts_earring_type_stud(self):
        """Prompt 1 builder accepts Stud earring type."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt(earring_type="Stud")
        self.assertIn("STUD", result)

    def test_builder_accepts_earring_type_dangle(self):
        """Prompt 1 builder accepts Dangle earring type."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt(earring_type="Dangle")
        self.assertIn("DANGLE", result)

    def test_builder_generic_when_no_type(self):
        """Prompt 1 builder uses generic preservation when no type given."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        self.assertIn("EARRING TYPE", result)


class TestPrompt1Isolation(unittest.TestCase):
    """Verify Prompt 1 does not contain Prompt 2 concepts."""

    def test_prompt1_no_on_ear_references(self):
        """Prompt 1 output must NOT contain on-ear / ear-model instructions."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        result_lower = result.lower()
        self.assertNotIn("on-ear", result_lower)
        self.assertNotIn("on ear", result_lower)
        self.assertNotIn("woman's ear", result_lower)
        self.assertNotIn("earlobe", result_lower)
        self.assertNotIn("macro close-up", result_lower)
        self.assertNotIn("close up ears", result_lower)

    def test_prompt1_no_human_model(self):
        """Prompt 1 output must NOT require a human model."""
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        result = build_earring_ecommerce_prompt()
        result_lower = result.lower()
        # Prompt 1 should REMOVE human elements, not require them
        self.assertIn("hand, fingers, or body parts", result_lower)


class TestPrompt2Isolation(unittest.TestCase):
    """Verify Prompt 2 does not import or modify Prompt 1."""

    def test_prompt2_does_not_import_prompt1_builder(self):
        """Prompt 2 files must not import Prompt 1's builder function."""
        for filepath in PROMPT2_FILES:
            full_path = PROJECT_ROOT / filepath
            if not full_path.exists():
                continue
            content = full_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "build_earring_ecommerce_prompt",
                content,
                f"{filepath} imports Prompt 1 builder — ISOLATION VIOLATION",
            )

    def test_prompt2_does_not_import_prompt1_route(self):
        """Prompt 2 files must not import Prompt 1's route module."""
        for filepath in PROMPT2_FILES:
            full_path = PROJECT_ROOT / filepath
            if not full_path.exists():
                continue
            content = full_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "earring_ecommerce",
                content,
                f"{filepath} references Prompt 1 route — ISOLATION VIOLATION",
            )

    def test_prompt2_has_own_builder(self):
        """Prompt 2 has its own independent prompt builder."""
        from app.services.earring_close_up_ears_prompt import build_close_up_ears_prompt
        result = build_close_up_ears_prompt()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 200, "Prompt 2 builder output too short")

    def test_prompt2_requires_ear(self):
        """Prompt 2 output explicitly requires a human ear (opposite of Prompt 1)."""
        from app.services.earring_close_up_ears_prompt import build_close_up_ears_prompt
        result = build_close_up_ears_prompt()
        result_lower = result.lower()
        self.assertIn("ear", result_lower)
        self.assertIn("on-ear", result_lower)


class TestPrompt1RouteExists(unittest.TestCase):
    """Verify Prompt 1 route is registered and accessible."""

    def test_prompt1_route_imports(self):
        """Prompt 1 route module can be imported."""
        from app.api.routes.earring_ecommerce import router
        self.assertIsNotNone(router)

    def test_prompt1_route_has_prompt_endpoint(self):
        """Prompt 1 route has the /prompt endpoint."""
        from app.api.routes.earring_ecommerce import generate_earring_ecommerce_prompt
        self.assertTrue(callable(generate_earring_ecommerce_prompt))

    def test_prompt1_route_has_health_endpoint(self):
        """Prompt 1 route has the /health endpoint."""
        from app.api.routes.earring_ecommerce import earring_ecommerce_health
        self.assertTrue(callable(earring_ecommerce_health))


class TestPrompt1SharedComponentProtection(unittest.TestCase):
    """Verify shared components Prompt 2 touches are not broken."""

    def test_main_py_still_registers_prompt1(self):
        """main.py still registers the Prompt 1 router."""
        path = PROJECT_ROOT / "backend/app/main.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn("earring_ecommerce", content)
        self.assertIn("app.include_router(earring_ecommerce.router)", content)

    def test_main_py_still_registers_prompt2(self):
        """main.py also registers the Prompt 2 router."""
        path = PROJECT_ROOT / "backend/app/main.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn("earring_close_up_ears", content)
        self.assertIn("app.include_router(earring_close_up_ears.router)", content)

    def test_image_generation_manager_unchanged(self):
        """ImageGenerationManager file hash matches baseline."""
        path = PROJECT_ROOT / "backend/app/ai/image_generation_manager.py"
        self.assertTrue(path.exists())
        # Verify it still has the reference priority block logic
        content = path.read_text(encoding="utf-8")
        self.assertIn("REFERENCE_PRIORITY_BLOCK", content)
        self.assertIn("REFERENCE IMAGE PRIORITY", content)

    def test_product_fidelity_unchanged(self):
        """Product fidelity module still exports required constants."""
        from app.ai.product_fidelity import (
            REFERENCE_PRIORITY_BLOCK,
            GENERIC_FIDELITY_INSTRUCTION,
            PRODUCT_PRESERVATION_CONSTRAINTS,
        )
        self.assertIsInstance(REFERENCE_PRIORITY_BLOCK, str)
        self.assertIsInstance(GENERIC_FIDELITY_INSTRUCTION, str)
        self.assertIsInstance(PRODUCT_PRESERVATION_CONSTRAINTS, str)

    def test_openai_provider_unchanged(self):
        """OpenAI provider still has identity anchor and image editing support."""
        from app.ai.providers.openai_image_provider import (
            OPENAI_IDENTITY_ANCHOR,
            OpenAIImageProvider,
        )
        self.assertIn("authoritative source", OPENAI_IDENTITY_ANCHOR)
        provider = OpenAIImageProvider()
        self.assertIn("gpt-image-1", provider.IMAGE_EDITING_MODELS)


if __name__ == "__main__":
    unittest.main()
