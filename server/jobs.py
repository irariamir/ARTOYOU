"""Background download job manager.

Each job downloads one video (single-file progressive, or video+audio merged
with ffmpeg) or extracts audio (M4A / MP3). Progress is tracked so the UI can
poll it. Finished files are kept for JOB_TTL_SECONDS and then swept.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yt_dlp

from . import config
from .ytdl import InfoError, duration_error, extract_info, normalize_youtube_url


class CancelDownload(Exception):
    """Raised inside the progress hook to abort a download."""


def _sanitize_filename_part(text: str, limit: int = 70) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return (cleaned[:limit].strip() or "video")


def _now() -> float:
    return time.time()


@dataclass
class Job:
    id: str
    url: str
    kind: str                    # "video" | "audio"
    format_id: str
    container: str               # "mp4" | "m4a" | "mp3"
    label: str                   # e.g. "1080p", "MP3"
    mode: str                    # "progressive" | "merge" | "audio"
    title: str = ""
    thumbnail: str = ""
    status: str = "queued"       # queued|downloading|processing|ready|error|cancelled
    progress: float = 0.0        # 0..1
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed: Optional[float] = None
    eta: Optional[float] = None
    error: Optional[str] = None
    filename: Optional[str] = None
    filepath: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: float = field(default_factory=_now)
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    prefetched_info: Optional[dict] = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "kind": self.kind,
            "container": self.container,
            "label": self.label,
            "mode": self.mode,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "status": self.status,
            "progress": round(self.progress, 4),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
        self._sweep_stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="artoyou-sweeper", daemon=True)
        self._sweeper.start()

    # ------------------------------------------------------------------ API
    def create(self, *, url: str, kind: str, format_id: str, container: str,
               label: str, title: str = "", thumbnail: str = "",
               info: Optional[dict] = None) -> Job:
        canonical = normalize_youtube_url(url)  # raises ValueError on bad input
        job = Job(
            id=uuid.uuid4().hex[:12],
            url=canonical,
            kind=kind,
            format_id=str(format_id),
            container=container,
            label=_sanitize_filename_part(label or "video", 24),
            mode="audio" if kind == "audio" else "progressive",  # refined at run time
            title=title or "",
            thumbnail=thumbnail or "",
            prefetched_info=info,
        )
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run_safely, args=(job,), name=f"job-{job.id}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 30) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def cancel(self, job_id: str) -> Optional[Job]:
        job = self.get(job_id)
        if not job:
            return None
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = _now()
        return job

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job.status in {"downloading", "processing"}:
            job.cancel_requested = True
            return True
        self._cleanup_job_dir(job)
        with self._lock:
            self._jobs.pop(job_id, None)
        return True

    # -------------------------------------------------------------- runner
    def _run_safely(self, job: Job) -> None:
        try:
            with self._semaphore:
                if job.cancel_requested or job.status == "cancelled":
                    job.status = "cancelled"
                    return
                self._run(job)
        except CancelDownload:
            job.status = "cancelled"
            job.error = "دانلود توسط کاربر لغو شد."
            job.finished_at = _now()
            self._cleanup_job_dir(job, keep_record=True)
        except ValueError as exc:  # validation errors (duration limits, bad url…)
            job.status = "error"
            job.error = str(exc)
            job.finished_at = _now()
            self._cleanup_job_dir(job, keep_record=True)
        except InfoError as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished_at = _now()
            self._cleanup_job_dir(job, keep_record=True)
        except Exception as exc:  # noqa: BLE001 — surface anything to the UI
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            msg = re.sub(r"^ERROR:\s*", "", msg).strip()
            job.status = "error"
            job.error = msg or "خطای ناشناخته در دانلود."
            job.finished_at = _now()
            self._cleanup_job_dir(job, keep_record=True)

    def _job_dir(self, job: Job) -> Path:
        return config.DOWNLOAD_DIR / f"job-{job.id}"

    def _run(self, job: Job) -> None:
        job.status = "downloading"

        # Reuse metadata already extracted for validation when available —
        # format URLs are fresh and this saves a full round-trip to YouTube.
        info = job.prefetched_info or extract_info(job.url)
        job.prefetched_info = None  # release the (potentially large) dict
        duration = int(info.get("duration") or 0)
        if not config.ALLOW_OUT_OF_RANGE and not (config.MIN_DURATION <= duration <= config.MAX_DURATION):
            raise ValueError(duration_error(duration))
        if info.get("title") and not job.title:
            job.title = info["title"]

        formats: list[dict] = info.get("formats") or []
        chosen = next((f for f in formats if str(f.get("format_id")) == job.format_id), None)

        job_dir = self._job_dir(job)
        job_dir.mkdir(parents=True, exist_ok=True)

        base = _sanitize_filename_part(job.title or info.get("title") or "video")
        suffix = _sanitize_filename_part(job.label, 24)
        outtmpl = str(job_dir / f"{base} [{suffix}].%(ext)s")

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": config.SOCKET_TIMEOUT,
            "outtmpl": outtmpl,
            "progress_hooks": [self._make_progress_hook(job)],
            "postprocessor_hooks": [self._make_pp_hook(job)],
            "windowsfilenames": True,
        }
        if config.YT_COOKIES_FILE:
            opts["cookiefile"] = config.YT_COOKIES_FILE
        if config.YT_PLAYER_CLIENTS:
            opts["extractor_args"] = {"youtube": {"player_client": config.YT_PLAYER_CLIENTS}}
        if config.FFMPEG_LOCATION:
            opts["ffmpeg_location"] = config.FFMPEG_LOCATION

        if job.kind == "audio":
            fmt = job.format_id if chosen else "bestaudio/best"
            opts["format"] = fmt
            if job.container == "mp3":
                if not config.FFMPEG_LOCATION:
                    raise ValueError("برای تبدیل به MP3 به ffmpeg نیاز است؛ روی سرور نصب نیست.")
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
                ]
            job.mode = "audio"
        else:
            if chosen is None:
                raise ValueError("فرمت انتخاب‌شده دیگر در دسترس نیست — صفحه را تازه کن و دوباره امتحان کن.")
            has_audio = chosen.get("acodec") not in (None, "none")
            if has_audio:
                opts["format"] = job.format_id
                job.mode = "progressive"
            else:
                if not config.FFMPEG_LOCATION:
                    raise ValueError("این کیفیت به ترکیب تصویر و صدا نیاز دارد و ffmpeg نصب نیست.")
                opts["format"] = f"{job.format_id}+bestaudio[ext=m4a]/bestaudio/best"
                opts["merge_output_format"] = "mp4"
                job.mode = "merge"
                job.status = "downloading"

        total_hint = chosen.get("filesize") or chosen.get("filesize_approx") if chosen else None
        if total_hint and job.mode != "merge":
            job.total_bytes = int(total_hint)

        # Hand the already-extracted info dict to yt-dlp instead of letting it
        # re-fetch the YouTube page — one extraction, then straight to download.
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.process_ie_result(dict(info), download=True)

        produced = self._find_produced_file(job_dir)
        if produced is None:
            raise RuntimeError("فایل خروجی پیدا نشد — دوباره تلاش کن.")

        job.filepath = str(produced)
        job.filename = produced.name
        job.size_bytes = produced.stat().st_size
        job.progress = 1.0
        job.downloaded_bytes = job.size_bytes
        job.total_bytes = job.size_bytes
        job.speed = None
        job.eta = None
        job.status = "ready"
        job.finished_at = _now()

    # -------------------------------------------------------------- hooks
    def _make_progress_hook(self, job: Job):
        def hook(d: dict) -> None:
            if job.cancel_requested:
                raise CancelDownload()
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job.total_bytes = int(total)
                    job.progress = min(max(downloaded / total, 0.0), 0.999)
                else:
                    job.progress = max(job.progress, 0.01)
                job.downloaded_bytes = int(downloaded)
                job.speed = d.get("speed")
                job.eta = d.get("eta")
                if job.status != "downloading":
                    job.status = "downloading"
            elif status == "finished":
                job.progress = max(job.progress, 0.99) if job.mode == "progressive" else job.progress
                job.speed = None
        return hook

    def _make_pp_hook(self, job: Job):
        def hook(d: dict) -> None:
            if job.cancel_requested:
                raise CancelDownload()
            if d.get("status") in {"started", "processing"}:
                job.status = "processing"
            elif d.get("status") == "finished":
                job.progress = max(job.progress, 0.995)
        return hook

    # ------------------------------------------------------------ cleanup
    @staticmethod
    def _find_produced_file(job_dir: Path) -> Optional[Path]:
        files = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not files:
            return None
        files.sort(key=lambda p: p.stat().st_size, reverse=True)
        return files[0]

    def _cleanup_job_dir(self, job: Job, keep_record: bool = False) -> None:
        job_dir = self._job_dir(job)
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        job.filepath = None

    def _sweep_loop(self) -> None:
        while not self._sweep_stop.wait(600):  # every 10 minutes
            self._sweep_once()

    def _sweep_once(self) -> None:
        now = _now()
        doomed: list[Job] = []
        with self._lock:
            for job in list(self._jobs.values()):
                if job.status in {"ready", "error", "cancelled"} and job.finished_at:
                    if now - job.finished_at > config.JOB_TTL_SECONDS:
                        doomed.append(job)
            for job in doomed:
                self._jobs.pop(job.id, None)
        for job in doomed:
            self._cleanup_job_dir(job)
        # Drop orphaned job-* directories older than the TTL.
        try:
            for child in config.DOWNLOAD_DIR.iterdir():
                if child.is_dir() and child.name.startswith("job-"):
                    if now - child.stat().st_mtime > config.JOB_TTL_SECONDS:
                        shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


manager = JobManager()
