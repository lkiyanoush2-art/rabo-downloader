import os
import asyncio
import re
import time
from typing import Dict, Any, Optional

from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import yt_dlp

# ============================================================================
# Configurations & Credentials
# ============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8993469100:AAGZ2rJktVIavAIgWq_s1xCVpqPAg_3YMsw")
API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")

PORT = int(os.getenv("PORT", "10000"))
DOWNLOAD_DIR = "/app/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory session cache for URL selections
CACHE: Dict[str, Dict[str, Any]] = {}

# ============================================================================
# Pyrogram Telegram Bot Client (MTProto - Supports up to 2 GB)
# ============================================================================
bot = Client(
    "rabo_render_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="/app",
)

# ============================================================================
# FastAPI Health Service (For Render 24/7 Keep-Alive via UptimeRobot)
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 [Startup] Initializing Telegram Downloader Service...", flush=True)

    # 1. Clean up any leftover webhooks so Pyrogram MTProto receives all updates
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true") as resp:
                res = await resp.json()
                print(f"🗑️ [Webhook Cleanup] {res}", flush=True)
    except Exception as e:
        print(f"⚠️ [Webhook Cleanup Warning] {e}", flush=True)

    # 2. Start Pyrogram MTProto Bot Client
    print("🤖 [Startup] Connecting Pyrogram MTProto Bot Client to Telegram...", flush=True)
    try:
        await bot.start()
        me = await bot.get_me()
        print(f"✅ [Online] Bot @{me.username} ({me.first_name}) is fully active and listening!", flush=True)
    except Exception as e:
        print(f"❌ [Error] Failed to start Pyrogram Bot: {e}", flush=True)
        import traceback
        traceback.print_exc()

    yield

    print("🛑 [Shutdown] Stopping Pyrogram Bot...", flush=True)
    try:
        await bot.stop()
    except Exception as e:
        print(f"⚠️ [Shutdown Warning] {e}", flush=True)

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health_check():
    return {
        "status": "online",
        "service": "Telegram 2GB Downloader Bot",
        "platform": "Render.com Free Tier (Keep-Alive Active)",
        "max_file_size": "2000 MB (2 GB)",
    }

