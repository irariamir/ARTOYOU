"""ARTOYOU — FastAPI application exposing the YouTube downloader API + UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config
from .jobs import manager
from .ytdl import InfoError, duration_error, extract_info, info_payload, normalize_youtube_url

logger = logging.getLogger("artoyou")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Telegram bot (if configured) alongside the web server."""
    app.state.telegram_status = "disabled"
    bot_app = None
    if config.TELEGRAM_BOT_TOKEN:
        try:
            from .bot import build_bot_application

            bot_app = build_bot_application(config.TELEGRAM_BOT_TOKEN)
            await bot_app.initialize()
            await bot_app.start()
            if bot_app.updater is not None:
                await bot_app.updater.start_polling(
                    drop_pending_updates=True, allowed_updates=["message", "callback_query"]
                )
            app.state.telegram_status = "running"
            logger.info("Telegram bot started (polling).")
        except Exception:
            app.state.telegram_status = "error"
            logger.exception("Telegram bot failed to start — the web app keeps working.")
            bot_app = None
    else:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")

    yield

    if bot_app is not None:
        try:
            if bot_app.updater is not None and bot_app.updater.running:
                await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
            logger.info("Telegram bot stopped.")
        except Exception:
            logger.exception("Error while stopping the Telegram bot.")


app = FastAPI(title="ARTOYOU", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)


# --------------------------------------------------------------- security
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def token_guard(request: Request, call_next):
    if config.APP_TOKEN and request.url.path.startswith("/api") and request.url.path != "/api/health":
        token = request.headers.get("x-artoyou-token") or request.query_params.get("token")
        if token != config.APP_TOKEN:
            return JSONResponse({"detail": "توکن دسترسی نامعتبر است."}, status_code=401)
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # noqa: ARG001
    return JSONResponse({"detail": "خطای داخلی سرور رخ داد."}, status_code=500)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):  # noqa: ARG001
    return JSONResponse(
        {"detail": "ورودی نامعتبر است — لینک را کامل بچسبان و دوباره تلاش کن."},
        status_code=400,
    )


# ------------------------------------------------------------------ models
class InfoRequest(BaseModel):
    url: str = Field(min_length=3, max_length=500)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    kind: Literal["video", "audio"] = "video"
    format_id: str = Field(min_length=1, max_length=40)
    container: Literal["mp4", "m4a", "mp3", "webm", "mkv"] = "mp4"
    label: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=300)
    thumbnail: str = Field(default="", max_length=1000)


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health(request: Request):
    return {
        "status": "ok",
        "version": __version__,
        "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": bool(config.FFMPEG_LOCATION),
        "limits": {"min": config.MIN_DURATION, "max": config.MAX_DURATION},
        "auth": bool(config.APP_TOKEN),
        "telegram": getattr(request.app.state, "telegram_status", "disabled"),
    }


@app.post("/api/info")
def api_info(payload: InfoRequest):
    try:
        url = normalize_youtube_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        data = info_payload(url)
    except InfoError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return data


@app.post("/api/download", status_code=202)
def api_download(payload: DownloadRequest):
    try:
        url = normalize_youtube_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Re-validate against fresh metadata: the format must exist and the
    # duration must respect the configured 5–60 minute window (unless bypassed).
    try:
        info = extract_info(url)
    except InfoError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    duration = int(info.get("duration") or 0)
    if not config.ALLOW_OUT_OF_RANGE and not (config.MIN_DURATION <= duration <= config.MAX_DURATION):
        raise HTTPException(status_code=422, detail=duration_error(duration))

    formats = info.get("formats") or []
    if payload.kind == "video":
        chosen = next((f for f in formats if str(f.get("format_id")) == payload.format_id), None)
        if chosen is None or (chosen.get("vcodec") in (None, "none")):
            raise HTTPException(status_code=400, detail="فرمت انتخاب‌شده در دسترس نیست — صفحه را تازه کن.")
        container = "mp4"
    else:
        audios = [
            f for f in formats
            if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
        ]
        chosen = next((f for f in audios if str(f.get("format_id")) == payload.format_id), None) or (
            max(audios, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0)) if audios else None
        )
        if chosen is None:
            raise HTTPException(status_code=400, detail="فرمت صوتی در دسترس نیست — صفحه را تازه کن.")
        container = payload.container if payload.container in {"mp3", "m4a"} else "m4a"
        if container == "mp3" and not config.FFMPEG_LOCATION:
            raise HTTPException(status_code=400, detail="تبدیل به MP3 به ffmpeg نیاز دارد که روی سرور پیدا نشد.")

    title = payload.title.strip() or info.get("title") or "video"
    job = manager.create(
        url=url,
        kind=payload.kind,
        format_id=str(chosen.get("format_id")),
        container=container,
        label=payload.label or ("MP3" if container == "mp3" else payload.label or container.upper()),
        title=title,
        thumbnail=payload.thumbnail,
        info=info,
    )
    return {"job_id": job.id, "status": job.status}


@app.get("/api/jobs")
def api_jobs(limit: int = 30):
    limit = max(1, min(limit, 100))
    return {"jobs": [j.to_dict() for j in manager.list(limit)]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="این دانلود پیدا نشد — احتمالاً پاک شده است.")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="این دانلود پیدا نشد.")
    if job.status in {"downloading", "processing"}:
        manager.cancel(job_id)
        return {"status": "cancelling"}
    manager.remove(job_id)
    return {"status": "removed"}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    job = manager.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="این دانلود پیدا نشد.")
    return {"status": job.status}


@app.get("/api/jobs/{job_id}/file")
def api_download_file(job_id: str):
    job = manager.get(job_id)
    if not job or job.status != "ready" or not job.filepath:
        raise HTTPException(status_code=404, detail="فایل آماده نیست یا منقضی شده است.")
    path = Path(job.filepath)
    if not path.exists():
        raise HTTPException(status_code=410, detail="فایل از روی سرور پاک شده است.")
    filename = job.filename or path.name
    media_type = "application/octet-stream"
    if filename.lower().endswith(".mp3"):
        media_type = "audio/mpeg"
    elif filename.lower().endswith((".m4a", ".mp4")):
        media_type = "audio/mp4" if filename.lower().endswith(".m4a") else "video/mp4"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={"Content-Length": str(path.stat().st_size)},
    )


# ------------------------------------------------------------------ static
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
