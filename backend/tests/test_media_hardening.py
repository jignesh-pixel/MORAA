"""SEC-11: media ids are plain tokens and the download link must be on Meta's own hosts."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.services import meta_whatsapp_service as mws


class MediaIdTests(unittest.TestCase):
    def test_plain_ids_pass_and_path_like_ids_do_not(self):
        for good in ("1234567890", "abc_DEF-9"):
            self.assertTrue(mws.is_valid_media_id(good))
        for bad in ("", "../x", "123/456", "1?x=2", "a b", "1" * 101, None, 123, "123#"):
            self.assertFalse(mws.is_valid_media_id(bad), bad)

    def test_a_bad_id_never_triggers_a_request(self):
        with patch.object(settings, "META_WHATSAPP_TOKEN", "t"), patch.object(mws.httpx, "AsyncClient") as client:
            self.assertIsNone(asyncio.run(mws.get_media_url("../../me")))
        client.assert_not_called()


class HostTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(settings, "META_MEDIA_HOST_CHECK", True)
        p.start()
        self.addCleanup(p.stop)

    def test_meta_hosts_are_accepted(self):
        for url in ("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
                    "https://scontent.xx.fbcdn.net/v/x.jpg", "https://graph.facebook.com/v21.0/1",
                    "https://mmg.whatsapp.net/d/f/x"):
            self.assertTrue(mws.is_meta_media_host(url), url)

    def test_other_hosts_schemes_and_lookalikes_are_refused(self):
        for url in ("http://lookaside.fbsbx.com/x", "https://evil.example.com/x", "https://fbsbx.com.evil.com/x",
                    "https://notfacebook.com/x", "https://169.254.169.254/latest", "file:///etc/passwd", "", "https:///x"):
            self.assertFalse(mws.is_meta_media_host(url), url)

    def test_a_download_from_a_foreign_host_is_refused_without_sending_the_token(self):
        with patch.object(mws.httpx, "AsyncClient") as client:
            self.assertIsNone(asyncio.run(mws.download_media("https://evil.example.com/x.jpg")))
        client.assert_not_called()

    def test_a_lookup_that_returns_a_foreign_link_is_refused(self):
        response = AsyncMock()
        response.status_code = 200
        response.json = lambda: {"url": "https://evil.example.com/x.jpg"}
        with patch.object(settings, "META_WHATSAPP_TOKEN", "t"), \
             patch.object(mws, "_meta_request", new=AsyncMock(return_value=response)):
            self.assertIsNone(asyncio.run(mws.get_media_url("12345")))

    def test_the_check_can_be_switched_off(self):
        with patch.object(settings, "META_MEDIA_HOST_CHECK", False):
            self.assertTrue(mws.is_meta_media_host("http://cdn/x"))


if __name__ == "__main__":
    unittest.main()
