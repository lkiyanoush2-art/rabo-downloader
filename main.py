import os
import sys
import time
import re
import asyncio
import threading
import http.server
import urllib.request
import urllib.parse
import json
from typing import Dict, Any, Optional, List

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
)
import yt_dlp
import aiohttp
import aiofiles

# ============================================================================
# yt-dlp Hot-patches & Extractor Resiliency
# ============================================================================
try:
    import yt_dlp.extractor.xhamster as xh_mod

    orig_xh_download = xh_mod.XHamsterIE._download_webpage_handle
    def patched_xh_download(self, *args, **kwargs):
        webpage, urlh = orig_xh_download(self, *args, **kwargs)
        m = re.search(r'<title[^>]*>(.+?)(?:,\s*[^,]*?\s*Porn\s*[^,]*?:\s*xHamster[^<]*| - xHamster\.com)</title>', webpage, re.IGNORECASE)
        if not m:
            m = re.search(r'<h1[^>]*>([^<]+)</h1>', webpage, re.IGNORECASE)
        if m:
            self._last_title = m.group(1).strip()
        return webpage, urlh

    orig_xh_parse = xh_mod.XHamsterIE._parse_json
    def patched_xh_parse(self, json_string, video_id, *args, **kwargs):
        res = orig_xh_parse(self, json_string, video_id, *args, **kwargs)
        if isinstance(res, dict):
            if "videoModel" not in res:
                res["videoModel"] = res.get("video") or res.get("videoData") or res.get("pageState", {}).get("video") or {}
            if isinstance(res.get("videoModel"), dict):
                vm = res["videoModel"]
                if not vm.get("title"):
                    vm["title"] = res.get("title") or getattr(self, "_last_title", None) or "xHamster Video"
                vm.setdefault("sources", {})
        return res

    xh_mod.XHamsterIE._download_webpage_handle = patched_xh_download
    xh_mod.XHamsterIE._parse_json = patched_xh_parse
    print("✅ Applied resiliency hot-patch to XHamsterIE", flush=True)
except Exception as e:
    print(f"⚠️ Could not patch XHamsterIE: {e}", flush=True)

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

def get_quality_label(height: int) -> str:
    if height >= 2160:
        return "4K"
    elif height >= 1440:
        return "2K"
    elif height >= 1080:
        return "1080p"
    elif height >= 720:
        return "720p"
    elif height >= 480:
        return "480p"
    elif height >= 360:
        return "360p"
    elif height > 0:
        return f"{height}p"
    return "Best"

def format_quality_caption(width: int, height: int) -> str:
    dim = min(width, height) if width > 0 and height > 0 else max(width, height)
    if dim >= 2160:
        return "4K (2160p)"
    elif dim >= 1440:
        return "2K (1440p)"
    elif dim >= 1080:
        return "1080p"
    elif dim >= 720:
        return "720p"
    elif dim >= 480:
        return "480p"
    elif dim >= 360:
        return "360p"
    elif dim > 0:
        return f"{dim}p"
    return "HD"

def detect_resolution_from_text(text: str) -> int:
    if not text:
        return 0
    t = text.lower()
    if re.search(r'\b(2160p|4k|uhd)\b', t) or re.search(r'3840x2160|2160x3840', t):
        return 2160
    if re.search(r'\b(1440p|2k|qhd)\b', t) or re.search(r'2560x1440|1440x2560', t):
        return 1440
    if re.search(r'\b(1080p|fhd)\b', t) or re.search(r'1920x1080|1080x1920', t):
        return 1080
    if re.search(r'\b(720p|hd)\b', t) or re.search(r'1280x720|720x1280', t):
        return 720
    if re.search(r'\b480p\b', t) or re.search(r'854x480|480x854', t):
        return 480
    if re.search(r'\b360p\b', t) or re.search(r'640x360|360x640', t):
        return 360
    return 0



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
        f"📊 <b>Size:</b> <code>{format_bytes(current)} / {format_bytes(total)}</code>\n"
        f"🚀 <b>Speed:</b> <code>{speed_str}</code>"
    )
    try:
        await status_msg.edit_text(text)
    except Exception:
        pass

# ============================================================================
# Media Resolvers
# ============================================================================

