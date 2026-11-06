"""YouTube URL validation, metadata extraction and format selection."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp

from . import config

_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "gaming.youtube.com",
    "tv.youtube.com",
}


class InfoError(RuntimeError):
    """Raised when video info cannot be extracted (network, bot-check, invalid url…)."""


def normalize_youtube_url(raw: str) -> str:
    """Validate a YouTube URL and return a canonical watch URL.

    Raises ValueError with a Persian message when the URL is not a YouTube video link.
    """
    url = (raw or "").strip()
    if not url:
        raise ValueError("لینک خالی است — اول یه لینک یوتیوب بچسبون!")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if not host:
        raise ValueError("لینک نامعتبر است — صورت کامل لینک (https://…) را بچسبان.")

    if host and host not in _YT_HOSTS and not host.endswith(".youtube.com"):
        raise ValueError("این لینک یوتیوب نیست — فقط لینک‌های youtube.com و youtu.be پذیرفته می‌شود.")

    video_id: Optional[str] = None
    if host.endswith("youtu.be"):
        candidate = path.strip("/").split("/")[0]
        if _YT_ID_RE.match(candidate):
            video_id = candidate
    else:
        qs = parse_qs(parsed.query)
        if _YT_ID_RE.match((qs.get("v") or [""])[0] or ""):
            video_id = qs["v"][0]
        else:
            match = re.match(r"^/(shorts|live|embed|v)/([A-Za-z0-9_-]{6,20})", path)
            if match:
                video_id = match.group(2)
            elif path.rstrip("/") == "/watch" and qs.get("v"):
                cand = qs["v"][0]
                if _YT_ID_RE.match(cand):
                    video_id = cand

    if not video_id:
        raise ValueError("شناسه ویدیو پیدا نشد — مطمئن شو لینک یه ویدیوی تکیه (نه پلی‌لیست یا کانال).")
    return f"https://www.youtube.com/watch?v={video_id}"


def _base_ydl_opts() -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": config.SOCKET_TIMEOUT,
        "retries": 2,
        "extract_flat": False,
    }
    if config.YT_COOKIES_FILE:
        opts["cookiefile"] = config.YT_COOKIES_FILE
    if config.YT_PLAYER_CLIENTS:
        opts["extractor_args"] = {"youtube": {"player_client": config.YT_PLAYER_CLIENTS}}
    if config.YT_PROXY:
        opts["proxy"] = config.YT_PROXY
    return opts


def extract_info(url: str) -> dict:
    opts = _base_ydl_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many subclasses of DownloadError
        msg = str(exc)
        msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # strip ANSI colors
        msg = re.sub(r"^ERROR:\s*", "", msg).strip()
        low = msg.lower()
        if any(s in low for s in ("sign in to confirm", "not a bot", "cookies")):
            msg = (
                "یوتیوب درخواست تأیید هویت (anti-bot) کرده است. "
                "کوکی مرورگرت را export کن و مسیرش را در متغیر محیطی YT_COOKIES_FILE بده."
            )
        elif any(s in low for s in ("unavailable", "private", "removed", "404")):
            msg = "این ویدیو در دسترس نیست — احتمالاً خصوصی، حذف‌شده یا منطقه‌مسدود است."
        elif any(s in low for s in ("getaddrinfo", "timed out", "connection", "network", "unreachable")):
            msg = "اتصال به یوتیوب برقرار نشد — وضعیت شبکه/فیلترشکن را بررسی کن و دوباره تلاش کن."
        raise InfoError(msg) from exc

    if info is None:
        raise InfoError("اطلاعات ویدیو دریافت نشد.")
    # Some URLs resolve to a playlist shell; re-raise with a useful message.
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
        if info is None:
            raise InfoError("پلی‌لیست‌ها پشتیبانی نمی‌شوند — فقط لینک ویدیوی تکی بفرست.")
    return info


def _size_of(fmt: dict) -> Optional[int]:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    duration = fmt.get("duration")
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * float(duration))
    return None


def _best_audio(formats: list[dict]) -> Optional[dict]:
    audios = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    if not audios:
        return None
    return max(audios, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))


def _video_rank(fmt: dict) -> tuple:
    ext_pref = {"mp4": 2, "webm": 1}.get((fmt.get("ext") or "").lower(), 0)
    codec = (fmt.get("vcodec") or "").lower()
    codec_pref = 2 if codec.startswith(("avc1", "h264")) else (1 if codec.startswith("vp9") else 0)
    return (ext_pref, codec_pref, fmt.get("tbr") or 0)


def build_options(info: dict) -> dict:
    """Group raw yt-dlp formats into a tidy per-resolution menu for the UI."""
    formats: list[dict] = info.get("formats") or []
    duration = info.get("duration") or 0
    audio = _best_audio(formats)
    audio_size = _size_of(audio) if audio else None
    if audio_size is None and audio and audio.get("abr"):
        audio_size = int(float(audio["abr"]) * 1000 / 8 * float(duration))

    by_height: dict[int, list[dict]] = {}
    for fmt in formats:
        if (fmt.get("vcodec") in (None, "none")):
            continue
        height = fmt.get("height") or 0
        if height <= 0:
            continue
        by_height.setdefault(int(height), []).append(fmt)

    options: list[dict] = []
    for height in sorted(by_height.keys(), reverse=True):
        group = by_height[height]
        combined = [f for f in group if f.get("acodec") not in (None, "none")]
        chosen: Optional[dict] = None
        mode = "merge"
        if combined:
            chosen = max(combined, key=_video_rank)
            mode = "progressive"  # single file, no ffmpeg needed
        else:
            chosen = max(group, key=_video_rank)
            mode = "merge"        # video-only stream, merged with best audio via ffmpeg

        size = _size_of(chosen)
        if not size and chosen.get("tbr"):
            size = int(float(chosen["tbr"]) * 1000 / 8 * float(duration))
        total = size + (audio_size if (mode == "merge" and audio_size) else 0) if size else (audio_size if mode == "merge" else None)

        fps = chosen.get("fps") or 0
        badge = "8K" if height >= 4320 else "4K" if height >= 2160 else "2K" if height >= 1440 else "HD" if height >= 720 else "SD"
        codec = (chosen.get("vcodec") or "").split(".")[0].upper()
        ext = "mp4" if mode == "merge" or (chosen.get("ext") or "").lower() == "mp4" else (chosen.get("ext") or "mp4")
        options.append(
            {
                "format_id": chosen.get("format_id"),
                "height": height,
                "label": f"{height}p" + (f"{int(fps)}" if fps and fps > 30 else ""),
                "badge": badge,
                "fps": fps,
                "ext": ext,
                "codec": codec,
                "size": int(total) if total else None,
                "mode": mode,
                "needs_ffmpeg": mode == "merge",
                "acodec": (chosen.get("acodec") or "none"),
            }
        )

    audio_formats = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    m4a_audio = max(
        (f for f in audio_formats if (f.get("ext") or "").lower() == "m4a"),
        key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0),
        default=None,
    )
    audio_options: list[dict] = []
    m4a_size = _size_of(m4a_audio) if m4a_audio else None
    if m4a_audio:
        abr = m4a_audio.get("abr") or m4a_audio.get("tbr") or 0
        audio_options.append(
            {
                "format_id": m4a_audio.get("format_id"),
                "container": "m4a",
                "label": "M4A",
                "sub": f"صوتی اصلی یوتیوب · ~{int(abr)}kbps" if abr else "صوتی اصلی یوتیوب",
                "size": m4a_size,
                "needs_ffmpeg": False,
            }
        )
    if audio and config.FFMPEG_LOCATION:
        audio_options.append(
            {
                "format_id": audio.get("format_id"),
                "container": "mp3",
                "label": "MP3",
                "sub": "تبدیل به MP3 با کیفیت بالا (320k)",
                "size": None,
                "needs_ffmpeg": True,
            }
        )

    return {"options": options, "audio": audio_options, "has_ffmpeg": bool(config.FFMPEG_LOCATION)}


def info_payload(url: str) -> dict:
    """Full payload the frontend needs to render the analyze screen."""
    info = extract_info(url)
    duration = int(info.get("duration") or 0)
    views = info.get("view_count")
    grouped = build_options(info)
    within = config.MIN_DURATION <= duration <= config.MAX_DURATION
    return {
        "title": info.get("title") or "بدون عنوان",
        "uploader": info.get("uploader") or info.get("channel") or "نامشخص",
        "uploader_url": info.get("uploader_url") or info.get("channel_url") or "",
        "duration": duration,
        "duration_string": info.get("duration_string") or _format_duration(duration),
        "views": views,
        "likes": info.get("like_count"),
        "thumbnail": info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url", ""),
        "webpage_url": info.get("webpage_url") or url,
        "upload_date": info.get("upload_date"),
        "within_limits": within or config.ALLOW_OUT_OF_RANGE,
        "limits": {"min": config.MIN_DURATION, "max": config.MAX_DURATION},
        "live_status": info.get("live_status"),
        **grouped,
    }


def _format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def duration_error(duration: int) -> str:
    mins = duration / 60
    return (
        f"طول این ویدیو {mins:.0f} دقیقه است؛ این سامانه فقط ویدیوهای "
        f"{config.MIN_DURATION // 60} تا {config.MAX_DURATION // 60} دقیقه را پردازش می‌کند."
    )
