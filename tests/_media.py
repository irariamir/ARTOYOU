"""
Shared test fixtures: a tiny synthetic mp4/m4a served over a local HTTP server,
plus a fake yt-dlp info dict pointing at it.

No network access required — used by both the pipeline tests and the bot
end-to-end flow tests.
"""

import http.server
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import imageio_ffmpeg

DURATION = 620  # 10m20s — inside the 5–60 minute window

MEDIA_DIR = Path(tempfile.mkdtemp(prefix="artoyou-test-media-"))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


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


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass


class LocalMediaServer:
    def __init__(self, directory: Path):
        handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)
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


def fake_info() -> dict:
    """A realistic yt-dlp info dict whose format URLs point at the local server."""

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
        "thumbnail": "https://i.ytimg.com/vi/localtest01/hqdefault.jpg",
        "formats": [
            fmt("18", ext="mp4", vcodec="avc1.42001E", acodec="mp4a.40.2",
                height=180, tbr=300, file_key="combined", filesize=os.path.getsize(MEDIA["combined"])),
            fmt("137", ext="mp4", vcodec="avc1.64001F", acodec="none",
                height=180, tbr=250, file_key="video_only", filesize=os.path.getsize(MEDIA["video_only"])),
            fmt("140", ext="m4a", vcodec="none", acodec="mp4a.40.2",
                abr=128, tbr=129, file_key="audio_only", filesize=os.path.getsize(MEDIA["audio_only"])),
        ],
    }