@app.get("/delete-webhook")
async def trigger_delete_webhook():
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true") as resp:
                data = await resp.json()
                return {"status": "ok", "telegram_response": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def format_bytes(size: int) -> str:
    if not size or size <= 0:
        return "2.9 MB"
    if size >= 1024 * 1024:
        mb = size / (1024 * 1024)
        return f"{round(mb)} MB" if mb >= 10 else f"{mb:.1f} MB"
    return f"{(size / 1024):.1f} KB"

def format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "0:15"
    m = seconds // 60
    s = seconds % 60
    return f"{m}:{s:02d}"

async def progress_tracker(current: int, total: int, status_msg: Message, action_name: str, start_time: float):
    now = time.time()
    if not hasattr(progress_tracker, "last_edit"):
        progress_tracker.last_edit = 0

    if now - progress_tracker.last_edit < 3 and current < total:
        return
    progress_tracker.last_edit = now

    percent = (current / total) * 100 if total > 0 else 0
    bar_len = 10
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (bar_len - filled)

    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    speed_str = f"{format_bytes(int(speed))}/s"

    text = (
        f"⚡ <b>{action_name}...</b>\n\n"
        f"<code>[{bar}] {percent:.1f}%</code>\n"
        f"📊 <b>حجم:</b> <code>{format_bytes(current)} / {format_bytes(total)}</code>\n"
        f"🚀 <b>سرعت:</b> <code>{speed_str}</code>"
    )
    try:
        await status_msg.edit_text(text)
    except Exception:
        pass

# ============================================================================
# Telegram Handlers
# ============================================================================
@bot.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    welcome = (
        "👋 <b>به ربات دانلود مدیا تا سقف ۲ گیگابایت خوش آمدید!</b>\n\n"
        "⚡ <i>بدون محدودیت ۵۰ مگابایت — قابلیت دانلود و ارسال تا ۲۰۰۰ مگابایت!</i>\n\n"
        "🚀 <b>سایت‌های پشتیبانی‌شده:</b>\n"
        "• اینستاگرام (Reels، پست‌ها، استوری)\n"
        "• توییتر / X (تمام کیفیت‌های 1080p, 720p, 480p)\n"
        "• یوتیوب (کیفیت‌های HD و 4K تا ۲ گیگ)\n"
        "• تیک‌تاک (بدون واترمارک HD)\n"
        "• تمام لینک‌های مستقیم وب و ویدیوها\n\n"
        "📥 <b>کافیست لینک ویدیوی مورد نظر را در چت ارسال کنید!</b>"
    )
    await message.reply_text(welcome)

async def resolve_bunkr(url: str) -> Optional[Dict[str, Any]]:
    domain_match = re.search(r"https?://([^/]+)", url)
    referer = f"https://{domain_match.group(1)}/" if domain_match else "https://bunkr.cr/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        import aiohttp
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

                vid_match = (
                    re.search(r'<source[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE) or
                    re.search(r'<video[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE) or
                    re.search(r'href=["\']([^"\']*(?:media-files|\.mp4|\.mkv|\.mov)[^"\']*)["\']', html, re.IGNORECASE) or
                    re.search(r'["\']downloadUrl["\']\s*:\s*["\']([^"\']+)["\']', html) or
                    re.search(r'["\']file["\']\s*:\s*["\']([^"\']+(?:\.mp4|\.mkv|\.mov)[^"\']*)["\']', html)
                )

                if vid_match:
                    video_url = vid_match.group(1)
                    if video_url.startswith("//"):
                        video_url = "https:" + video_url
                    elif video_url.startswith("/"):
                        host = domain_match.group(1) if domain_match else "bunkr.cr"
                        video_url = f"https://{host}{video_url}"

                    title_match = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE) or re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.IGNORECASE)
                    title = title_match.group(1).strip() if title_match else "Bunkr Video"
                    title = re.sub(r'\s*\|\s*Bunkr.*$', '', title, flags=re.IGNORECASE).strip()

                    poster_match = re.search(r'poster=["\']([^"\']+)["\']', html, re.IGNORECASE)
                    poster = poster_match.group(1) if poster_match else None

                    filesize = 0
                    try:
                        async with session.head(video_url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as head_resp:
                            if "Content-Length" in head_resp.headers:
                                filesize = int(head_resp.headers["Content-Length"])
                    except Exception:
                        pass

                    return {
                        "url": video_url,
                        "title": title,
                        "uploader": "Bunkr",
                        "duration": 0,
                        "filesize": filesize,
                        "thumbnail": poster,
                        "is_direct": True,
                        "referer": referer,
                    }
    except Exception as e:
        print(f"[Bunkr] Resolution error: {e}")
    return None

@bot.on_message(filters.regex(r"https?://[^\s]+"))
async def link_handler(_, message: Message):
    url = re.search(r"https?://[^\s]+", message.text).group(0)
    status_msg = await message.reply_text("🔎 <b>در حال تحلیل لینک و دریافت مشخصات...</b>")

    loop = asyncio.get_event_loop()

    info = None
    if re.search(r"bunkr\.[a-z]+|bunkrr\.[a-z]+", url, re.IGNORECASE):
        info = await resolve_bunkr(url)

    if not info:
        def extract_info():
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await loop.run_in_executor(None, extract_info)
        except Exception as e:
            await status_msg.edit_text(f"❌ <b>خطا در دریافت ویدیو:</b>\n<code>{str(e)[:150]}</code>")
            return

    if not info:
        await status_msg.edit_text("❌ ویدیویی در این لینک یافت نشد.")
        return

    cache_id = str(int(time.time() * 1000))
    CACHE[cache_id] = {
        "url": url,
        "direct_url": info.get("url") if info.get("is_direct") else None,
        "is_direct": info.get("is_direct", False),
        "referer": info.get("referer"),
        "title": info.get("title", "Video"),
        "uploader": info.get("uploader") or info.get("channel") or "Video",
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
    }

    uploader = info.get("uploader") or info.get("channel") or "Video"
    duration_str = format_duration(info.get("duration"))
    filesize_approx = format_bytes(info.get("filesize") or info.get("filesize_approx") or 3000000)

    menu_text = (
        f"<b>{uploader}</b>\n"
        f"[{duration_str}]\n\n"
        f"📹 <b>Video</b>\n"
        f"1. <i>mp4, 1080p [{filesize_approx}]</i> ⚡\n"
        f"2. <i>mp4, 720p [2 MB]</i>\n\n"
        f"🎧 <b>Audio</b>\n"
        f"3. <i>m4a, 64kbps, 44kHz [131.1 KB]</i>\n\n"
        f"ⓘ <i>Formats 1080p not compatible with Apple iPhone</i>"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📹 1080p", callback_data=f"dl:{cache_id}:1080"),
            InlineKeyboardButton("📹 720p", callback_data=f"dl:{cache_id}:720"),
            InlineKeyboardButton("♫ 64kbps", callback_data=f"dl:{cache_id}:audio"),
        ],
        [
            InlineKeyboardButton("⟳ Refresh metadata", callback_data=f"refresh:{cache_id}"),
        ],
        [
            InlineKeyboardButton("← Back", callback_data="back"),
        ],
    ])

    await status_msg.delete()

    thumb_url = info.get("thumbnail")
    if thumb_url:
        try:
            await message.reply_photo(photo=thumb_url, caption=menu_text, reply_markup=buttons)
            return
        except Exception:
            pass

    await message.reply_text(menu_text, reply_markup=buttons)

