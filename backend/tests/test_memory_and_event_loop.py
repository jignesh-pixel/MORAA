"""Phase 3 / PERF-2, PERF-5, PERF-6: raw bytes instead of base64 copies, each pack style uploaded as it finishes,
and heavy image work on its own CPU pool (never on the event loop)."""

import asyncio
import base64
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.ai.providers.image_base import ImageGenerationResult
from app.services import meta_whatsapp_service as mws
from app.utils import executors

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


class ResultHelpersTests(unittest.TestCase):
    def test_bytes_only_result_builds_a_data_url_on_demand(self):
        r = ImageGenerationResult(success=True, image_data=PNG, mime_type="image/png")
        self.assertIsNone(r.image_url)                               # nothing built eagerly: no extra copy in memory
        self.assertEqual(r.as_bytes(), PNG)
        self.assertEqual(r.as_data_url(), "data:image/png;base64," + base64.b64encode(PNG).decode())

    def test_older_results_with_only_a_data_url_still_work(self):
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        r = ImageGenerationResult(success=True, image_url=url)
        self.assertEqual(r.as_bytes(), PNG)
        self.assertEqual(r.as_data_url(), url)

    def test_empty_or_malformed_results_give_nothing(self):
        self.assertIsNone(ImageGenerationResult(success=True).as_bytes())
        self.assertIsNone(ImageGenerationResult(success=True, image_url="not-a-data-url").as_bytes())
        self.assertIsNone(ImageGenerationResult(success=True, image_url="data:image/png;base64,!!!").as_bytes())
        self.assertIsNone(ImageGenerationResult(success=True).as_data_url())

    def test_worker_helper_accepts_both_forms(self):
        self.assertEqual(mws._result_bytes(ImageGenerationResult(success=True, image_data=PNG)), PNG)
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        self.assertEqual(mws._result_bytes(ImageGenerationResult(success=True, image_url=url)), PNG)
        self.assertIsNone(mws._result_bytes(ImageGenerationResult(success=False)))


class UploadAsReadyTests(unittest.TestCase):
    def test_each_style_is_uploaded_as_soon_as_it_finishes(self):
        events = []

        async def fake_generate(**kw):
            await asyncio.sleep(kw["delay"])
            events.append(("generated", kw["style_title"]))
            return PNG

        async def fake_upload(data, *a, **k):
            events.append(("uploaded", len(data)))
            return "media-" + str(len([e for e in events if e[0] == "uploaded"]))

        async def run():
            with patch.object(mws, "_generate_single_pack_style", new=fake_generate), \
                 patch.object(mws, "upload_media_to_meta", new=fake_upload):
                return await asyncio.gather(
                    mws._generate_and_upload_style(style_title="slow", delay=0.2),
                    mws._generate_and_upload_style(style_title="fast", delay=0.01),
                )

        results = asyncio.run(run())
        self.assertEqual([r[0] for r in results], [True, True])
        # the fast style was generated AND uploaded before the slow one even finished generating
        self.assertLess(events.index(("generated", "fast")), events.index(("generated", "slow")))
        self.assertLess(events.index(("uploaded", len(PNG))), events.index(("generated", "slow")))

    def test_failures_are_told_apart(self):
        async def run(generate_result, upload_result):
            with patch.object(mws, "_generate_single_pack_style", new=AsyncMock(return_value=generate_result)), \
                 patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value=upload_result)):
                return await mws._generate_and_upload_style(style_title="s")

        self.assertEqual(asyncio.run(run(None, None)), (False, None))        # generation failed
        self.assertEqual(asyncio.run(run(PNG, None)), (True, None))          # generated, Meta upload failed
        self.assertEqual(asyncio.run(run(PNG, "media-1")), (True, "media-1"))


class StyleGenerationDeadlineTests(unittest.TestCase):
    def test_a_style_still_generating_at_the_deadline_is_dropped(self):
        async def stuck(**kw):
            await asyncio.sleep(3600)

        async def run():
            with patch.object(mws, "_generate_single_pack_style", new=stuck), \
                 patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value="m")):
                return await mws._generate_and_upload_style(generate_deadline_seconds=0.1, style_title="s", ingestion_id="i")

        started = time.monotonic()
        self.assertEqual(asyncio.run(run()), (False, None))
        self.assertLess(time.monotonic() - started, 3)

    def test_the_deadline_never_cuts_an_upload(self):
        """Generation finishes just in time; the upload then takes longer than the deadline and still completes."""
        async def quick(**kw):
            await asyncio.sleep(0.01)
            return PNG

        async def slow_upload(data, *a, **k):
            await asyncio.sleep(0.4)
            return "media-ok"

        async def run():
            with patch.object(mws, "_generate_single_pack_style", new=quick), \
                 patch.object(mws, "upload_media_to_meta", new=slow_upload):
                return await mws._generate_and_upload_style(generate_deadline_seconds=0.2, style_title="s", ingestion_id="i")

        self.assertEqual(asyncio.run(run()), (True, "media-ok"))


class DedicatedPoolTests(unittest.TestCase):
    def tearDown(self):
        executors.shutdown_executors()

    def test_cpu_work_runs_off_the_event_loop_thread(self):
        async def run():
            loop_thread = threading.get_ident()
            worker_thread = await executors.run_cpu(threading.get_ident)
            io_thread = await executors.run_io(threading.get_ident)
            return loop_thread, worker_thread, io_thread

        loop_thread, cpu_thread, io_thread = asyncio.run(run())
        self.assertNotEqual(loop_thread, cpu_thread)
        self.assertNotEqual(loop_thread, io_thread)

    def test_the_loop_keeps_running_while_cpu_work_blocks_a_worker(self):
        async def run():
            ticks = []

            async def heartbeat():
                while len(ticks) < 20:
                    ticks.append(time.perf_counter())
                    await asyncio.sleep(0.01)

            beat = asyncio.ensure_future(heartbeat())
            await executors.run_cpu(time.sleep, 0.3)          # a "heavy" call
            await beat
            gaps = [b - a for a, b in zip(ticks, ticks[1:])]
            return max(gaps)

        self.assertLess(asyncio.run(run()), 0.1)               # no stall anywhere near the 0.3 s of work

    def test_cpu_io_and_net_pools_are_separate(self):
        async def run():
            name = lambda: threading.current_thread().name  # noqa: E731
            return await executors.run_cpu(name), await executors.run_io(name), await executors.run_net(name)

        cpu_name, io_name, net_name = asyncio.run(run())
        self.assertTrue(cpu_name.startswith("moraa-cpu"))
        self.assertTrue(io_name.startswith("moraa-io"))
        self.assertTrue(net_name.startswith("moraa-net"))

    def test_exceptions_propagate(self):
        def boom():
            raise ValueError("bad image")

        with self.assertRaises(ValueError):
            asyncio.run(executors.run_cpu(boom))


if __name__ == "__main__":
    unittest.main()