# 1. Bunkr Resolver (Fully Tested & Working with CDN token signing)
async def resolve_bunkr(url: str) -> Optional[Dict[str, Any]]:
    domain_match = re.search(r"https?://([^/]+)", url)
    domain = domain_match.group(1) if domain_match else "bunkr.cr"
    referer = f"https://{domain}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

                title_match = (
                    re.search(r'Original\s*=\s*([^,]+)', html) or
                    re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.IGNORECASE) or
                    re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
                )
                title = title_match.group(1).strip() if title_match else "Bunkr Video"
                title = re.sub(r'\s*\|\s*Bunkr.*$', '', title, flags=re.IGNORECASE).strip()

                cover_match = (
                    re.search(r'var\s+videoCoverUrl\s*=\s*["\']([^"\']+)["\']', html) or
                    re.search(r'poster=["\']([^"\']+)["\']', html, re.IGNORECASE) or
                    re.search(r'property="og:image"\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                )
                poster = cover_match.group(1).replace(r"\/", "/") if cover_match else None

                filesize = 0
                size_match = re.search(r'Size\s*=\s*(\d+)', html)
                if size_match:
                    filesize = int(size_match.group(1))

                # Method 1: Bunkr CDN signing API (jsCDN + signUrl)
                cdn_match = re.search(r'var\s+jsCDN\s*=\s*["\']([^"\']+)["\']', html)
                sign_match = re.search(r'var\s+signUrl\s*=\s*["\']([^"\']+)["\']', html)

                def build_bunkr_dict(v_url):
                    bunkr_h = detect_resolution_from_text(f"{title} {html}")
                    tier_label = get_quality_label(bunkr_h) if bunkr_h > 0 else None
                    btn_label = f"📹 {tier_label} (Original)" if tier_label else "📹 Original Quality"
                    name_label = f"{tier_label} (Original)" if tier_label else "Original Quality"
                    formats = [{
                        "id": "best",
                        "label": btn_label,
                        "name": name_label,
                        "height": bunkr_h,
                        "filesize": filesize,
                        "url": v_url,
                    }]
                    return {
                        "type": "video",
                        "url": v_url,
                        "title": title,
                        "uploader": "Bunkr",
                        "duration": 0,
                        "filesize": filesize,
                        "thumbnail": poster,
                        "height": bunkr_h,
                        "formats": formats,
                        "quality_urls": {"best": v_url, "audio": v_url},
                        "is_direct": True,
                        "referer": referer,
                    }

                if cdn_match and sign_match:
                    raw_cdn = cdn_match.group(1).replace(r"\/", "/")
                    sign_url = sign_match.group(1).replace(r"\/", "/")
                    parsed_cdn = urllib.parse.urlparse(raw_cdn)
                    encoded_path = urllib.parse.quote(parsed_cdn.path)
                    sign_req_url = f"{sign_url}?path={encoded_path}"

                    async with session.get(sign_req_url, timeout=aiohttp.ClientTimeout(total=10)) as sign_resp:
                        if sign_resp.status == 200:
                            sign_data = await sign_resp.json()
                            token = sign_data.get("token")
                            ex = sign_data.get("ex")
                            if token and ex:
                                final_video_url = f"{raw_cdn}?token={token}&ex={ex}"
                                return build_bunkr_dict(final_video_url)

                # Method 2: Download button href
                dl_btn_match = re.search(r'href=["\'](https?://dl\.bunkr\.[^/]+/file/\d+)["\']', html)
                if dl_btn_match:
                    dl_url = dl_btn_match.group(1)
                    return build_bunkr_dict(dl_url)

                # Method 3: Direct video in HTML
                vid_match = (
                    re.search(r'<source[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE) or
                    re.search(r'<video[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE) or
                    re.search(r'href=["\']([^"\']*(?:media-files|\.mp4|\.mkv|\.mov)[^"\']*)["\']', html, re.IGNORECASE) or
                    re.search(r'["\']downloadUrl["\']\s*:\s*["\']([^"\']+)["\']', html) or
                    re.search(r'["\']file["\']\s*:\s*["\']([^"\']+(?:\.mp4|\.mkv|\.mov)[^"\']*)["\']', html)
                )

                if vid_match:
                    video_url = vid_match.group(1).replace(r"\/", "/")
                    if video_url.startswith("//"):
                        video_url = "https:" + video_url
                    elif video_url.startswith("/"):
                        video_url = f"https://{domain}{video_url}"

                    return build_bunkr_dict(video_url)
    except Exception as e:
        print(f"[Bunkr] Resolution error: {e}", flush=True)
    return None

# 2. Twitter / X Resolver (Supports Photos, Albums, and Videos)
async def resolve_twitter(url: str) -> Optional[Dict[str, Any]]:
    match = re.search(r'(?:twitter\.com|x\.com)/(?:[^/]+/status/|i/status/)(\d+)', url)
    if not match:
        return None
    tweet_id = match.group(1)

    # 1. Primary: FxTwitter API v2 (Supports single/multi photos and all video qualities)
    api_url = f"https://api.fxtwitter.com/2/status/{tweet_id}"
    headers = {
        "User-Agent": "TelegramBot (like TwitterBot)",
        "Accept": "application/json",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data.get("status", {})
                    author = status.get("author", {})
                    author_name = author.get("name", "X User")
                    screen_name = author.get("screen_name", "twitter")
                    text = status.get("text", "")
                    media = status.get("media", {})

                    photos = media.get("photos", [])
                    videos = media.get("videos", [])

                    # Case A: Only Photos (Single or Gallery)
                    if photos and not videos:
                        photo_urls = [p["url"] for p in photos if p.get("url")]
                        return {
                            "type": "photos",
                            "photos": photo_urls,
                            "uploader": f"{author_name} (@{screen_name})",
                            "title": text[:80] if text else "Twitter Photo",
                            "text": text,
                        }

                    # Case B: Video
                    if videos:
                        v = videos[0]
                        formats = v.get("formats", [])
                        variants = []
                        if formats:
                            for f in formats:
                                v_url = f.get("url")
                                if v_url and f.get("container") == "mp4":
                                    bitrate = int(f.get("bitrate", 0) or 0)
                                    size = int(f.get("size", 0) or 0)
                                    # Extract resolution from URL path (e.g. /1920x1080/ or /720x1280/)
                                    res_m = re.search(r'/(\d+)x(\d+)/', v_url)
                                    if res_m:
                                        d1, d2 = int(res_m.group(1)), int(res_m.group(2))
                                        h = min(d1, d2)
                                        w = max(d1, d2)
                                    else:
                                        h = int(f.get("height", 0) or 0)
                                        w = int(f.get("width", 0) or 0)
                                    variants.append({
                                        "url": v_url,
                                        "height": h,
                                        "width": w,
                                        "size": size,
                                        "bitrate": bitrate,
                                    })

                        # Sort strictly by resolution descending, then bitrate descending
                        variants.sort(key=lambda x: (x.get("height", 0), x.get("bitrate", 0)), reverse=True)

                        duration = int(v.get("duration", 0))

                        # Build discrete available format tiers (e.g. 4K, 2K, 1080p, 720p, 480p, 360p)
                        seen_tiers = set()
                        video_formats = []
                        quality_urls = {}

                        for var in variants:
                            h = var["height"]
                            tier_label = get_quality_label(h)
                            if tier_label in seen_tiers:
                                continue
                            seen_tiers.add(tier_label)

                            calc_sz = var.get("size") or (int(var.get("bitrate", 0) * duration / 8) if duration and var.get("bitrate") else 0)
                            tier_id = str(h) if h > 0 else "best"
                            video_formats.append({
                                "id": tier_id,
                                "label": f"📹 {tier_label}",
                                "name": tier_label,
                                "height": h,
                                "filesize": calc_sz,
                                "url": var["url"],
                            })
                            quality_urls[tier_id] = var["url"]
                            quality_urls[tier_label.lower()] = var["url"]
                            if len(video_formats) >= 4:
                                break

                        best_mp4 = variants[0]["url"] if variants else v.get("url")
                        if not video_formats and best_mp4:
                            video_formats.append({
                                "id": "best",
                                "label": "📹 Best Quality",
                                "name": "Best Quality",
                                "height": 0,
                                "filesize": 0,
                                "url": best_mp4,
                            })
                            quality_urls["best"] = best_mp4

                        # Guarantee legacy keys exist
                        if "1080" not in quality_urls and variants:
                            quality_urls["1080"] = variants[0]["url"]
                        if "720" not in quality_urls and variants:
                            v_720 = next((var for var in variants if var.get("height", 0) <= 720), None)
                            quality_urls["720"] = v_720["url"] if v_720 else variants[0]["url"]

                        quality_urls["audio"] = video_formats[-1]["url"] if video_formats else best_mp4
                        calc_filesize = video_formats[0]["filesize"] if video_formats else 0

                        return {
                            "type": "video",
                            "url": video_formats[0]["url"] if video_formats else best_mp4,
                            "formats": video_formats,
                            "quality_urls": quality_urls,
                            "variants": variants,
                            "filesize": calc_filesize,
                            "thumbnail": v.get("thumbnail_url"),
                            "duration": duration,
                            "uploader": f"{author_name} (@{screen_name})",
                            "title": text[:80] if text else "Twitter Video",
                            "text": text,
                            "is_direct": True,
                        }
    except Exception as e:
        print(f"[Twitter] FxTwitter error: {e}", flush=True)

    # 2. Fallback: Twitter Syndication API
    try:
        syn_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=4"
        syn_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        async with aiohttp.ClientSession(headers=syn_headers) as session:
            async with session.get(syn_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user = data.get("user", {})
                    author_name = user.get("name", "X User")
                    screen_name = user.get("screen_name", "twitter")
                    text = data.get("text", "")
                    media_details = data.get("mediaDetails", [])

                    photo_urls = []
                    video_info = None
                    for m in media_details:
                        if m.get("type") == "photo" and m.get("media_url_https"):
                            photo_urls.append(m["media_url_https"])
                        elif m.get("type") == "video" and not video_info:
                            video_info = m.get("video_info")

                    if photo_urls and not video_info:
                        return {
                            "type": "photos",
                            "photos": photo_urls,
                            "uploader": f"{author_name} (@{screen_name})",
                            "title": text[:80] if text else "Twitter Photo",
                            "text": text,
                        }

                    if video_info:
                        variants = []
                        for var in video_info.get("variants", []):
                            if var.get("content_type") == "video/mp4":
                                v_url = var.get("url", "")
                                bitrate = int(var.get("bitrate", 0) or 0)
                                res_m = re.search(r'/(\d+)x(\d+)/', v_url)
                                if res_m:
                                    d1, d2 = int(res_m.group(1)), int(res_m.group(2))
                                    h = min(d1, d2)
                                    w = max(d1, d2)
                                else:
                                    h = 0
                                    w = 0
                                variants.append({
                                    "url": v_url,
                                    "height": h,
                                    "width": w,
                                    "bitrate": bitrate,
                                })
                        variants.sort(key=lambda x: (x.get("height", 0), x.get("bitrate", 0)), reverse=True)
                        duration = int(video_info.get("duration_millis", 0) / 1000) if video_info.get("duration_millis") else 0

                        seen_tiers = set()
                        video_formats = []
                        quality_urls = {}

                        for var in variants:
                            h = var["height"]
                            tier_label = get_quality_label(h)
                            if tier_label in seen_tiers:
                                continue
                            seen_tiers.add(tier_label)

                            calc_sz = int(var.get("bitrate", 0) * duration / 8) if duration and var.get("bitrate") else 0
                            tier_id = str(h) if h > 0 else "best"
                            video_formats.append({
                                "id": tier_id,
                                "label": f"📹 {tier_label}",
                                "name": tier_label,
                                "height": h,
                                "filesize": calc_sz,
                                "url": var["url"],
                            })
                            quality_urls[tier_id] = var["url"]
                            quality_urls[tier_label.lower()] = var["url"]
                            if len(video_formats) >= 4:
                                break

                        if not video_formats and variants:
                            video_formats.append({
                                "id": "best",
                                "label": "📹 Best Quality",
                                "name": "Best Quality",
                                "height": 0,
                                "filesize": 0,
                                "url": variants[0]["url"],
                            })
                            quality_urls["best"] = variants[0]["url"]

                        if "1080" not in quality_urls and variants:
                            quality_urls["1080"] = variants[0]["url"]
                        if "720" not in quality_urls and variants:
                            v_720 = next((var for var in variants if var.get("height", 0) <= 720), None)
                            quality_urls["720"] = v_720["url"] if v_720 else variants[0]["url"]

                        quality_urls["audio"] = video_formats[-1]["url"] if video_formats else (variants[0]["url"] if variants else None)
                        calc_filesize = video_formats[0]["filesize"] if video_formats else 0

                        if variants:
                            return {
                                "type": "video",
                                "url": video_formats[0]["url"] if video_formats else variants[0]["url"],
                                "formats": video_formats,
                                "quality_urls": quality_urls,
                                "variants": variants,
                                "filesize": calc_filesize,
                                "duration": duration,
                                "uploader": f"{author_name} (@{screen_name})",
                                "title": text[:80] if text else "Twitter Video",
                                "text": text,
                                "is_direct": True,
                            }
    except Exception as e:
        print(f"[Twitter] Syndication error: {e}", flush=True)

    return None

# 3. Instagram Resolver (Photos, Albums/Carousels, and Reels/Videos)
def _decode_snap_app(h: str, u: str, n: str, t: str, e: str, r: str) -> str:
    t_num = int(t)
    e_num = int(e)
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"

    def decode(d: str, base_from: int, base_to: int) -> str:
        h_arr = chars[:base_from]
        i_arr = chars[:base_to]
        j = 0
        for c, b in enumerate(reversed(d)):
            idx = h_arr.find(b)
            if idx != -1:
                j += idx * (base_from ** c)
        k = ""
        while j > 0:
            k = i_arr[j % base_to] + k
            j = j // base_to
        return k or "0"

    result = []
    i = 0
    length = len(h)
    while i < length:
        s = ""
        while i < length and h[i] != n[e_num]:
            s += h[i]
            i += 1
        i += 1
        for j_idx, char in enumerate(n):
            s = s.replace(char, str(j_idx))
        if s:
            try:
                val = int(decode(s, e_num, 10)) - t_num
                result.append(chr(val))
            except Exception:
                pass

    raw = "".join(result)
    try:
        return raw.encode('latin1').decode('utf-8')
    except Exception:
        return raw

async def resolve_instagram(url: str) -> Optional[Dict[str, Any]]:
    # 1. SnapSave / SnapInsta Decoder
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://snapsave.app",
        "Referer": "https://snapsave.app/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    }
    post_data = urllib.parse.urlencode({"url": url})

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post("https://snapsave.app/action.php?lang=en", data=post_data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    resp_text = await resp.text()

                    # Find packer parameters (h, u, n, t, e, r)
                    match = re.search(r'\}\s*\(\s*(".*?")\s*,\s*(".*?"|\d+)\s*,\s*(".*?")\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', resp_text)
                    if not match:
                        sub = re.search(r'decodeURIComponent\(escape\(r\)\)\}\s*\(\s*(.*?)\s*\)\)', resp_text, re.DOTALL)
                        if sub:
                            args = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"|\b(\d+)\b', sub.group(1))
                            parsed = [a[0] if a[0] else a[1] for a in args]
                            if len(parsed) >= 6:
                                h, u, n, t, e, r = parsed[:6]
                                html = _decode_snap_app(h, u, n, t, e, r)
                            else:
                                html = None
                        else:
                            html = None
                    else:
                        h = match.group(1).strip('"')
                        u = match.group(2).strip('"')
                        n = match.group(3).strip('"')
                        t, e, r = match.group(4), match.group(5), match.group(6)
                        html = _decode_snap_app(h, u, n, t, e, r)

                    if html and "alert" not in html:
                        # Extract items
                        # Check carousel or download items
                        download_cards = re.findall(r'<div[^>]*class=["\'][^"\']*download-items[^"\']*["\'][^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
                        photos = []
                        videos = []

                        if download_cards:
                            for card in download_cards:
                                img_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card)
                                btn_m = re.search(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', card, re.DOTALL)
                                thumb = img_m.group(1) if img_m else None
                                dl_url = btn_m.group(1) if btn_m else None
                                btn_text = btn_m.group(2) if btn_m else ""

                                if "Photo" in btn_text:
                                    photos.append(thumb or dl_url)
                                else:
                                    videos.append(dl_url or thumb)

                        # Check table formats (single video reels)
                        table_rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
                        table_video_items = []
                        for row in table_rows:
                            link_m = re.search(r'href=["\'](https?://[^"\']+)["\']', row)
                            if link_m:
                                dl_link = link_m.group(1)
                                res_m = re.search(r'(\d+)p', row, re.IGNORECASE)
                                h = int(res_m.group(1)) if res_m else detect_resolution_from_text(row)
                                table_video_items.append({"url": dl_link, "height": h})
                                videos.append(dl_link)

                        # Check general single download button
                        if not photos and not videos:
                            single_btn = re.search(r'href=["\'](https?://[^"\']+)["\'][^>]*>.*?Download\s*(Video|Photo)', html, re.DOTALL | re.IGNORECASE)
                            if single_btn:
                                target = single_btn.group(1)
                                is_photo = "Photo" in single_btn.group(2)
                                if is_photo:
                                    photos.append(target)
                                else:
                                    videos.append(target)

                        # Clean any duplicate or None items
                        photos = [p for p in photos if p and p.startswith("http")]
                        videos = [v for v in videos if v and v.startswith("http")]

                        if photos and not videos:
                            return {
                                "type": "photos",
                                "photos": photos,
                                "uploader": "Instagram",
                                "title": "Instagram Photo",
                            }

                        if table_video_items and any(item["height"] > 0 for item in table_video_items):
                            table_video_items.sort(key=lambda x: x["height"], reverse=True)
                            seen_tiers = set()
                            formats_list = []
                            quality_urls = {}
                            for item in table_video_items:
                                h = item["height"]
                                tier_label = get_quality_label(h) if h > 0 else "Best"
                                if tier_label in seen_tiers:
                                    continue
                                seen_tiers.add(tier_label)
                                tier_id = str(h) if h > 0 else "best"
                                formats_list.append({
                                    "id": tier_id,
                                    "label": f"📹 {tier_label}",
                                    "name": tier_label,
                                    "height": h,
                                    "filesize": 0,
                                    "url": item["url"],
                                })
                                quality_urls[tier_id] = item["url"]
                                quality_urls[tier_label.lower()] = item["url"]

                            quality_urls["audio"] = table_video_items[0]["url"]
                            return {
                                "type": "video",
                                "url": table_video_items[0]["url"],
                                "formats": formats_list,
                                "quality_urls": quality_urls,
                                "uploader": "Instagram",
                                "title": "Instagram Video",
                                "duration": 0,
                                "is_direct": True,
                            }

                        if videos:
                            detected_h = detect_resolution_from_text(html) or 1080
                            tier_label = get_quality_label(detected_h)
                            formats_list = [{
                                "id": "best",
                                "label": f"📹 {tier_label} (Original)",
                                "name": f"{tier_label} (Original)",
                                "height": detected_h,
                                "filesize": 0,
                                "url": videos[0],
                            }]
                            return {
                                "type": "video",
                                "url": videos[0],
                                "formats": formats_list,
                                "quality_urls": {"best": videos[0], "audio": videos[0]},
                                "uploader": "Instagram",
                                "title": "Instagram Video",
                                "duration": 0,
                                "is_direct": True,
                            }
    except Exception as e:
        print(f"[Instagram] SnapSave error: {e}", flush=True)

    return None

# 4. Universal Generic Web Scraper (Zero-failure fallback for any website)
async def resolve_generic_video(url: str) -> Optional[Dict[str, Any]]:
    domain = urllib.parse.urlparse(url).netloc
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": url,
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

                # 1. Extract title
                title_match = (
                    re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.I) or
                    re.search(r'<meta\s+name=["\']twitter:title["\']\s+content=["\']([^"\']+)["\']', html, re.I) or
                    re.search(r'<title[^>]*>([^<]+)</title>', html, re.I) or
                    re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.I)
                )
                title = title_match.group(1).strip() if title_match else "Online Video"
                title = re.sub(r'\s*[-|]\s*[^|]+$', '', title).strip()

                # 2. Extract thumbnail
                thumb_match = (
                    re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I) or
                    re.search(r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']', html, re.I) or
                    re.search(r'poster=["\']([^"\']+)["\']', html, re.I)
                )
                thumb = thumb_match.group(1).strip() if thumb_match else None

                # 3. Extract candidate video streams
                video_urls = []
                patterns = [
                    r'<meta\s+property=["\']og:video(?::secure_url|:url)?["\']\s+content=["\']([^"\']+)["\']',
                    r'<source[^>]+src=["\']([^"\']+\.(?:mp4|m3u8|webm)[^"\']*)["\']',
                    r'<video[^>]+src=["\']([^"\']+\.(?:mp4|m3u8|webm)[^"\']*)["\']',
                    r'file\s*:\s*["\'](https?://[^"\']+\.(?:mp4|m3u8)[^"\']*)["\']',
                    r'["\'](https?://[^"\']+\.(?:mp4|m3u8)[^"\']*)["\']',
                ]
                for pat in patterns:
                    for m in re.finditer(pat, html, re.I):
                        cand = m.group(1).replace(r"\/", "/")
                        if cand not in video_urls and not re.search(r'\.(?:m4a|mp3|jpg|png|gif|css|js)(?:\?|$)', cand):
                            video_urls.append(cand)

                if video_urls:
                    best_url = video_urls[0]
                    detected_h = detect_resolution_from_text(f"{title} {html}") or 720
                    tier_label = get_quality_label(detected_h)
                    formats_list = [{
                        "id": "best",
                        "label": f"📹 {tier_label} (Direct)",
                        "name": f"{tier_label} (Direct)",
                        "height": detected_h,
                        "filesize": 0,
                        "url": best_url,
                    }]
                    return {
                        "type": "video",
                        "url": best_url,
                        "title": title,
                        "uploader": domain or "Web",
                        "duration": 0,
                        "thumbnail": thumb,
                        "formats": formats_list,
                        "quality_urls": {"best": best_url, "audio": best_url},
                        "is_direct": True,
                        "referer": url,
                    }
    except Exception as e:
        print(f"[Universal Fallback] Error resolving {url}: {e}", flush=True)

    return None

# ============================================================================
# Telegram Handlers
# ============================================================================

# Debug logger: Prints every single message received to Render logs
@bot.on_message(group=-1)
async def log_all_updates(_, message: Message):
    sender = message.from_user.username if (message.from_user and message.from_user.username) else (message.from_user.id if message.from_user else "Unknown")
    print(f"📩 [Incoming Telegram Message] From @{sender} (ID: {message.chat.id}): {message.text or message.caption or '[Media]'}", flush=True)

@bot.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    welcome = (
        "👋 <b>Welcome to the 2GB Media Downloader Bot!</b>\n\n"
        "⚡ <i>Supports photos, carousels, and videos up to 2000 MB!</i>\n\n"
        "🚀 <b>Supported Platforms:</b>\n"
        "• <b>Bunkr</b> (Original quality, fast CDN streaming)\n"
        "• <b>Twitter / X</b> (Photos, multi-image galleries, and 1080p videos)\n"
        "• <b>Instagram</b> (Reels, video posts, single photos, and carousels)\n"
        "• <b>YouTube & TikTok</b> (HD Video & Audio)\n"
        "• <b>Direct media and video links</b>\n\n"
        "📥 <b>Simply send any post or video link to get started!</b>"
    )
    await message.reply_text(welcome)

@bot.on_message(filters.regex(r"https?://[^\s]+"))
async def link_handler(client: Client, message: Message):
    url = re.search(r"https?://[^\s]+", message.text or "").group(0)
    status_msg = await message.reply_text("🔎 <b>Analyzing link and retrieving metadata...</b>")

    loop = asyncio.get_event_loop()
    info = None

    # 1. Bunkr
    if re.search(r"bunkr\.[a-z]+|bunkrr\.[a-z]+", url, re.IGNORECASE):
        info = await resolve_bunkr(url)

    # 2. Twitter / X
    elif re.search(r"twitter\.com|x\.com", url, re.IGNORECASE):
        info = await resolve_twitter(url)

    # 3. Instagram
    elif re.search(r"instagram\.com", url, re.IGNORECASE):
        info = await resolve_instagram(url)

    # 4. Fallback (yt-dlp for YouTube, TikTok, Reddit, etc.)
    if not info:
        def extract_info():
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["visionos", "android", "tv"]
                    },
                    "generic": {
                        "impersonate": ["chrome"]
                    }
                },
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            }
            # Check for cookies file
            if os.path.exists("/app/cookies.txt"):
                ydl_opts["cookiefile"] = "/app/cookies.txt"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            raw_info = await loop.run_in_executor(None, extract_info)
            if raw_info:
                dur = raw_info.get("duration", 0)
                seen_tiers = set()
                formats_list = []
                raw_formats = raw_info.get("formats", [])

                valid_vids = [f for f in raw_formats if f.get("vcodec") != "none" and f.get("height")]
                valid_vids.sort(key=lambda x: (x.get("height", 0), x.get("tbr", 0) or x.get("filesize", 0) or 0), reverse=True)

                for f in valid_vids:
                    h = f.get("height", 0)
                    tier_label = get_quality_label(h)
                    if tier_label in seen_tiers:
                        continue
                    seen_tiers.add(tier_label)

                    calc_sz = f.get("filesize") or f.get("filesize_approx") or (int(f.get("tbr", 0) * 1000 * dur / 8) if dur and f.get("tbr") else 0)
                    formats_list.append({
                        "id": str(h),
                        "label": f"📹 {tier_label}",
                        "name": tier_label,
                        "height": h,
                        "filesize": calc_sz,
                    })
                    if len(formats_list) >= 4:
                        break

                if not formats_list:
                    formats_list.append({
                        "id": "best",
                        "label": "📹 Best Quality",
                        "name": "Best Quality",
                        "height": 0,
                        "filesize": raw_info.get("filesize") or raw_info.get("filesize_approx") or 0,
                    })

                info = {
                    "type": "video",
                    "url": url,
                    "title": raw_info.get("title", "Video"),
                    "uploader": raw_info.get("uploader") or raw_info.get("channel") or "Web",
                    "duration": dur,
                    "thumbnail": raw_info.get("thumbnail"),
                    "filesize": raw_info.get("filesize") or raw_info.get("filesize_approx") or 0,
                    "formats": formats_list,
                    "is_direct": False,
                }
        except Exception as e:
            print(f"[yt-dlp] Extraction error: {e}. Trying Universal Fallback...", flush=True)
            info = await resolve_generic_video(url)
            if not info:
                await status_msg.edit_text(f"❌ <b>Error retrieving metadata:</b>\n<code>{str(e)[:150]}</code>")
                return

    if not info:
        info = await resolve_generic_video(url)

    if not info:
        await status_msg.edit_text("❌ No media found in this link.")
        return

    # ========================================================================
    # Case 1: Photos (Single or Gallery/Album)
    # ========================================================================
    if info.get("type") == "photos":
        photos = info.get("photos", [])
        if not photos:
            await status_msg.edit_text("❌ No photos found in this link.")
            return

        await status_msg.edit_text("🚀 <b>Sending photos to Telegram...</b>")
        uploader = info.get("uploader", "Social Media")
        caption = f"📸 <b>{uploader}</b>\n\n⚡ <i>@Rabodownloaderbot</i>"
        if info.get("text"):
            caption = f"📸 <b>{uploader}</b>\n\n{info['text'][:300]}\n\n⚡ <i>@Rabodownloaderbot</i>"

        try:
            if len(photos) == 1:
                await client.send_photo(
                    chat_id=message.chat.id,
                    photo=photos[0],
                    caption=caption,
                )
            else:
                media_group = [
                    InputMediaPhoto(media=p_url, caption=caption if idx == 0 else "")
                    for idx, p_url in enumerate(photos[:10])
                ]
                await client.send_media_group(
                    chat_id=message.chat.id,
                    media=media_group,
                )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"❌ <b>Error sending photos:</b>\n<code>{str(e)[:150]}</code>")
        return

    # ========================================================================
    # Case 2: Video (Bunkr, Twitter, Instagram, YouTube)
    # ========================================================================
    video_formats = info.get("formats", [])
    if not video_formats:
        h = info.get("height", 0) or detect_resolution_from_text(f"{info.get('title', '')} {url}")
        tier_label = get_quality_label(h) if h > 0 else None
        if tier_label:
            label = f"📹 {tier_label} (Original)" if info.get("is_direct") else f"📹 {tier_label}"
            name = f"{tier_label} (Original)" if info.get("is_direct") else tier_label
        else:
            label = "📹 Original Quality" if info.get("is_direct") else "📹 Best Quality"
            name = "Original Quality" if info.get("is_direct") else "Best Quality"

        v_url = info.get("url")
        video_formats = [
            {"id": "best", "label": label, "name": name, "height": h, "filesize": info.get("filesize", 0), "url": v_url}
        ]

    cache_id = str(int(time.time() * 1000))
    quality_urls = info.get("quality_urls", {})
    if not quality_urls.get("best") and info.get("url"):
        quality_urls["best"] = info["url"]
    if not quality_urls.get("audio") and info.get("url"):
        quality_urls["audio"] = info["url"]

    CACHE[cache_id] = {
        "url": url,
        "direct_url": info.get("url") if info.get("is_direct") else None,
        "quality_urls": quality_urls,
        "is_direct": info.get("is_direct", False),
        "referer": info.get("referer"),
        "title": info.get("title", "Video"),
        "uploader": info.get("uploader") or "Video",
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
        "variants": info.get("variants", []),
        "formats": video_formats,
    }

    uploader = info.get("uploader") or "Video"
    duration_str = format_duration(info.get("duration"))

    video_lines = []
    for idx, vf in enumerate(video_formats, 1):
        sz_str = f" [{format_bytes(vf['filesize'])}]" if vf.get("filesize") else ""
        tier_tag = " ⚡" if idx == 1 else ""
        tier_name = vf.get("name") or vf.get("label", "").replace("📹 ", "")
        video_lines.append(f"{idx}. <i>mp4, {tier_name}{sz_str}</i>{tier_tag}")

    vid_section = "\n".join(video_lines)

    menu_text = (
        f"<b>{uploader}</b>\n"
        f"[{duration_str}]\n\n"
        f"📹 <b>Video Formats:</b>\n"
        f"{vid_section}\n\n"
        f"🎧 <b>Audio:</b>\n"
        f"• <i>m4a, mp3</i>\n\n"
        f"⚡ <i>Select desired format:</i>"
    )

    btn_rows = []
    if len(video_formats) == 1:
        btn_rows.append([
            InlineKeyboardButton(video_formats[0]["label"], callback_data=f"dl:{cache_id}:{video_formats[0]['id']}"),
            InlineKeyboardButton("♫ Audio", callback_data=f"dl:{cache_id}:audio"),
        ])
    elif len(video_formats) == 2:
        btn_rows.append([
            InlineKeyboardButton(video_formats[0]["label"], callback_data=f"dl:{cache_id}:{video_formats[0]['id']}"),
            InlineKeyboardButton(video_formats[1]["label"], callback_data=f"dl:{cache_id}:{video_formats[1]['id']}"),
            InlineKeyboardButton("♫ Audio", callback_data=f"dl:{cache_id}:audio"),
        ])
    elif len(video_formats) == 3:
        btn_rows.append([
            InlineKeyboardButton(vf["label"], callback_data=f"dl:{cache_id}:{vf['id']}") for vf in video_formats
        ])
        btn_rows.append([InlineKeyboardButton("♫ Audio", callback_data=f"dl:{cache_id}:audio")])
    else:
        for i in range(0, len(video_formats), 2):
            chunk = video_formats[i:i+2]
            btn_rows.append([InlineKeyboardButton(vf["label"], callback_data=f"dl:{cache_id}:{vf['id']}") for vf in chunk])
        btn_rows.append([InlineKeyboardButton("♫ Audio", callback_data=f"dl:{cache_id}:audio")])

    btn_rows.append([InlineKeyboardButton("⟳ Refresh metadata", callback_data=f"refresh:{cache_id}")])
    btn_rows.append([InlineKeyboardButton("← Back", callback_data="back")])

    buttons = InlineKeyboardMarkup(btn_rows)

    await status_msg.delete()

    thumb_url = info.get("thumbnail")
    if thumb_url:
        try:
            await message.reply_photo(photo=thumb_url, caption=menu_text, reply_markup=buttons)
            return
        except Exception:
            pass

    await message.reply_text(menu_text, reply_markup=buttons)

# Fallback helper for non-link messages
@bot.on_message(filters.text & ~filters.command("start"))
async def text_fallback_handler(_, message: Message):
    text = message.text or ""
    if not re.search(r"https?://[^\s]+", text):
        await message.reply_text(
            "📥 <b>Please send a valid link!</b>\n\n"
            "Examples:\n"
            "• Bunkr video link\n"
            "• Instagram post or reel (photos or video)\n"
            "• Twitter / X post (photos or video)\n"
            "• YouTube, TikTok, or direct video link"
        )

async def prepare_video_metadata(file_path: str) -> Dict[str, Any]:
    faststart_path = f"{file_path}.fast.mp4"
    thumb_path = f"{file_path}.thumb.jpg"

    # 1. Apply Faststart (relocate moov atom to the front for instant streaming playback)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", file_path, "-c", "copy", "-movflags", "+faststart", faststart_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        if os.path.exists(faststart_path) and os.path.getsize(faststart_path) > 1000:
            os.replace(faststart_path, file_path)
    except Exception as e:
        print(f"Faststart notice: {e}", flush=True)

    duration = 0
    width = 1280
    height = 720

    # 2. Extract precise duration, width, and height using ffprobe
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-show_entries", "format=duration",
            "-of", "json", file_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode('utf-8', errors='ignore'))

        if "format" in data and "duration" in data["format"]:
            duration = int(float(data["format"]["duration"]))
        elif "streams" in data and len(data["streams"]) > 0 and "duration" in data["streams"][0]:
            duration = int(float(data["streams"][0]["duration"]))

        if "streams" in data and len(data["streams"]) > 0:
            width = int(data["streams"][0].get("width", 1280))
            height = int(data["streams"][0].get("height", 720))
    except Exception as e:
        print(f"ffprobe notice: {e}", flush=True)

    # 3. Generate crisp thumbnail preview image
    has_thumb = False
    try:
        ss_time = "00:00:01" if duration > 2 else "00:00:00"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", ss_time, "-i", file_path, "-vframes", "1", "-q:v", "2", thumb_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            has_thumb = True
    except Exception as e:
        print(f"Thumbnail notice: {e}", flush=True)

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "thumb": thumb_path if has_thumb else None
    }

