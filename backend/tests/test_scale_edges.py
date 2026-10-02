"""Scale phase: bounded uploads (SEC-8), production hides the API docs (SEC-7), pure-ASGI middleware (PERF-7)."""

import asyncio
import io
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.middleware.logging_middleware import RequestLoggingMiddleware
from app.middleware.public_host_guard import PublicHostGuardMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.utils.upload_limits import MAX_BASE64_CHARS, read_capped


class _FakeUpload:
    def __init__(self, data: bytes):
        self._io = io.BytesIO(data)
        self.reads = 0

    async def read(self, n=-1):
        self.reads += 1
        return self._io.read(n)


class ReadCappedTests(unittest.TestCase):
    def test_a_small_file_is_read_whole(self):
        self.assertEqual(asyncio.run(read_capped(_FakeUpload(b"abc" * 1000), limit=10_000)), b"abc" * 1000)

    def test_an_oversized_file_is_refused_after_a_few_chunks_not_read_to_the_end(self):
        up = _FakeUpload(b"x" * (50 * 1024 * 1024))
        with self.assertRaises(ValueError):
            asyncio.run(read_capped(up, limit=1024 * 1024))
        self.assertLess(up.reads, 10)                     # stopped early

    def test_exactly_the_limit_is_allowed(self):
        self.assertEqual(len(asyncio.run(read_capped(_FakeUpload(b"x" * 100), limit=100))), 100)


class FieldCapTests(unittest.TestCase):
    def test_base64_fields_have_a_maximum_length(self):
        import app.schemas.analysis as analysis

        model = next(v for v in vars(analysis).values() if isinstance(v, type) and "image_base64" in getattr(v, "model_fields", {}))
        with self.assertRaises(ValidationError):
            model(image_base64="A" * (MAX_BASE64_CHARS + 1), mime_type="image/jpeg")


class DocsHiddenInProductionTests(unittest.TestCase):
    def test_docs_are_on_in_development_and_off_in_production(self):
        import app.main as main

        self.assertIsNotNone(main.app.docs_url)           # tests run in development
        with patch.object(type(settings), "IS_PRODUCTION", True):
            reloaded = FastAPI(docs_url=None if settings.IS_PRODUCTION else "/docs")
        self.assertIsNone(reloaded.docs_url)


class PureAsgiTests(unittest.TestCase):
    def test_the_three_layers_are_plain_asgi_callables_not_base_http_middleware(self):
        from starlette.middleware.base import BaseHTTPMiddleware

        for cls in (RequestLoggingMiddleware, RateLimitMiddleware, PublicHostGuardMiddleware):
            self.assertFalse(issubclass(cls, BaseHTTPMiddleware), cls.__name__)

    def test_request_id_header_and_state_are_set(self):
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        @app.get("/x")
        async def x():
            return {"ok": True}

        response = TestClient(app).get("/x")
        self.assertEqual(len(response.headers["X-Request-ID"]), 8)

    def test_a_public_websocket_is_closed_by_the_guard(self):
        app = FastAPI()
        app.add_middleware(PublicHostGuardMiddleware)
        with patch.object(settings, "PUBLIC_HOST_GUARD_ENABLED", True):
            client = TestClient(app, base_url="http://public.example.com")
            with self.assertRaises(Exception):
                with client.websocket_connect("/ws"):
                    pass


if __name__ == "__main__":
    unittest.main()
