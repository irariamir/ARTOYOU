"""
Offline tests for the Telegram bot helpers.

The real bot can only be exercised against Telegram's API, but all the
pure logic — sizing decisions, progress bar rendering, callback parsing,
cache TTL eviction, URL extraction, application bootstrap — is tested here.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["ARTOYOU_DOWNLOAD_DIR"] = tempfile.mkdtemp(prefix="artoyou-bot-test-")

from server import bot, config  # noqa: E402


class FmtSizeTest(unittest.TestCase):
    def test_megabytes_and_gigabytes(self):
        self.assertEqual(bot._fmt_size(30 * 1024 * 1024), "30 MB")
        self.assertEqual(bot._fmt_size(int(1.5 * 1024 * 1024 * 1024)), "1.5 GB")

    def test_unknown(self):
        self.assertEqual(bot._fmt_size(None), "? MB")
        self.assertEqual(bot._fmt_size(0), "? MB")


class ProgressBarTest(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(bot._progress_bar(-5).count("█"), 0)
        full = bot._progress_bar(150)
        self.assertEqual(full.count("█"), len(full))  # clamped to 100%

    def test_width_and_half(self):
        bar = bot._progress_bar(50, width=10)
        self.assertEqual(len(bar), 10)
        self.assertEqual(bar.count("█"), 5)


class CallbackParseTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(bot._parse_callback("v:abc1234567:3"), ("v", "abc1234567", 3))
        self.assertEqual(bot._parse_callback("a:xyz:0"), ("a", "xyz", 0))
        self.assertEqual(bot._parse_callback("j:deadbeef00"), ("j", "deadbeef00", None))
        self.assertEqual(bot._parse_callback("c:key1234567"), ("c", "key1234567", None))

    def test_garbage(self):
        self.assertEqual(bot._parse_callback(""), ("", None, None))
        self.assertEqual(bot._parse_callback("v:key:notanumber"), ("v", "key", None))


class CacheTest(unittest.TestCase):
    def setUp(self):
        bot._ANALYSIS_CACHE.clear()

    def test_put_get_pop(self):
        key = bot._cache_put({"url": "u", "title": "t"})
        self.assertIn("created_at", bot._ANALYSIS_CACHE[key])
        self.assertEqual(bot._cache_get(key)["title"], "t")
        bot._cache_pop(key)
        self.assertIsNone(bot._cache_get(key))

    def test_ttl_expiry(self):
        key = bot._cache_put({"title": "old"})
        bot._ANALYSIS_CACHE[key]["created_at"] = time.time() - bot.CACHE_TTL_SECONDS - 1
        self.assertIsNone(bot._cache_get(key))

    def test_capacity_eviction(self):
        for i in range(bot.MAX_CACHE_ENTRIES + 5):
            key = bot._cache_put({"title": f"t{i}"})
            bot._ANALYSIS_CACHE[key]["created_at"] = time.time() - i  # oldest = first inserted
        self.assertLessEqual(len(bot._ANALYSIS_CACHE), bot.MAX_CACHE_ENTRIES)


class MaxSendBytesTest(unittest.TestCase):
    def tearDown(self):
        config.TELEGRAM_API_BASE_URL = None

    def test_cloud_limit(self):
        config.TELEGRAM_API_BASE_URL = None
        self.assertEqual(bot.max_send_bytes(), 48 * 1024 * 1024)

    def test_local_bot_api_limit(self):
        config.TELEGRAM_API_BASE_URL = "http://127.0.0.1:8081"
        self.assertEqual(bot.max_send_bytes(), 1900 * 1024 * 1024)


class ExtractUrlTest(unittest.TestCase):
    def test_finds_url_in_persian_text(self):
        self.assertEqual(
            bot._extract_url("سلام این ویدیو رو برام دانلود کن https://youtu.be/dQw4w9WgXcQ ممنون"),
            "https://youtu.be/dQw4w9WgXcQ",
        )

    def test_strips_trailing_punctuation(self):
        self.assertEqual(
            bot._extract_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ)."),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_no_url(self):
        self.assertIsNone(bot._extract_url("سلام خوبی؟"))


class BuildApplicationTest(unittest.TestCase):
    def test_builds_offline_with_handlers(self):
        app = bot.build_bot_application("9270152200:AAGOQMqOj8Z0P93j8albAuHN9hvgYoV8rF8")
        handlers = sum(len(group) for group in app.handlers.values())
        self.assertGreaterEqual(handlers, 4)  # start, help, link, callback


if __name__ == "__main__":
    unittest.main()
