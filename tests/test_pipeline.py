"""
End-to-end pipeline tests for ARTOYOU — no YouTube network access required.

A tiny synthetic mp4/m4a is generated with ffmpeg and served over a local
HTTP server; yt-dlp's generic extractor downloads it through the real job
pipeline (progress hooks, ffmpeg merge, filename sanitisation, cleanup).

Run:  .venv/bin/python -m pytest tests/ -v
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Isolate downloads into a temp dir BEFORE importing server modules.
_TMP_DOWNLOADS = tempfile.mkdtemp(prefix="artoyou-test-downloads-")
os.environ["ARTOYOU_DOWNLOAD_DIR"] = _TMP_DOWNLOADS

import _media  # noqa: E402

from server import config, jobs  # noqa: E402
from server.ytdl import duration_error  # noqa: E402

FFMPEG = config.FFMPEG_LOCATION or _media.FFMPEG


class PipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_extract = jobs.extract_info
        cls._orig_normalize = jobs.normalize_youtube_url
        jobs.extract_info = lambda url: _media.fake_info()
        jobs.normalize_youtube_url = lambda url: url

    @classmethod
    def tearDownClass(cls):
        jobs.extract_info = cls._orig_extract
        jobs.normalize_youtube_url = cls._orig_normalize
        _media.SERVER.__exit__()

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
        msg = duration_error(30)
        self.assertIn("دقیقه", msg)
        self.assertIn("5", msg)   # min bound mentioned
        self.assertIn("60", msg)  # max bound mentioned

    def test_out_of_range_job_ends_in_error_not_download(self):
        jobs.extract_info = lambda url: {**_media.fake_info(), "duration": 30}  # 30 seconds
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
            jobs.extract_info = lambda url: _media.fake_info()


if __name__ == "__main__":
    unittest.main()
