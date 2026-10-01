"""Phase 0 / U9: analysis tasks retry only transient errors, so permanent
failures do not re-run paid AI calls."""

import unittest

import httpx

from app.tasks.analysis_tasks import AnalysisTask, TRANSIENT_ERRORS


class RetryPolicyTests(unittest.TestCase):
    def test_transient_errors_are_retried(self):
        for exc in (ConnectionError("reset"), TimeoutError("slow"), httpx.ConnectError("down"),
                    httpx.ReadTimeout("slow")):
            self.assertIsInstance(exc, AnalysisTask.autoretry_for, repr(exc))

    def test_permanent_errors_are_not_retried(self):
        for exc in (ValueError("analysis record not found"), KeyError("x"), RuntimeError("bad image"),
                    PermissionError("denied"), FileNotFoundError("gone")):
            self.assertNotIsInstance(exc, AnalysisTask.autoretry_for, repr(exc))

    def test_retry_count_stays_bounded(self):
        self.assertEqual(AnalysisTask.max_retries, 2)
        self.assertEqual(AnalysisTask.autoretry_for, TRANSIENT_ERRORS)


if __name__ == "__main__":
    unittest.main()