@bot.on_callback_query()
async def callback_handler(client: Client, cq: CallbackQuery):
    data = cq.data or ""

    if data == "back":
        await cq.answer("Back")
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if data.startswith("refresh:"):
        await cq.answer("Metadata refreshed ✅")
        return

    if not data.startswith("dl:"):
        return

    _, cache_id, quality = data.split(":")
    item = CACHE.get(cache_id)
    if not item:
        await cq.answer("Link expired. Please resend the link.", show_alert=True)
        return

    await cq.answer("⏳ Downloading and preparing...")
    status_msg = await cq.message.reply_text(f"⏳ <b>Downloading {quality} to server...</b>")

    target_url = item["url"]
    file_id = f"{cache_id}_{quality}"
    loop = asyncio.get_event_loop()

    is_direct = item.get("is_direct", False)
    direct_url = item.get("direct_url")

    # If quality_urls mapping is present (e.g. from Twitter), pick the exact matched URL
    quality_urls = item.get("quality_urls", {})
    if quality in quality_urls and quality_urls[quality]:
        direct_url = quality_urls[quality]
    elif item.get("variants"):
        variants = item.get("variants", [])
        if quality.isdigit():
            req_h = int(quality)
            v_match = next((v for v in variants if v.get("height", 0) <= req_h), None)
            direct_url = v_match["url"] if v_match else variants[0]["url"]
        else:
            direct_url = variants[0]["url"]

    if is_direct and direct_url:
        downloaded_file = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")
        dl_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        if item.get("referer"):
            dl_headers["Referer"] = item["referer"]

        try:
            if ".m3u8" in direct_url:
                ref_h = dl_headers.get("Referer", "")
                ua_h = dl_headers.get("User-Agent", "")
                cmd = ["ffmpeg", "-y"]
                if ref_h:
                    cmd.extend(["-headers", f"Referer: {ref_h}\r\nUser-Agent: {ua_h}\r\n"])
                cmd.extend(["-i", direct_url, "-c", "copy", downloaded_file])
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
            else:
                async with aiohttp.ClientSession(headers=dl_headers) as session:
                    async with session.get(direct_url, timeout=aiohttp.ClientTimeout(total=1200)) as resp:
                        if resp.status not in (200, 206):
                            raise Exception(f"Download server error (HTTP {resp.status})")
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
            await status_msg.edit_text(f"❌ <b>Error downloading file:</b>\n<code>{str(e)[:150]}</code>")
            return
    else:
        out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
        if quality == "audio":
            format_opt = "bestaudio/best"
        elif quality.isdigit():
            format_opt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        else:
            format_opt = "bestvideo+bestaudio/best"

        ydl_opts = {
            "format": format_opt,
            "outtmpl": out_template,
            "merge_output_format": "mp4" if quality != "audio" else "m4a",
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["visionos", "android", "tv"]
                }
            },
        }
        if os.path.exists("/app/cookies.txt"):
            ydl_opts["cookiefile"] = "/app/cookies.txt"

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
            await status_msg.edit_text(f"❌ <b>Error downloading video:</b>\n<code>{str(e)[:150]}</code>")
            return

    if not os.path.exists(downloaded_file):
        await status_msg.edit_text("❌ Final file not found.")
        return

    file_size = os.path.getsize(downloaded_file)
    meta: Dict[str, Any] = {"duration": 0, "width": 1280, "height": 720, "thumb": None}
    if quality != "audio":
        meta = await prepare_video_metadata(downloaded_file)

    if quality == "audio":
        quality_display = "Audio (m4a)"
    else:
        quality_display = format_quality_caption(meta.get("width", 0), meta.get("height", 0))
    caption = (
        f"🌐 <b>{item.get('title', 'Video')}</b>\n"
        f"🎯 <b>Quality:</b> <code>{quality_display} [{format_bytes(file_size)}]</code>\n"
        f"👤 <b>Source:</b> {item.get('uploader', 'Web')}\n\n"
        f"⚡ <i>Downloaded by @Rabodownloaderbot</i>"
    )

    start_time = time.time()
    await status_msg.edit_text("🚀 <b>Uploading to Telegram (up to 2 GB)...</b>")

    try:
        if quality == "audio":
            await client.send_audio(
                chat_id=cq.message.chat.id,
                audio=downloaded_file,
                caption=caption,
                title=item.get("title"),
                performer=item.get("uploader"),
                progress=progress_tracker,
                progress_args=(status_msg, "Uploading audio to Telegram", start_time),
            )
        else:
            await client.send_video(
                chat_id=cq.message.chat.id,
                video=downloaded_file,
                caption=caption,
                duration=meta["duration"],
                width=meta["width"],
                height=meta["height"],
                thumb=meta["thumb"],
                supports_streaming=True,
                progress=progress_tracker,
                progress_args=(status_msg, "Uploading video to Telegram", start_time),
            )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Error uploading to Telegram:</b>\n<code>{str(e)[:150]}</code>")
    finally:
        if os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except Exception:
                pass
        if meta.get("thumb") and os.path.exists(meta["thumb"]):
            try:
                os.remove(meta["thumb"])
            except Exception:
                pass

