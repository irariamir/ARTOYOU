"""
Offline end-to-end flow tests for the Telegram bot.

A mock Telegram Bot API server records every call the bot makes, while a
local HTTP media server serves tiny synthetic mp4/m4a files. The test drives
real bot updates through the real python-telegram-bot Application machinery:

    /start (user 1)  → owner claim message
    /start (user 2)  → denial (private bot)
    link message     → analysis + inline quality keyboard
    quality tap      → job created → download → progress → file delivered

Run:  .venv/bin/python -m pytest tests/test_bot_flow.py -v
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["ARTOYOU_DOWNLOAD_DIR"] = tempfile.mkdtemp(prefix="artoyou-botflow-downloads-")

import _media  # noqa: E402

from server import bot, config, jobs  # noqa: E402

from telegram import Update  # noqa: E402

BOT_TOKEN = "9270152200:AAGOQMqOj8Z0P93j8albAuHN9hvgYoV8rF8"
USER_OWNER = 111111
USER_STRANGER = 222222


# ─────────────────────────────────────────────────── mock Telegram server
class MockTelegramHandler(BaseHTTPRequestHandler):
    server_version = "MockTelegram/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        method = urlsplit(self.path).path.rstrip("/").split("/")[-1]
        fields, files = self._parse_body(content_type, body)
        record = {"method": method, "content_type": content_type,
                  "fields": fields, "files": files, "raw": body}
        with self.server.records_lock:
            self.server.records.append(record)
            message_id = self.server.next_message_id
            self.server.next_message_id += 1

        chat_id = self._to_int(fields.get("chat_id"), USER_OWNER)
        result = self._result_for(method, fields, chat_id, message_id)
        payload = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---- helpers
    @staticmethod
    def _to_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _parse_body(self, content_type, body):
        fields: dict[str, str] = {}
        files: list[dict] = []
        if "application/x-www-form-urlencoded" in content_type:
            for key, vals in parse_qs(body.decode("utf-8", "replace")).items():
                fields[key] = vals[0]
        elif "multipart/form-data" in content_type:
            match = re.search(rb"boundary=([^;]+)", content_type.encode())
            if not match:
                return fields, files
            boundary = b"--" + match.group(1).strip().strip(b'"')
            for part in body.split(boundary):
                part = part.strip(b"\r\n")
                if not part or part == b"--":
                    continue
                head, sep, content = part.partition(b"\r\n\r\n")
                if not sep:
                    continue
                cd = head.decode("utf-8", "replace")
                name_m = re.search(r'name="([^"]+)"', cd)
                file_m = re.search(r'filename="([^"]+)"', cd)
                if not name_m:
                    continue
                name = name_m.group(1)
                if file_m:
                    if content.endswith(b"\r\n"):
                        content = content[:-2]
                    files.append({"name": name, "filename": file_m.group(1), "size": len(content)})
                    fields[name] = f"<file {file_m.group(1)}>"
                else:
                    fields[name] = content.decode("utf-8", "replace").strip()
        return fields, files

    def _result_for(self, method, fields, chat_id, message_id):
        message = {"message_id": message_id, "date": int(time.time()),
                   "chat": {"id": chat_id, "type": "private"}}
        if method == "getMe":
            return {"id": 999001, "is_bot": True, "first_name": "ARTOYOU Test",
                    "username": "artoyou_e2e_bot", "can_join_groups": True}
        if method == "sendMessage":
            return {**message, "text": fields.get("text", "")}
        if method == "editMessageText":
            return {**message, "text": fields.get("text", "")}
        if method == "sendPhoto":
            return {**message, "caption": fields.get("caption", "")}
        if method == "editMessageCaption":
            return {**message, "caption": fields.get("caption", "")}
        if method in {"sendVideo", "sendAudio", "sendDocument"}:
            return {**message, "caption": fields.get("caption", "")}
        if method in {"deleteMessage", "answerCallbackQuery", "logOut", "close"}:
            return True
        if method == "getUpdates":
            return []
        return True


class MockTelegramServer:
    def __init__(self):
        self.records: list[dict] = []
        self.records_lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockTelegramHandler)
        self.httpd.records = self.records
        self.httpd.records_lock = self.records_lock
        self.httpd.next_message_id = 5000
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def clear(self):
        with self.records_lock:
            self.records.clear()

    def find(self, method: str) -> list[dict]:
        with self.records_lock:
            return [r for r in self.records if r["method"] == method]

    async def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.records_lock:
                if any(predicate(r) for r in self.records):
                    return True
            await asyncio.sleep(0.1)
        return False


def make_message_update(user_id: int, text: str, update_id: int, bot_instance) -> Update:
    """Fabricate a raw Telegram update exactly as the polling layer would deliver it
    (Update.de_json attaches the bot so shortcut methods work)."""
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": 100 + update_id,
                "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "امیر"},
                "text": text,
            },
        },
        bot_instance,
    )


def make_callback_update(user_id: int, data: str, update_id: int, bot_instance) -> Update:
    return Update.de_json(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "from": {"id": user_id, "is_bot": False, "first_name": "امیر"},
                "chat_instance": "chat-instance",
                "data": data,
                "message": {
                    "message_id": 900 + update_id,
                    "date": int(time.time()),
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "امیر"},
                    "text": "placeholder",
                },
            },
        },
        bot_instance,
    )


class BotFlowTestCase(unittest.TestCase):
    """Sync wrapper — each test gets a fresh asyncio.run() + mock world."""

    def _flow(self):
        return _Flow(self)

    # ──────────────────────────────────────────────────────── tests
    def test_getme_handshake(self):
        async def go():
            async with self._flow():
                self.assertTrue(self.mock.find("getMe"), "bot did not call getMe on initialize()")
        asyncio.run(go())

    def test_first_user_becomes_owner_second_denied(self):
        async def go():
            async with self._flow():
                await self.app.process_update(make_message_update(USER_OWNER, "/start", 1, self.app.bot))
                claimed = await self.mock.wait_for(
                    lambda r: r["method"] == "sendMessage" and "مالک" in (r["fields"].get("text") or ""),
                    timeout=5,
                )
                self.assertTrue(claimed, "owner claim message was not sent")
                self.assertEqual((self._tmp / ".bot_owner").read_text().strip(), str(USER_OWNER))

                self.mock.clear()
                await self.app.process_update(make_message_update(USER_STRANGER, "/start", 2, self.app.bot))
                denied = await self.mock.wait_for(
                    lambda r: r["method"] == "sendMessage"
                    and str(r["fields"].get("chat_id")) == str(USER_STRANGER)
                    and "خصوصی" in (r["fields"].get("text") or ""),
                    timeout=5,
                )
                self.assertTrue(denied, "stranger was not rejected")
        asyncio.run(go())

    def test_full_link_to_video_delivery_flow(self):
        async def go():
            async with self._flow():
                # 1) claim (owner) — required before anything else
                await self.app.process_update(make_message_update(USER_OWNER, "/start", 1, self.app.bot))
                self.assertTrue(await self.mock.wait_for(
                    lambda r: r["method"] == "sendMessage" and "مالک" in (r["fields"].get("text") or ""), 5))
                self.mock.clear()

                # 2) send a YouTube link mixed with Persian text
                link_text = "سلام میشه اینو برام دانلود کنی؟ https://youtu.be/localtest01 ممنون"
                await self.app.process_update(make_message_update(USER_OWNER, link_text, 2, self.app.bot))

                got_card = await self.mock.wait_for(lambda r: r["method"] == "sendPhoto", timeout=10)
                self.assertTrue(got_card, f"no analysis card sent; got: {[r['method'] for r in self.mock.records]}")
                card = self.mock.find("sendPhoto")[0]
                self.assertEqual(str(card["fields"].get("chat_id")), str(USER_OWNER))

                # 3) quality keyboard is inline and carries our format buttons + MP3
                markup = json.loads(card["fields"]["reply_markup"])
                buttons = [b for row in markup["inline_keyboard"] for b in row]
                video_buttons = [b for b in buttons if b["callback_data"].startswith("v:")]
                audio_buttons = [b for b in buttons if b["callback_data"].startswith("a:")]
                self.assertTrue(video_buttons, "no video quality buttons")
                self.assertTrue(audio_buttons, "no audio buttons")
                self.assertTrue(any("MP3" in b["text"] for b in audio_buttons))

                # 4) tap the first video quality button
                tap_data = video_buttons[0]["callback_data"]
                await self.app.process_update(make_callback_update(USER_OWNER, tap_data, 3, self.app.bot))
                self.assertTrue(await self.mock.wait_for(lambda r: r["method"] == "answerCallbackQuery", 5))

                # 5) wait for the download job to finish (local media server, real yt-dlp)
                deadline = time.time() + 60
                final_status = None
                while time.time() < deadline:
                    jobs_now = jobs.manager.list(10)
                    if jobs_now and jobs_now[0].status in {"ready", "error", "cancelled"}:
                        final_status = jobs_now[0].status
                        break
                    await asyncio.sleep(0.25)
                self.assertEqual(final_status, "ready",
                                 f"job failed: {jobs.manager.list(1)[0].error if jobs.manager.list(1) else 'no job'}")

                # 6) the bot must deliver the file as a multipart upload with a filename
                got_video = await self.mock.wait_for(lambda r: r["method"] == "sendVideo", timeout=15)
                self.assertTrue(got_video, f"sendVideo never happened; calls: {[r['method'] for r in self.mock.records]}")
                video_call = self.mock.find("sendVideo")[0]
                self.assertEqual(str(video_call["fields"].get("chat_id")), str(USER_OWNER))
                self.assertTrue(any(f["filename"].lower().endswith(".mp4") for f in video_call["files"]),
                                f"no mp4 uploaded: {video_call['files']}")
                self.assertGreater(sum(f["size"] for f in video_call["files"]), 1000)

                # 7) the progress message must have been cleaned up
                self.assertTrue(await self.mock.wait_for(lambda r: r["method"] == "deleteMessage", 5))
        asyncio.run(go())

    def test_expired_callback_shows_alert(self):
        async def go():
            async with self._flow():
                await self.app.process_update(make_message_update(USER_OWNER, "/start", 1, self.app.bot))
                await self.mock.wait_for(lambda r: r["method"] == "sendMessage", 5)
                self.mock.clear()
                await self.app.process_update(make_callback_update(USER_OWNER, "v:deadbeef99:0", 2, self.app.bot))
                got_alert = await self.mock.wait_for(
                    lambda r: r["method"] == "answerCallbackQuery" and "منقضی" in (r["fields"].get("text") or ""),
                    timeout=5,
                )
                self.assertTrue(got_alert, "expired cache did not produce an alert")
        asyncio.run(go())

    def test_oversize_file_explains_limit(self):
        async def go():
            async with self._flow():
                await self.app.process_update(make_message_update(USER_OWNER, "/start", 1, self.app.bot))
                await self.mock.wait_for(lambda r: r["method"] == "sendMessage", 5)
                self.mock.clear()

                big = self._tmp / "big-video.mp4"
                big.write_bytes(b"\x00" * 4096)
                job = jobs.Job(
                    id="b" * 12, url="u", kind="video", format_id="18", container="mp4",
                    label="360p", mode="progressive", title="ویدیوی بزرگ", status="ready",
                    size_bytes=200 * 1024 * 1024, filepath=str(big), filename="big-video.mp4",
                )
                await bot._deliver_file(self.app.bot, USER_OWNER, job, "ویدیوی بزرگ", "360p")
                explained = any(
                    "اجازه ارسال" in (r["fields"].get("text") or "")
                    or "Bot API محلی" in (r["fields"].get("text") or "")
                    for r in self.mock.find("sendMessage")
                )
                self.assertTrue(explained, f"oversize case not explained; got: {[r['fields'].get('text') for r in self.mock.find('sendMessage')]}")
                self.assertFalse(self.mock.find("sendVideo"), "oversize file must NOT be uploaded")
        asyncio.run(go())


class _Flow:
    """Async context manager: fresh monkeypatched world per test."""

    def __init__(self, case: BotFlowTestCase):
        self.case = case

    async def __aenter__(self):
        case = self.case
        case._orig_dir = config.DOWNLOAD_DIR
        case._orig_static = config.TELEGRAM_ALLOWED_USERS
        case._tmp = Path(tempfile.mkdtemp(prefix="artoyou-botflow-"))
        config.DOWNLOAD_DIR = case._tmp
        config.TELEGRAM_ALLOWED_USERS = set()

        case._orig_bot_extract = bot.extract_info
        case._orig_bot_norm = bot.normalize_youtube_url
        case._orig_jobs_extract = jobs.extract_info
        case._orig_jobs_norm = jobs.normalize_youtube_url
        bot.extract_info = lambda url: _media.fake_info()
        bot.normalize_youtube_url = lambda url: url
        jobs.extract_info = lambda url: _media.fake_info()
        jobs.normalize_youtube_url = lambda url: url

        jobs.manager._jobs.clear()
        bot._ANALYSIS_CACHE.clear()
        bot._PROGRESS_TASKS.clear()

        case.mock = MockTelegramServer()
        case.mock.start()
        case.app = bot.build_bot_application(
            BOT_TOKEN, base_url=case.mock.base_url, base_file_url=case.mock.base_url
        )
        await case.app.initialize()
        return case

    async def __aexit__(self, exc_type, exc, tb):
        case = self.case
        try:
            await case.app.shutdown()
        except Exception:
            pass
        case.mock.stop()
        config.DOWNLOAD_DIR = case._orig_dir
        config.TELEGRAM_ALLOWED_USERS = case._orig_static
        bot.extract_info = case._orig_bot_extract
        bot.normalize_youtube_url = case._orig_bot_norm
        jobs.extract_info = case._orig_jobs_extract
        jobs.normalize_youtube_url = case._orig_jobs_norm
        return False


if __name__ == "__main__":
    unittest.main()


