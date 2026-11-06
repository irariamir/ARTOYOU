# ARTOYOU — دانلودر یوتیوب 🎬

سامانه شخصی و خودمیزبان برای دانلود ویدیوهای یوتیوب — لینک را بچسبان، کیفیت را انتخاب کن، تحویل بگیر.

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.162-009688?logo=fastapi)
![yt-dlp](https://img.shields.io/badge/yt--dlp-2026.10.26-red)
![License](https://img.shields.io/badge/license-MIT-green)

## ✨ ویژگی‌ها

- **بازه ۵ دقیقه تا ۱ ساعت** — ویدیوهای بلند (مستند، سخنرانی، پادکست تصویری) بدون محدودیت حجم
- **همه کیفیت‌ها از 360p تا 8K** — 4320p / 2160p / 1440p / 1080p / 720p / 480p / 360p
- **ترکیب خودکار تصویر + صدا** با ffmpeg برای کیفیت‌های بالای 720p (خروجی MP4 سازگار با همه پخش‌کننده‌ها)
- **استخراج صدا** به دو صورت: **M4A اصلی** (بدون افت کیفیت) و **MP3 تا 320k**
- **نوار پیشرفت زنده** — درصد، سرعت لحظه‌ای و زمان باقی‌مانده
- **صف دانلود همزمان** با لغو، حذف و تاریخچه دانلودها
- **ضدبات یوتیوب** — پشتیبانی از کوکی مرورگر (`YT_COOKIES_FILE`)
- **کاملاً شخصی** — بدون اکانت، بدون ترکینگ؛ امکان فعال‌سازی توکن دسترسی (`ARTOYOU_TOKEN`)
- **رابط فارسی راست‌به‌چپ** با طراحی تیره و واکنش‌گرا (موبایل و دسکتاپ) — فونت [وزیرمتن](https://github.com/rastikerdar/vazirmatn) (OFL) به صورت محلی و بدون CDN سرو می‌شود

## 🏗️ معماری

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────┐
│  مرورگر     │ ───► │  FastAPI (Python) │ ───► │   YouTube   │
│  (RTL UI)   │ ◄─── │  /api/info       │      │  (yt-dlp)   │
└─────────────┘      │  /api/download   │      └─────────────┘
                     │  /api/jobs/…     │ ───► ffmpeg (merge/MP3)
                     └──────────────────┘
```

```
ARTOYOU/
├── server/
│   ├── config.py          # تنظیمات محیطی (بازه مدت، کوکی، توکن، …)
│   ├── ytdl.py            # اعتبارسنجی لینک، استخراج متادیتا و گروه‌بندی کیفیت‌ها
│   ├── jobs.py            # مدیریت صف دانلود پس‌زمینه + پیشرفت + پاک‌سازی خودکار
│   ├── main.py            # اپ FastAPI: API ها + سرو اسناد استاتیک
│   ├── requirements.txt   # وابستگی‌های قفل‌شده (pinned)
│   └── static/            # فرانت‌اند (HTML/CSS/JS خالص — بدون build step)
├── tests/
│   └── test_pipeline.py   # تست انتها‌به‌انتها با مدیا‌ی مصنوعی (بدون نیاز به اینترنت)
├── start.sh               # اجرا با یک دستور
└── downloads/             # فایل‌های موقت دانلود (gitignore — حداکثر ۲ ساعت نگه داشته می‌شوند)
```

## 🚀 اجرا

### پیش‌نیاز
- Python ≥ 3.10
- ffmpeg — **نیازی به نصب جداگانه نیست**؛ [`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/) یک باینری ffmpeg همراه خودش می‌آورد (یا با `FFMPEG_LOCATION` مسیر یک ffmpeg سیستمی را بده).

### یک‌دستوره
```bash
./start.sh          # ساخت خودکار venv و نصب وابستگی‌ها + اجرا روی پورت 8000
PORT=9000 ./start.sh
```

### دستی
```bash
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
.venv/bin/python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
```

بعد برو به: **http://localhost:8000**

## ⚙️ تنظیمات محیطی

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `PORT` / `HOST` | `8000` / `0.0.0.0` | پورت و میزبان سرور (برای `start.sh`) |
| `MIN_DURATION` | `300` | حداقل مدت ویدیو به ثانیه (۵ دقیقه) |
| `MAX_DURATION` | `3600` | حداکثر مدت ویدیو به ثانیه (۱ ساعت) |
| `ALLOW_OUT_OF_RANGE` | — | `1` = غیرفعال‌سازی محدودیت مدت |
| `MAX_CONCURRENT` | `1` | تعداد دانلودهای همزمان |
| `JOB_TTL_SECONDS` | `7200` | نگه‌داری فایل‌های آماده قبل از پاک‌سازی خودکار |
| `ARTOYOU_DOWNLOAD_DIR` | `./downloads` | مسیر ذخیره موقت |
| `ARTOYOU_TOKEN` | — | اگر تنظیم شود، همه `/api/*` ها نیازمند هدر `x-artoyou-token` (یا `?token=`) می‌شوند |
| `YT_COOKIES_FILE` | — | مسیر فایل کوکی نت‌اسکیپ برای عبور از anti-bot یوتیوب |
| `YT_PLAYER_CLIENT` | — | کلاینت‌های جایگزین یوتیوب، مثلاً `android,web` |
| `FFMPEG_LOCATION` | خودکار | مسیر باینری ffmpeg |
| `SOCKET_TIMEOUT` | `30` | تایم‌اوت شبکه (ثانیه) |

### 🍪 اگر یوتیوب «Sign in to confirm you're not a bot» داد

کوکی مرورگرت را export کن و مسیرش را بده:

```bash
# با yt-dlp از کروم/فایرفاکس:
.venv/bin/yt-dlp --cookies-from-browser chrome --cookies cookies.txt "<any youtube url>" --skip-download
# یا با افزونه مرورگر «Get cookies.txt LOCALLY» و سپس:
YT_COOKIES_FILE=/path/to/cookies.txt ./start.sh
```

## 🔌 API

| روش | مسیر | توضیح |
|---|---|---|
| `GET` | `/api/health` | سلامت سرور، نسخه yt-dlp و ffmpeg، محدودیت‌ها |
| `POST` | `/api/info` | `{url}` → متادیتا + لیست کیفیت‌ها (حجم تقریبی هرکدام) |
| `POST` | `/api/download` | `{url, kind, format_id, container, label, title, thumbnail}` → `202 {job_id}` |
| `GET` | `/api/jobs` | لیست دانلودها با پیشرفت لحظه‌ای |
| `GET` | `/api/jobs/{id}` | وضعیت یک دانلود |
| `POST` | `/api/jobs/{id}/cancel` | لغو دانلود فعال |
| `DELETE` | `/api/jobs/{id}` | حذف دانلود (یا لغو اگر فعال است) |
| `GET` | `/api/jobs/{id}/file` | دریافت فایل نهایی |

## 🧪 تست‌ها

```bash
.venv/bin/pip install -r server/requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

تست‌ها با مدیا‌ی مصنوعی (ffmpeg testsrc) و یک HTTP سرور محلی اجرا می‌شوند — نیازی به اینترنت یا اکانت یوتیوب ندارند و کل مسیر (دانلود، پیشرفت، merge با ffmpeg، استخراج MP3، سانیتایز نام فایل) را واقعی تست می‌کنند.

## ⚠️ نکات و محدودیت‌ها

- فقط لینک **ویدیوی تکی** پذیرفته می‌شود (نه پلی‌لیست، نه کانال، نه استریم زنده).
- مدت ویدیو باید بین **۵ تا ۶۰ دقیقه** باشد (با `ALLOW_OUT_OF_RANGE=1` قابل تغییر است).
- کیفیت‌های بالای 720p در یوتیوب فقط به صورت جریان جداگانه (video-only) عرضه می‌شوند و با بهترین جریان صدا با ffmpeg ترکیب می‌شوند.
- فایل‌های دانلودشده بعد از `JOB_TTL_SECONDS` (پیش‌فرض ۲ ساعت) به‌صورت خودکار پاک می‌شوند.
- استفاده شخصی/غیرتجاری. مسئولیت رعایت شرایط استفاده یوتیوب و قوانین کپی‌رایت با کاربر است.

## 📄 لایسنس

MIT — ببینید [LICENSE](LICENSE).
