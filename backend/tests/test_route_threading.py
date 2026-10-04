"""Phase 3 / PERF-2, Q-4: routes that only wait on the database (or hash passwords) are plain functions, so FastAPI
runs them on worker threads and they can never block the event loop; routes that really wait stay async.
Celery tasks have time limits and retry only transient errors."""

import ast
import inspect
import pathlib
import unittest

from app.celery_app import celery_app
from app.config import settings
from app.tasks.analysis_tasks import AnalysisTask
from app.tasks.retry_policy import TRANSIENT_ERRORS

ROUTES = pathlib.Path(__file__).resolve().parent.parent / "app" / "api" / "routes"

# Routes that must be plain `def` (blocking database / hashing work, nothing awaited).
PLAIN = {
    "analysis.py": ["get_analysis", "list_analyses", "get_analysis_timeline"],
    "auth.py": ["signup", "login", "refresh_token"],
    "history.py": ["get_history", "get_history_stats", "get_history_item", "delete_history_item"],
    "meta_webhook.py": ["retry_delivery", "get_ingestion_status", "get_generation_failure_rate"],
    "reports.py": ["download_report"],
}


def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


class RouteShapeTests(unittest.TestCase):
    def test_database_only_routes_are_plain_functions(self):
        for filename, names in PLAIN.items():
            funcs = _functions(ROUTES / filename)
            for name in names:
                self.assertIsInstance(funcs[name], ast.FunctionDef, f"{filename}:{name} must not be async")

    def test_no_async_route_is_left_that_never_waits_on_anything_and_touches_the_database(self):
        """The guard against going back: an `async def` route with a Session and no await blocks the loop."""
        offenders = []
        for path in sorted(ROUTES.glob("*.py")):
            for node in _functions(path).values():
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                routed = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                             and d.func.attr in ("get", "post", "put", "delete", "patch") for d in node.decorator_list)
                waits = any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(node))
                uses_db = any(isinstance(a.annotation, ast.Name) and a.annotation.id == "Session" for a in node.args.args)
                if routed and uses_db and not waits:
                    offenders.append(f"{path.name}:{node.name}")
        self.assertEqual(offenders, [])

    def test_routes_that_really_wait_stay_async(self):
        from app.api.routes import batch_upload, meta_webhook, payment_routes, upload

        for fn in (meta_webhook.receive_webhook, payment_routes.razorpay_webhook, upload.upload_image,
                   batch_upload.upload_images_batch):
            self.assertTrue(inspect.iscoroutinefunction(fn), fn.__name__)


class CeleryPolicyTests(unittest.TestCase):
    def test_tasks_have_soft_and_hard_time_limits(self):
        conf = celery_app.conf
        self.assertEqual(conf.task_soft_time_limit, settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS)
        self.assertEqual(conf.task_time_limit, settings.CELERY_TASK_TIME_LIMIT_SECONDS)
        self.assertLess(conf.task_soft_time_limit, conf.task_time_limit)

    def test_only_transient_errors_are_retried_and_work_is_not_lost_on_a_crash(self):
        self.assertEqual(AnalysisTask.autoretry_for, TRANSIENT_ERRORS)
        self.assertNotIn(Exception, TRANSIENT_ERRORS)
        self.assertTrue(AnalysisTask.acks_late)
        self.assertTrue(AnalysisTask.reject_on_worker_lost)


if __name__ == "__main__":
    unittest.main()
