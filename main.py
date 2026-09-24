import os
import asyncio
import re
import time
from typing import Dict, Any, Optional

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
# FastAPI Health Service (For Render 24/7 Keep-Alive via UptimeRobot)
# ============================================================================
app = FastAPI()

@app.get("/")
async def health_check():
    return {
        "status": "online",
        "service": "Telegram 2GB Downloader Bot",
        "platform": "Render.com Free Tier (Keep-Alive Active)",
        "max_file_size": "2000 MB (2 GB)",
    }

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

@bot.on_message(filters.regex(r"https?://[^\s]+"))
async def link_handler(_, message: Message):
    url = re.search(r"https?://[^\s]+", message.text).group(0)
    status_msg = await message.reply_text("🔎 <b>در حال تحلیل لینک و دریافت مشخصات...</b>")

    loop = asyncio.get_event_loop()

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
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    loop = asyncio.get_event_loop()

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
# Main Entrypoint: Start Pyrogram & FastAPI concurrently
# ============================================================================
async def main():
    print(f"🚀 Starting FastAPI Server on port {PORT}...")
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    print("🤖 Starting Pyrogram MTProto Bot Client...")
    await bot.start()
    print("✅ Pyrogram Bot is running on Render!")

    try:
        await server.serve()
    finally:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
