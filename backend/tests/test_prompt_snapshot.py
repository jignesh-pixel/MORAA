"""Image quality lock: every assembled image prompt must be byte-identical to the approved snapshot.

The prompts are what decide image quality. This test builds all of them offline (no provider call)
and compares SHA-256 hashes with tests/snapshots/prompt_hashes.json. A failure means an output-quality
change: if it is intended, review the new prompt text and run
`python scripts/prompt_snapshot.py --update`, then commit the snapshot with the prompt change.
"""

import importlib.util
import sys
import json
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SNAPSHOT = BACKEND / "tests" / "snapshots" / "prompt_hashes.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("prompt_snapshot_script", BACKEND / "scripts" / "prompt_snapshot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["prompt_snapshot_script"] = module          # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


class PromptSnapshotTests(unittest.TestCase):
    def test_every_assembled_prompt_matches_the_approved_snapshot(self):
        script = _load_script()
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        diff = script.compare(script.build_snapshot(in_process=True), expected)
        self.assertEqual(diff, {"changed": [], "missing": [], "added": []}, "image prompt output changed")

    def test_snapshot_covers_every_builder_type_and_both_providers(self):
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(expected), 60)
        self.assertTrue(any(k.endswith("-> gemini") for k in expected))
        self.assertTrue(any(k.endswith("-> openai") for k in expected))
        self.assertTrue(any("[v1]" in k for k in expected) and any("[v2]" in k for k in expected))

    def test_comparison_detects_a_single_byte_change(self):
        script = _load_script()
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        tampered = dict(expected)
        key = sorted(tampered)[0]
        tampered[key] = "0" * 64
        diff = script.compare(tampered, expected)
        self.assertEqual(diff["changed"], [key])
