"""ARTOYOU — Telegram bot.

Persian-speaking users can simply send a YouTube link to the bot:
it analyzes the video, shows an inline quality menu (360p → 8K + MP3),
downloads in the background with a live progress bar, then sends the
file back into the chat (or explains why it cannot).

The bot runs INSIDE the FastAPI process (started from the app lifespan),
so jobs it creates are the same JobManager jobs shown in the web UI.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .jobs import manager
from .ytdl import InfoError, build_options, duration_error, extract_info, normalize_youtube_url

logger = logging.getLogger("artoyou.bot")

CACHE_TTL_SECONDS = 20 * 60
MAX_CACHE_ENTRIES = 50
MAX_VIDEO_QUALITIES = 8          # keep the keyboard tidy
PROGRESS_EDIT_INTERVAL = 4.0     # seconds between message edits (flood control)

_ANALYSIS_CACHE: dict[str, dict] = {}
_PROGRESS_TASKS: set[asyncio.Task] = set()

# Upload ceiling: 48MB on the cloud Bot API (50MB hard limit),
# ~1.9GB when pointed at a self-hosted local Bot API server.
def max_send_bytes() -> int:
    return (1900 if config.TELEGRAM_API_BASE_URL else 48) * 1024 * 1024


# ───────────────────────────────────────────────────────────── cache
def _cache_put(entry: dict) -> str:
    _cache_evict()
    if len(_ANALYSIS_CACHE) >= MAX_CACHE_ENTRIES:
        oldest = min(_ANALYSIS_CACHE, key=lambda k: _ANALYSIS_CACHE[k]["created_at"])
        _ANALYSIS_CACHE.pop(oldest, None)
    key = uuid.uuid4().hex[:10]
    _ANALYSIS_CACHE[key] = {**entry, "created_at": time.time()}
    return key


def _cache_get(key: str) -> Optional[dict]:
    entry = _ANALYSIS_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["created_at"] > CACHE_TTL_SECONDS:
        _ANALYSIS_CACHE.pop(key, None)
        return None
    return entry


def _cache_pop(key: str) -> None:
    _ANALYSIS_CACHE.pop(key, None)


def _cache_evict() -> None:
    now = time.time()
    for key in list(_ANALYSIS_CACHE):
        if now - _ANALYSIS_CACHE[key]["created_at"] > CACHE_TTL_SECONDS:
            _ANALYSIS_CACHE.pop(key, None)


# ─────────────────────────────────────────────────────────── helpers
def _fmt_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "? MB"
    mb = num_bytes / (1024 * 1024)
    return f"{mb:.0f} MB" if mb < 1024 else f"{mb / 1024:.1f} GB"


def _progress_bar(percent: float, width: int = 14) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = round(percent / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _short_title(title: str, limit: int = 120) -> str:
    title = (title or "بدون عنوان").strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def _parse_callback(data: str) -> tuple[str, Optional[str], Optional[int]]:
    parts = (data or "").split(":")
    kind = parts[0] if parts else ""
    key = parts[1] if len(parts) > 1 else None
    try:
        idx = int(parts[2]) if len(parts) > 2 else None
    except ValueError:
        idx = None
    return kind, key, idx


def _is_allowed(user_id: Optional[int]) -> bool:
    allowed = config.TELEGRAM_ALLOWED_USERS
    if not allowed:
        return True  # open bot — see README to restrict
    return user_id in allowed


def _extract_url(text: str) -> Optional[str]:
    for token in text.split():
        token = token.strip().rstrip(").,،؛")
        if token.startswith(("http://", "https://", "youtu.be/", "www.youtube.com")):
            return token
    return None


# ────────────────────────────────────────────────────────── handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        await _deny(update)
        return
    name = html.escape((user.first_name if user else "") or "دوست")
    await update.message.reply_html(
        f"سلام {name}! 👋\n\n"
        "من <b>ARTOYOU</b> هستم — دانلودر یوتیوب اختصاصی تو.\n\n"
        "📎 فقط <b>لینک ویدیوی یوتیوب</b> را برام بفرست و کیفیت دلخواهت را انتخاب کن:\n"
        "• فیلم‌ها از <b>۵ دقیقه تا ۱ ساعت</b>\n"
        "• همه کیفیت‌ها از <b>360p تا 8K</b>\n"
        "• یا فقط صدا: <b>MP3</b> / <b>M4A</b>\n\n"
        "مثال: <code>https://youtu.be/dQw4w9WgXcQ</code>"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        await _deny(update)
        return
    await update.message.reply_html(
        "<b>راهنمای استفاده</b>\n\n"
        "1️⃣ لینک ویدیوی تکی یوتیوب (watch یا youtu.be) را بفرست\n"
        "2️⃣ کیفیت را از دکمه‌ها انتخاب کن\n"
        "3️⃣ پیشرفت دانلود را ببین و در پایان فایل را همینجا تحویل بگیر\n\n"
        "محدودیت‌ها: مدت ویدیو ۵ تا ۶۰ دقیقه · فقط ویدیوی تکی (نه پلی‌لیست)\n"
        "⚠️ حجم ارسال در تلگرام ۵۰ مگابایت است؛ فایل‌های بزرگ‌تر با Bot API محلی پشتیبانی می‌شود.\n"
        "برای لغو هر دانلود، دکمه «لغو دانلود» زیر خود پیام پیشرفت ظاهر می‌شود."
    )


async def _deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_html(
        "⛔️ این بات <b>خصوصی</b> است و شما دسترسی ندارید.\n\n"
        f"شناسه عددی شما: <code>{uid}</code>\n"
        "برای فعال‌سازی، این شناسه را به متغیر <code>TELEGRAM_ALLOWED_USERS</code> روی سرور اضافه کن."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        await _deny(update)
        return

    text = (update.message.text or "").strip()
    raw_url = _extract_url(text)
    if not raw_url:
        await update.message.reply_text(
            "🤔 لینکی پیدا نکردم! یه لینک یوتیوب مثل این بفرست:\nhttps://www.youtube.com/watch?v=…\nhttps://youtu.be/…"
        )
        return

    try:
        url = normalize_youtube_url(raw_url)
    except ValueError as exc:
        await update.message.reply_html(f"❌ {html.escape(str(exc))}")
        return

    status_msg = await update.message.reply_html("🔍 <b>در حال تحلیل ویدیو…</b> چند ثانیه صبر کن")

    try:
        info = await asyncio.to_thread(extract_info, url)
    except InfoError as exc:
        await status_msg.edit_text(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("info extraction failed for %s", url)
        await status_msg.edit_text("⚠️ خطای غیرمنتظره در تحلیل ویدیو — دوباره تلاش کن.")
        return

    duration = int(info.get("duration") or 0)
    if not config.ALLOW_OUT_OF_RANGE and not (config.MIN_DURATION <= duration <= config.MAX_DURATION):
        await status_msg.edit_text(f"⚠️ {duration_error(duration)}")
        return

    title = _short_title(info.get("title") or "")
    duration_str = info.get("duration_string") or f"{duration // 60}:{duration % 60:02d}"
    views = info.get("view_count")
    uploader = info.get("uploader") or info.get("channel") or ""
    thumbnail = info.get("thumbnail") or ""

    grouped = build_options(info)
    options = grouped["options"][:MAX_VIDEO_QUALITIES]
    audio_options = grouped["audio"]

    cache_key = _cache_put(
        {
            "url": url,
            "info": info,
            "title": title,
            "thumbnail": thumbnail,
            "options": options,
            "audio": audio_options,
        }
    )

    views_str = f" · 👁 {views:,}" if views else ""
    caption = (
        f"<b>{html.escape(title)}</b>\n\n"
        f"👤 {html.escape(uploader)}\n"
        f"⏱ {html.escape(str(duration_str))}{views_str}\n\n"
        "کیفیت موردنظرت را انتخاب کن 👇"
    )

    keyboard_rows = []
    for i in range(0, len(options), 2):
        row = []
        for j, opt in enumerate(options[i : i + 2]):
            idx = i + j
            size = _fmt_size(opt.get("size"))
            row.append(
                InlineKeyboardButton(
                    f"🎬 {opt['label']} · {size}", callback_data=f"v:{cache_key}:{idx}"
                )
            )
        keyboard_rows.append(row)
    if audio_options:
        keyboard_rows.append(
            [
                InlineKeyboardButton(f"🎵 {opt['label']}", callback_data=f"a:{cache_key}:{idx}")
                for idx, opt in enumerate(audio_options[:2])
            ]
        )
    keyboard_rows.append([InlineKeyboardButton("❌ انصراف", callback_data=f"c:{cache_key}")])
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    try:
        await status_msg.delete()
    except TelegramError:
        pass
    try:
        if thumbnail:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, photo=thumbnail, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        else:
            await update.message.reply_html(caption, reply_markup=keyboard)
    except BadRequest:
        # Thumbnail may be rejected (size/host) — fall back to a text card.
        await update.message.reply_html(caption, reply_markup=keyboard)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        await query.answer("⛔️ دسترسی ندارید.")
        return

    kind, key, idx = _parse_callback(query.data or "")

    if kind == "j":  # cancel a running job from the progress message
        manager.cancel(key)
        await query.answer("لغو درخواست شد…")
        return

    entry = _cache_get(key) if key else None
    if kind == "c":
        _cache_pop(key)
        await query.answer("لغو شد.")
        try:
            await query.edit_message_caption(caption="🚫 عملیات لغو شد.")
        except BadRequest:
            try:
                await query.edit_message_text("🚫 عملیات لغو شد.")
            except BadRequest:
                pass
        return

    if entry is None:
        await query.answer("این تحلیل منقضی شده — لینک را دوباره بفرست. ♻️", show_alert=True)
        return

    if kind == "v" and idx is not None and 0 <= idx < len(entry["options"]):
        opt = entry["options"][idx]
        job_kind, container, label = "video", "mp4", opt["label"]
    elif kind == "a" and idx is not None and 0 <= idx < len(entry["audio"]):
        opt = entry["audio"][idx]
        job_kind, container, label = "audio", opt["container"], opt["label"]
    else:
        await query.answer("گزینه نامعتبر است.")
        return

    await query.answer("🎬 دانلود شروع شد!")
    _cache_pop(key)

    job = manager.create(
        url=entry["url"],
        kind=job_kind,
        format_id=str(opt["format_id"]),
        container=container,
        label=label,
        title=entry["title"],
        thumbnail=entry["thumbnail"],
        info=entry["info"],
    )

    chat_id = update.effective_chat.id
    progress_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=_progress_text(entry["title"], label, 0.0, None, None, "queued"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ لغو دانلود", callback_data=f"j:{job.id}")]]
        ),
    )
    task = asyncio.create_task(_watch_job(context.application, chat_id, progress_msg.message_id, job.id, entry["title"], label))
    _PROGRESS_TASKS.add(task)
    task.add_done_callback(_PROGRESS_TASKS.discard)


# ──────────────────────────────────────────────────────── progress
def _progress_text(title, label, percent, speed, eta, status) -> str:
    safe_title = html.escape(_short_title(title, 80))
    if status == "queued":
        return f"⏳ <b>{safe_title}</b>\nکیفیت: <code>{html.escape(label)}</code>\n\nدر صف منتظر نوبت است…"
    if status == "processing":
        return f"⚙️ <b>{safe_title}</b>\nکیفیت: <code>{html.escape(label)}</code>\n\nدر حال ترکیب/تبدیل نهایی با ffmpeg…"
    speed_str = f"⚡ {speed / (1024 * 1024):.1f} MB/s  ·  " if speed else ""
    eta_str = f"⏱ {int(eta)}s" if eta else ""
    return (
        f"⏬ <b>{safe_title}</b>\nکیفیت: <code>{html.escape(label)}</code>\n\n"
        f"<code>{_progress_bar(percent)}</code> <b>{percent:.1f}%</b>\n"
        f"{speed_str}{eta_str}".rstrip()
    )


async def _watch_job(bot, chat_id, message_id, job_id, title, label) -> None:
    last_text = None
    last_edit = 0.0
    while True:
        await asyncio.sleep(2.0)
        job = manager.get(job_id)
        if job is None:
            return

        if job.status in {"queued", "downloading", "processing"}:
            percent = 100.0 * (job.progress or 0.0)
            if job.status == "processing":
                text = _progress_text(title, label, 99.0, None, None, "processing")
            elif job.status == "queued":
                text = _progress_text(title, label, 0.0, None, None, "queued")
            else:
                text = _progress_text(title, label, percent, job.speed, job.eta, "downloading")
            now = time.time()
            if text != last_text and now - last_edit >= PROGRESS_EDIT_INTERVAL:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("❌ لغو دانلود", callback_data=f"j:{job.id}")]]
                        ),
                    )
                    last_text, last_edit = text, now
                except BadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        logger.debug("progress edit skipped: %s", exc)
                except NetworkError:
                    logger.debug("progress edit network error — will retry")
            continue

        # Terminal states
        safe_title = html.escape(_short_title(title, 80))
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

        if job.status == "ready":
            await _deliver_file(bot, chat_id, job, safe_title, label)
        elif job.status == "cancelled":
            await bot.send_message(chat_id=chat_id, text=f"🚫 دانلود <b>{safe_title}</b> لغو شد.", parse_mode=ParseMode.HTML)
        else:
            error_text = html.escape(job.error or "خطای ناشناخته")
            await bot.send_message(chat_id=chat_id, text=f"❌ <b>دانلود ناموفق بود</b>\n{error_text}", parse_mode=ParseMode.HTML)
        return


async def _deliver_file(bot, chat_id, job, safe_title, label) -> None:
    size = job.size_bytes or 0
    limit = max_send_bytes()
    caption = f"<b>{safe_title}</b>\n{html.escape(label)} · {_fmt_size(size)}"

    if size > limit:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ فایل آماده شد ولی <b>{_fmt_size(size)}</b> است.\n"
                f"تلگرام اجازه ارسال بیش از <b>{_fmt_size(limit)}</b> را نمی‌دهد.\n\n"
                "راه‌حل‌ها:\n• همین ویدیو را با کیفیت پایین‌تر دوباره دانلود کن\n"
                "• یا از رابط وب ARTOYOU فایل را مستقیم بگیر\n"
                "• یا یک Bot API محلی راه بینداز (تا ۲ گیگابایت) — راهنما در README"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    filename = (job.filename or "file").lower()
    try:
        if job.kind == "video" and filename.endswith((".mp4", ".mkv", ".mov")):
            await bot.send_video(
                chat_id=chat_id, video=job.filepath, caption=caption,
                parse_mode=ParseMode.HTML, supports_streaming=True, read_timeout=900, write_timeout=900,
            )
        elif filename.endswith((".mp3", ".m4a", ".opus", ".ogg")):
            await bot.send_audio(
                chat_id=chat_id, audio=job.filepath, caption=caption,
                parse_mode=ParseMode.HTML, read_timeout=900, write_timeout=900,
            )
        else:
            await bot.send_document(
                chat_id=chat_id, document=job.filepath, caption=caption,
                parse_mode=ParseMode.HTML, read_timeout=900, write_timeout=900,
            )
    except BadRequest as exc:
        logger.warning("send_file rejected: %s", exc)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ ارسال فایل در تلگرام رد شد: {html.escape(str(exc))}\n"
                "می‌توانی همان فایل را از رابط وب ARTOYOU دانلود کنی."
            ),
            parse_mode=ParseMode.HTML,
        )
    except NetworkError:
        logger.exception("send_file network error")
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ اتصال به تلگرام هنگام ارسال فایل قطع شد — فایل روی سرور آماده است و از رابط وب قابل دریافت است.",
        )


# ───────────────────────────────────────────────────────── bootstrap
def build_bot_application(token: str) -> Application:
    builder = Application.builder().token(token).concurrent_updates(True)
    if config.TELEGRAM_API_BASE_URL:
        builder = builder.base_url(config.TELEGRAM_API_BASE_URL.rstrip("/") + "/bot")
        builder = builder.base_file_url(
            (config.TELEGRAM_FILE_BASE_URL or config.TELEGRAM_API_BASE_URL).rstrip("/") + "/file/bot"
        )
    application = builder.connect_timeout(60).read_timeout(120).write_timeout(900).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_handler(CallbackQueryHandler(handle_callback))
    return application
