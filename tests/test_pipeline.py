"""
End-to-end pipeline tests for ARTOYOU — no YouTube network access required.

A tiny synthetic mp4/m4a is generated with ffmpeg and served over a local
HTTP server; yt-dlp's generic extractor downloads it through the real job
pipeline (progress hooks, ffmpeg merge, filename sanitisation, cleanup).

Run:  .venv/bin/python -m pytest tests/ -v
"""

import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Isolate downloads into a temp dir BEFORE importing server modules.
_TMP_DOWNLOADS = tempfile.mkdtemp(prefix="artoyou-test-downloads-")
os.environ["ARTOYOU_DOWNLOAD_DIR"] = _TMP_DOWNLOADS

import imageio_ffmpeg  # noqa: E402

from server import config, jobs  # noqa: E402

FFMPEG = config.FFMPEG_LOCATION or imageio_ffmpeg.get_ffmpeg_exe()
MEDIA_DIR = Path(tempfile.mkdtemp(prefix="artoyou-test-media-"))
DURATION = 620  # 10m20s — inside the 5–60 minute window


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args], check=True)


def _generate_media() -> dict[str, Path]:
    combined = MEDIA_DIR / "combined.mp4"
    video_only = MEDIA_DIR / "video_only.mp4"
    audio_only = MEDIA_DIR / "audio_only.m4a"
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=8:size=320x180:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(combined),
    ])
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=8:size=320x180:rate=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-an", str(video_only),
    ])
    _run_ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=330:duration=8",
        "-c:a", "aac", "-b:a", "128k", str(audio_only),
    ])
    return {"combined": combined, "video_only": video_only, "audio_only": audio_only}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass


class LocalMediaServer:
    def __init__(self, directory: Path):
        handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


MEDIA = _generate_media()
SERVER = LocalMediaServer(MEDIA_DIR).__enter__()


def _fake_info() -> dict:
    def fmt(fid, *, ext, vcodec, acodec, height=None, abr=None, tbr=None, file_key, filesize):
        return {
            "format_id": fid,
            "ext": ext,
            "height": height,
            "fps": 12 if height else None,
            "vcodec": vcodec,
            "acodec": acodec,
            "abr": abr,
            "tbr": tbr,
            "filesize": filesize,
            "duration": DURATION,
            "url": SERVER.url(f"{file_key}.{ext}"),
        }

    return {
        "id": "localtest01",
        "title": 'Test Video: "Pipeline" / با حروف فارسی',
        "uploader": "ARTOYOU Test",
        "duration": DURATION,
        "duration_string": "10:20",
        "view_count": 12345,
        "webpage_url": "https://www.youtube.com/watch?v=localtest01",
        "formats": [
            fmt("18", ext="mp4", vcodec="avc1.42001E", acodec="mp4a.40.2",
                height=180, tbr=300, file_key="combined", filesize=os.path.getsize(MEDIA["combined"])),
            fmt("137", ext="mp4", vcodec="avc1.64001F", acodec="none",
                height=180, tbr=250, file_key="video_only", filesize=os.path.getsize(MEDIA["video_only"])),
            fmt("140", ext="m4a", vcodec="none", acodec="mp4a.40.2",
                abr=128, tbr=129, file_key="audio_only", filesize=os.path.getsize(MEDIA["audio_only"])),
        ],
    }


class PipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_extract = jobs.extract_info
        cls._orig_normalize = jobs.normalize_youtube_url
        jobs.extract_info = lambda url: _fake_info()
        jobs.normalize_youtube_url = lambda url: url

    @classmethod
    def tearDownClass(cls):
        jobs.extract_info = cls._orig_extract
        jobs.normalize_youtube_url = cls._orig_normalize
        SERVER.__exit__()

    def _wait_for(self, job, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job.status in {"ready", "error", "cancelled"}:
                return job.status
            time.sleep(0.25)
        self.fail(f"job did not finish in {timeout}s (status={job.status}, error={job.error})")

    def test_progressive_video_download(self):
        job = jobs.manager.create(
            url="https://www.youtube.com/watch?v=localtest01",
            kind="video", format_id="18", container="mp4", label="360p",
            title="Pipeline Test", thumbnail="",
        )
        status = self._wait_for(job)
        self.assertEqual(status, "ready", f"job failed: {job.error}")
        self.assertTrue(job.filepath and Path(job.filepath).exists())
        self.assertGreater(job.size_bytes or 0, 1000)
        self.assertTrue(job.filename.endswith(".mp4"))
        # window-unfriendly characters must be gone from the filename
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, job.filename)

    def test_merged_video_audio_download(self):
        job = jobs.manager.create(
            url="https://www.youtube.com/watch?v=localtest01",
            kind="video", format_id="137", container="mp4", label="1080p",
            title="Merge Pipeline", thumbnail="",
        )
        status = self._wait_for(job)
        self.assertEqual(status, "ready", f"job failed: {job.error}")
        self.assertEqual(job.mode, "merge")
        # ffprobe the merged file: it must contain BOTH video and audio streams
        probe = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", job.filepath],
            capture_output=True, text=True,
        ).stderr
        self.assertIn("Video:", probe)
        self.assertIn("Audio:", probe)

    def test_audio_mp3_extraction(self):
        job = jobs.manager.create(
            url="https://www.youtube.com/watch?v=localtest01",
            kind="audio", format_id="140", container="mp3", label="MP3",
            title="Audio Pipeline", thumbnail="",
        )
        status = self._wait_for(job)
        self.assertEqual(status, "ready", f"job failed: {job.error}")
        self.assertTrue(job.filename.lower().endswith(".mp3"))
        head = Path(job.filepath).read_bytes()[:3]
        self.assertTrue(head == b"ID3" or head[:2] == b"\xff\xfb", "not an mp3 file")

    def test_duration_out_of_range_rejected(self):
        from server.ytdl import duration_error

        msg = duration_error(30)
        self.assertIn("دقیقه", msg)
        self.assertIn("5", msg)   # min bound mentioned
        self.assertIn("60", msg)  # max bound mentioned

    def test_out_of_range_job_ends_in_error_not_download(self):
        jobs.extract_info = lambda url: {**_fake_info(), "duration": 30}  # 30 seconds
        try:
            job = jobs.manager.create(
                url="https://www.youtube.com/watch?v=localtest01",
                kind="video", format_id="18", container="mp4", label="360p",
                title="Too short", thumbnail="",
            )
            status = self._wait_for(job, timeout=15)
            self.assertEqual(status, "error")
            self.assertIn("دقیقه", job.error or "")
        finally:
            jobs.extract_info = lambda url: _fake_info()


if __name__ == "__main__":
    unittest.main()
