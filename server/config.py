"""Central configuration for ARTOYOU, overridable via environment variables.

Values are read from the process environment; a local `.env` file (git-ignored)
is loaded automatically when present — perfect for the Telegram bot token.
"""

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:  # .env is optional — never required, never committed
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover - dotenv is best-effort
    pass

DOWNLOAD_DIR = Path(
    os.getenv("ARTOYOU_DOWNLOAD_DIR", str(BASE_DIR / "downloads"))
).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Video length guardrails (seconds). The user asked for 5 minutes – 1 hour.
MIN_DURATION = int(os.getenv("MIN_DURATION", str(5 * 60)))        # 300s = 5 min
MAX_DURATION = int(os.getenv("MAX_DURATION", str(60 * 60)))       # 3600s = 1 hour
ALLOW_OUT_OF_RANGE = os.getenv("ALLOW_OUT_OF_RANGE", "").strip().lower() in {"1", "true", "yes"}

# How long finished/failed job files are kept before automatic cleanup (seconds).
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", str(2 * 3600)))

# How many downloads may run at once.
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT", "1"))

# Optional shared-secret token. When set, every /api/* call (except /api/health)
# requires the `x-artoyou-token` header or a `token` query param.
APP_TOKEN = os.getenv("ARTOYOU_TOKEN", "").strip() or None

# Optional helpers for anti-bot checks on YouTube.
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "").strip() or None
YT_PLAYER_CLIENTS = [
    c.strip() for c in os.getenv("YT_PLAYER_CLIENT", "").split(",") if c.strip()
]
# Optional proxy for YouTube traffic only (e.g. "socks5h://127.0.0.1:10808").
# Useful where YouTube is unreachable directly. Telegram is NOT proxied by this —
# run the whole app under a system VPN/proxy for that, or use a local Bot API.
YT_PROXY = os.getenv("YT_PROXY", "").strip() or None

# Telegram bot (optional). When TELEGRAM_BOT_TOKEN is set, the bot starts
# inside the same process and shares the download queue with the web UI.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
TELEGRAM_ALLOWED_USERS = {
    int(part)
    for part in os.getenv("TELEGRAM_ALLOWED_USERS", "").replace(";", ",").split(",")
    if part.strip().isdigit()
}
# Self-hosted local Bot API server (allows >50MB uploads, up to 2GB).
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "").strip() or None
TELEGRAM_FILE_BASE_URL = os.getenv("TELEGRAM_FILE_BASE_URL", "").strip() or None

# Per-request network timeout (seconds) for metadata extraction.
SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", "30"))


def _find_ffmpeg():
    """Locate an ffmpeg binary: env var > system PATH > bundled imageio-ffmpeg."""
    env = os.getenv("FFMPEG_LOCATION", "").strip()
    if env:
        return env
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG_LOCATION = _find_ffmpeg()