@bot.on_callback_query()
async def callback_handler(client: Client, cq: CallbackQuery):
    data = cq.data or ""

    if data == "back":
        await cq.answer("بازگشت")
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if data.startswith("refresh:"):
        await cq.answer("مشخصات بازخوانی شد ✅")
        return

    if not data.startswith("dl:"):
        return

    _, cache_id, quality = data.split(":")
    item = CACHE.get(cache_id)
    if not item:
        await cq.answer("لینک منقضی شده است. لطفاً دوباره آن را ارسال کنید.", show_alert=True)
        return

    await cq.answer("⏳ در حال دانلود و آماده‌سازی...")
    status_msg = await cq.message.reply_text(f"⏳ <b>در حال دانلود کیفیت {quality} بر روی سرور...</b>")

    target_url = item["url"]
    file_id = f"{cache_id}_{quality}"
    loop = asyncio.get_event_loop()

    is_direct = item.get("is_direct", False)
    direct_url = item.get("direct_url")

    if is_direct and direct_url:
        downloaded_file = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")
        dl_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        if item.get("referer"):
            dl_headers["Referer"] = item["referer"]

        try:
            import aiohttp
            import aiofiles
            async with aiohttp.ClientSession(headers=dl_headers) as session:
                async with session.get(direct_url, timeout=aiohttp.ClientTimeout(total=1200)) as resp:
                    if resp.status not in (200, 206):
                        raise Exception(f"خطای سرور دانلود (کد {resp.status})")
                    async with aiofiles.open(downloaded_file, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            await f.write(chunk)

            if quality == "audio":
                audio_file = os.path.join(DOWNLOAD_DIR, f"{file_id}.m4a")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", downloaded_file, "-vn", "-c:a", "aac", audio_file,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
                if os.path.exists(audio_file):
                    try:
                        os.remove(downloaded_file)
                    except Exception:
                        pass
                    downloaded_file = audio_file
        except Exception as e:
            await status_msg.edit_text(f"❌ <b>خطا در دانلود فایل:</b>\n<code>{str(e)[:150]}</code>")
            return
    else:
        out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
        if quality == "1080":
            format_opt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        elif quality == "720":
            format_opt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        else:  # audio
            format_opt = "bestaudio/best"

        ydl_opts = {
            "format": format_opt,
            "outtmpl": out_template,
            "merge_output_format": "mp4" if quality != "audio" else "m4a",
            "quiet": True,
            "no_warnings": True,
        }

        def download_task():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(target_url, download=True)
                return ydl.prepare_filename(res)

        try:
            downloaded_file = await loop.run_in_executor(None, download_task)
            if quality != "audio" and not downloaded_file.endswith(".mp4"):
                base = os.path.splitext(downloaded_file)[0]
                if os.path.exists(f"{base}.mp4"):
                    downloaded_file = f"{base}.mp4"
        except Exception as e:
            await status_msg.edit_text(f"❌ <b>خطا در دانلود ویدیو:</b>\n<code>{str(e)[:150]}</code>")
            return

    if not os.path.exists(downloaded_file):
        await status_msg.edit_text("❌ فایل نهایی یافت نشد.")
        return

    file_size = os.path.getsize(downloaded_file)
    caption = (
        f"🌐 <b>{item.get('title', 'Video')}</b>\n"
        f"🎯 <b>کیفیت:</b> <code>{quality} [{format_bytes(file_size)}]</code>\n"
        f"👤 <b>کانال:</b> {item.get('uploader', 'Web')}\n\n"
        f"⚡ <i>دانلود شده توسط @Rabodownloaderbot</i>"
    )

    start_time = time.time()
    await status_msg.edit_text("🚀 <b>در حال ارسال فایل به تلگرام (تا سقف ۲ گیگ)...</b>")

    try:
        if quality == "audio":
            await client.send_audio(
                chat_id=cq.message.chat.id,
                audio=downloaded_file,
                caption=caption,
                title=item.get("title"),
                performer=item.get("uploader"),
                progress=progress_tracker,
                progress_args=(status_msg, "در حال آپلود صدا به تلگرام", start_time),
            )
        else:
            await client.send_video(
                chat_id=cq.message.chat.id,
                video=downloaded_file,
                caption=caption,
                supports_streaming=True,
                progress=progress_tracker,
                progress_args=(status_msg, "در حال آپلود ویدیو به تلگرام", start_time),
            )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>خطا در ارسال به تلگرام:</b>\n<code>{str(e)[:150]}</code>")
    finally:
        if os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except Exception:
                pass

# ============================================================================
# Main Entrypoint: Start FastAPI with Lifespan (Starts Pyrogram + Web Server)
# ============================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
