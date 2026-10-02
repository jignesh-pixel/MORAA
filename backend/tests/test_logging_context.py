"""Phase 4 / OBS-6, OBS-7: every log line carries the id of the request that caused it (background jobs included),
and logs can be written as JSON."""

import asyncio
import json
import unittest

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from app.middleware.logging_middleware import RequestLoggingMiddleware
from app.utils.logger import get_request_id, logger, set_request_id


class _Capture:
    def __init__(self, **kwargs):
        self.lines = []
        self.handler_id = logger.add(self.lines.append, **kwargs)

    def stop(self):
        logger.remove(self.handler_id)


class RequestIdTests(unittest.TestCase):
    def test_the_id_is_on_every_line_and_defaults_to_a_dash(self):
        cap = _Capture(format="{extra[request_id]}|{message}")
        self.addCleanup(cap.stop)
        set_request_id("-")
        logger.info("no request")
        set_request_id("abc12345")
        logger.info("inside request")
        self.assertEqual([l.strip() for l in cap.lines], ["-|no request", "abc12345|inside request"])
        set_request_id("-")

    def test_concurrent_requests_never_see_each_others_id(self):
        seen = {}

        async def request(name, delay):
            set_request_id(name)
            await asyncio.sleep(delay)
            seen[name] = get_request_id()

        async def main():
            await asyncio.gather(request("A", 0.05), request("B", 0.01), request("C", 0.03))

        asyncio.run(main())
        self.assertEqual(seen, {"A": "A", "B": "B", "C": "C"})

    def test_a_background_job_started_by_a_request_keeps_its_id(self):
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        cap = _Capture(format="{extra[request_id]}|{message}")
        self.addCleanup(cap.stop)

        def job():
            logger.info("background job ran")

        @app.get("/work")
        async def work(tasks: BackgroundTasks):
            logger.info("handler ran")
            tasks.add_task(job)
            return {"ok": True}

        response = TestClient(app).get("/work")
        request_id = response.headers["X-Request-ID"]
        lines = [l.strip() for l in cap.lines]
        self.assertIn(f"{request_id}|handler ran", lines)
        self.assertIn(f"{request_id}|background job ran", lines)       # the job is traceable to its request

    def test_json_logs_carry_the_id_as_a_field(self):
        cap = _Capture(format="{message}", serialize=True)
        self.addCleanup(cap.stop)
        set_request_id("json123")
        logger.warning("something happened")
        record = json.loads(cap.lines[0])["record"]
        self.assertEqual(record["extra"]["request_id"], "json123")
        self.assertEqual(record["message"], "something happened")
        self.assertEqual(record["level"]["name"], "WARNING")
        set_request_id("-")


if __name__ == "__main__":
    unittest.main()