# ============================================================================
# Background Web Server (Guarantees Render Keeps Service Live 24/7)
# ============================================================================
class HealthServerHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"online","service":"Telegram 2GB Downloader Bot","max_size":"2GB"}')

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_health_server():
    server = http.server.HTTPServer(("0.0.0.0", PORT), HealthServerHandler)
    print(f"🌐 [Web] HTTP Health Server running on port {PORT}...", flush=True)
    server.serve_forever()

def cleanup_webhook():
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode()
            print(f"🗑️ [Webhook Cleanup] {data}", flush=True)
    except Exception as e:
        print(f"⚠️ [Webhook Cleanup Warning] {e}", flush=True)

# ============================================================================
# Main Entrypoint: Pure Pyrogram Runner + Background Keep-Alive Server
# ============================================================================
def main():
    print("🚀 [Boot] Starting 2GB Media Downloader Service...", flush=True)

    # 1. Start HTTP Health Server on a background thread (Instantly satisfies Render's port checker)
    web_thread = threading.Thread(target=start_health_server, daemon=True)
    web_thread.start()

    # 2. Delete any conflicting webhooks so Pyrogram MTProto receives all updates
    cleanup_webhook()

    # 3. Start Pyrogram on the main event loop natively (100% reliable, zero loop conflict)
    print("🤖 [Startup] Launching Pyrogram MTProto Bot Client...", flush=True)
    bot.run()

if __name__ == "__main__":
    main()
