"""The phase gate must be able to fail: a gate that cannot fail proves nothing."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("phase_gate_script", BACKEND / "scripts" / "phase_gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase_gate_script"] = module          # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


class PhaseGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = _load()

    def test_summary_line_is_read_from_pytest_output(self):
        text = "....\n==== 684 passed, 10 skipped, 57 warnings in 35.08s ====\n"
        self.assertEqual(self.gate.summary_line(text), "684 passed, 10 skipped, 57 warnings in 35.08s")
        self.assertEqual(self.gate.summary_line("nothing here"), "")

    def test_a_failing_step_makes_the_gate_fail_and_the_report_says_not_locked(self):
        gate = self.gate
        steps = [
            ("good", lambda: gate.Result("good", "PASS", "fine")),
            ("bad", lambda: gate.Result("bad", "FAIL", "broken")),
            ("crash", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "STEPS", steps), \
             patch("sys.argv", ["phase_gate.py", "--phase", "Unit Test", "--out", str(Path(tmp) / "r.md")]):
            code = gate.main()
            report = (Path(tmp) / "r.md").read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertIn("NOT LOCKED", report)
        self.assertIn("| bad | FAIL |", report)
        self.assertIn("| crash | FAIL |", report)        # a crashing step counts as failed, never skipped
        self.assertIn("RuntimeError: boom", report)

    def test_all_passing_steps_lock_the_phase(self):
        gate = self.gate
        steps = [("a", lambda: gate.Result("a", "PASS", "ok")), ("b", lambda: gate.Result("b", "SKIP", "n/a"))]
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "STEPS", steps), \
             patch("sys.argv", ["phase_gate.py", "--phase", "Unit Test", "--out", str(Path(tmp) / "r.md")]):
            code = gate.main()
            report = (Path(tmp) / "r.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("LOCKED (all steps passed)", report)

    def test_a_gate_where_nothing_ran_is_not_locked(self):
        gate = self.gate
        steps = [("a", lambda: gate.Result("a", "SKIP", "n/a"))]
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "STEPS", steps), \
             patch("sys.argv", ["phase_gate.py", "--phase", "Unit Test", "--out", str(Path(tmp) / "r.md")]):
            gate.main()
            report = (Path(tmp) / "r.md").read_text(encoding="utf-8")
        self.assertIn("NOT LOCKED", report)

    def test_skipping_a_required_step_cannot_lock_the_phase(self):
        gate = self.gate
        results = [gate.Result(name, "PASS", "ok") for name in gate.REQUIRED]
        self.assertTrue(gate.verdict(results).startswith("LOCKED"))
        results[0] = gate.Result(results[0].name, "SKIP", "skipped by --skip")
        self.assertIn("required step was skipped", gate.verdict(results))

    def test_load_harness_rank_order_treats_fail_as_worse_than_warn_and_pass(self):
        rank = self.gate.RANK
        self.assertLess(rank["FAIL"], rank["WARN"])
        self.assertLess(rank["WARN"], rank["PASS"])

    def test_expected_load_verdicts_file_is_complete(self):
        import json

        expected = json.loads((BACKEND / "tests" / "load_scenarios" / "expected_verdicts.json").read_text(encoding="utf-8"))
        scenarios = {k for k in expected if not k.startswith("_")}
        self.assertEqual(scenarios, set("abcdefghik"))
        self.assertTrue(all(v in ("PASS", "WARN", "FAIL") for k, v in expected.items() if not k.startswith("_")))


if __name__ == "__main__":
    unittest.main()
