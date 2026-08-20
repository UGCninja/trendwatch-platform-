import asyncio
import csv
import io
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi.responses import JSONResponse

SCRAPECREATORS_API_KEY  = os.getenv("SCRAPECREATORS_KEY", "")
YOUTUBE_API_KEY         = os.getenv("YOUTUBE_API_KEY", "")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
INSTAGRAM_SESSION_ID    = os.getenv("INSTAGRAM_SESSION_ID", "")
INSTAGRAM_SESSION_JSON  = os.getenv("INSTAGRAM_SESSION_JSON", "")
APIFY_TOKEN             = os.getenv("APIFY_TOKEN", "")
INSTAGRAM_USERNAME      = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD      = os.getenv("INSTAGRAM_PASSWORD", "")

# ── Instagram client (instagrapi) ─────────────────────────────────────────────
_ig_client = None
_ig_client_lock = threading.Lock()
_ig_client_error = ""

def _get_ig_client():
    global _ig_client, _ig_client_error
    if _ig_client is not None:
        return _ig_client
    with _ig_client_lock:
        if _ig_client is not None:
            return _ig_client
        try:
            from instagrapi import Client as _IGClient
            cl = _IGClient()
            cl.delay_range = [1, 3]
            # Вариант 1: готовая сессия из JSON (локально сгенерированная, без challenge)
            if INSTAGRAM_SESSION_JSON:
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                        f.write(INSTAGRAM_SESSION_JSON)
                        tmp_path = f.name
                    cl.load_settings(tmp_path)
                    os.unlink(tmp_path)
                    _ig_client = cl
                    _ig_client_error = "ok:session_json"
                    return _ig_client
                except Exception as e1:
                    _ig_client_error = f"session_json failed: {e1}"
            # Вариант 2: session_id из куки
            if INSTAGRAM_SESSION_ID:
                try:
                    from urllib.parse import unquote as _unquote
                    clean_session = _unquote(INSTAGRAM_SESSION_ID)
                    cl.login_by_sessionid(clean_session)
                    _ig_client = cl
                    _ig_client_error = "ok:sessionid"
                    return _ig_client
                except Exception as e1:
                    _ig_client_error = f"sessionid failed: {e1}"
        except Exception as e:
            _ig_client_error = str(e)
    return _ig_client

def _fetch_ig_post_location_sync(url: str) -> dict:
    """Возвращает геолокацию поста Instagram если есть геотег."""
    cl = _get_ig_client()
    if not cl:
        return {}
    try:
        media_pk = cl.media_pk_from_url(url)
        media = cl.media_info(media_pk)
        loc = getattr(media, "location", None)
        if loc:
            return {
                "name": getattr(loc, "name", "") or "",
                "lat":  getattr(loc, "lat", None),
                "lng":  getattr(loc, "lng", None),
            }
    except Exception:
        pass
    return {}

# Флаги → страны для парсинга bio
_FLAG_TO_COUNTRY = {
    "🇺🇸": "USA", "🇬🇧": "UK", "🇵🇭": "Philippines", "🇦🇺": "Australia",
    "🇨🇦": "Canada", "🇩🇪": "Germany", "🇫🇷": "France", "🇧🇷": "Brazil",
    "🇲🇽": "Mexico", "🇮🇳": "India", "🇮🇩": "Indonesia", "🇯🇵": "Japan",
    "🇰🇷": "South Korea", "🇳🇬": "Nigeria", "🇿🇦": "South Africa",
    "🇪🇸": "Spain", "🇮🇹": "Italy", "🇳🇱": "Netherlands", "🇵🇱": "Poland",
    "🇺🇦": "Ukraine", "🇸🇦": "Saudi Arabia", "🇦🇪": "UAE",
    "🇸🇬": "Singapore", "🇳🇿": "New Zealand", "🇦🇷": "Argentina",
    "🇨🇴": "Colombia", "🇵🇰": "Pakistan", "🇻🇳": "Vietnam", "🇹🇭": "Thailand",
    "🇲🇾": "Malaysia", "🇹🇼": "Taiwan", "🇨🇳": "China", "🇷🇺": "Russia",
    "🇸🇪": "Sweden", "🇳🇴": "Norway", "🇩🇰": "Denmark", "🇫🇮": "Finland",
    "🇨🇭": "Switzerland", "🇵🇹": "Portugal", "🇹🇷": "Turkey",
    "🇮🇱": "Israel", "🇪🇬": "Egypt", "🇲🇦": "Morocco", "🇰🇪": "Kenya",
    "🇬🇭": "Ghana", "🇷🇴": "Romania", "🇨🇿": "Czech Republic",
    "🇵🇪": "Peru", "🇨🇱": "Chile", "🇻🇪": "Venezuela", "🇧🇩": "Bangladesh",
}

# Язык → полное название
_LANG_NAMES = {
    "en": "English", "tl": "Filipino / Tagalog", "sq": "Albanian",
    "cy": "Welsh", "pt": "Portuguese", "hr": "Croatian", "pl": "Polish",
    "so": "Somali", "sv": "Swedish", "et": "Estonian", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "nl": "Dutch",
    "uk": "Ukrainian", "ru": "Russian", "tr": "Turkish", "ar": "Arabic",
    "hi": "Hindi", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai",
    "ko": "Korean", "ja": "Japanese", "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)", "sw": "Swahili", "af": "Afrikaans",
    "fi": "Finnish", "no": "Norwegian", "da": "Danish", "ro": "Romanian",
    "cs": "Czech", "sk": "Slovak", "hu": "Hungarian", "bg": "Bulgarian",
    "sr": "Serbian", "mk": "Macedonian", "sl": "Slovenian", "lv": "Latvian",
    "lt": "Lithuanian", "el": "Greek", "ca": "Catalan", "ms": "Malay",
    "bn": "Bengali", "ur": "Urdu", "he": "Hebrew", "fa": "Persian / Farsi",
    "az": "Azerbaijani", "ka": "Georgian", "hy": "Armenian", "kk": "Kazakh",
    "uz": "Uzbek", "am": "Amharic", "yo": "Yoruba", "ha": "Hausa",
    "ig": "Igbo",
}

# Язык → страна как fallback
_LANG_TO_COUNTRY = {
    "tl": "Philippines", "pt": "Brazil/Portugal", "es": "Latin America/Spain",
    "de": "Germany", "fr": "France", "it": "Italy", "nl": "Netherlands",
    "pl": "Poland", "uk": "Ukraine", "ru": "Russia", "tr": "Turkey",
    "ar": "Arabic-speaking", "hi": "India", "id": "Indonesia", "vi": "Vietnam",
    "th": "Thailand", "ko": "South Korea", "ja": "Japan", "zh-cn": "China",
    "sw": "East Africa", "af": "South Africa", "so": "Somalia/East Africa",
    "fi": "Finland", "sv": "Sweden", "no": "Norway", "da": "Denmark",
    "sq": "Albania", "hr": "Croatia", "cy": "Wales/UK", "ro": "Romania",
    "cs": "Czech Republic", "sk": "Slovakia", "hu": "Hungary", "bg": "Bulgaria",
    "sr": "Serbia", "mk": "Macedonia", "sl": "Slovenia", "et": "Estonia",
    "lv": "Latvia", "lt": "Lithuania", "el": "Greece", "ca": "Catalonia/Spain",
    "ms": "Malaysia", "bn": "Bangladesh", "ur": "Pakistan",
    "he": "Israel", "fa": "Iran/Persia", "az": "Azerbaijan", "ka": "Georgia",
    "hy": "Armenia", "kk": "Kazakhstan", "uz": "Uzbekistan",
    "am": "Ethiopia", "yo": "Nigeria", "ha": "West Africa", "ig": "Nigeria",
    "zh-tw": "Taiwan",
}

def _parse_bio_for_country(bio: str) -> str:
    if not bio:
        return ""
    for flag, country in _FLAG_TO_COUNTRY.items():
        if flag in bio:
            return country
    return ""

def _fetch_ig_commenter_regions_sync(usernames: list) -> dict:
    """Returns {username_lower: country} via instagrapi (city, phone code, bio flags). Max 40 users."""
    cl = _get_ig_client()
    if not cl or not usernames:
        return {}
    results = {}
    for username in usernames[:40]:
        try:
            time.sleep(0.8)
            info = cl.user_info_by_username(username)
            region = ""
            # 1. city_name (business accounts)
            if getattr(info, "city_name", None):
                region = info.city_name
            # 2. phone country code
            if not region:
                code = str(getattr(info, "public_phone_country_code", "") or "")
                if code:
                    region = f"+{code}"
            # 3. flag emoji in bio
            if not region:
                bio = getattr(info, "biography", "") or ""
                region = _parse_bio_for_country(bio)
            results[username.lower()] = region
        except Exception:
            results[username.lower()] = ""
    return results


async def _fetch_likers_instagram(client: httpx.AsyncClient, url: str, apify_token: str) -> list[dict]:
    """Fetch users who liked an Instagram post via Apify. Returns list of {username, user_region}."""
    if not apify_token:
        return []
    try:
        r = await client.post(
            "https://api.apify.com/v2/acts/apify~instagram-post-likers-scraper/run-sync-get-dataset-items",
            params={"token": apify_token, "timeout": 120},
            json={"directUrls": [url], "resultsLimit": 500},
            timeout=130,
        )
        if r.status_code not in (200, 201):
            return []
        items = r.json()
        out = []
        for item in items:
            username = (item.get("username") or item.get("ownerUsername") or "").lower()
            if not username:
                continue
            bio = item.get("biography") or item.get("bio") or ""
            region = _parse_bio_for_country(bio)
            out.append({"username": "@" + username, "user_region": region})
        return out
    except Exception:
        return []


def _save_likers_to_db(source_id: int, likers: list[dict], platform: str) -> int:
    """Save new likers to DB, skip duplicates. Returns count of new likers saved."""
    from app.database import SessionLocal
    from app.models import StoredLiker
    db = SessionLocal()
    new_count = 0
    try:
        for lk in likers:
            username = lk.get("username", "")
            if not username:
                continue
            exists = db.query(StoredLiker).filter(
                StoredLiker.source_id == source_id,
                StoredLiker.username == username,
            ).first()
            if exists:
                continue
            db.add(StoredLiker(
                source_id=source_id,
                platform=platform,
                username=username,
                user_region=lk.get("user_region", ""),
            ))
            new_count += 1
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return new_count


async def _fetch_ig_regions_apify(client: httpx.AsyncClient, usernames: list, apify_token: str) -> dict:
    """Instagram 'About country' via Apify profile scraper. Returns {username_lower: country}."""
    if not apify_token or not usernames:
        return {}
    try:
        r = await client.post(
            "https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run-sync-get-dataset-items",
            params={"token": apify_token, "timeout": 120},
            json={"usernames": usernames[:30]},
            timeout=130,
        )
        if r.status_code not in (200, 201):
            return {}
        results = {}
        for item in r.json():
            username = (item.get("username") or "").lower()
            if not username:
                continue
            region = ""
            # "About this account" country — официальное поле Instagram
            for field in ["aboutThisAccountCountry", "countryOfRegistration", "country",
                          "businessContactInfo", "city", "location", "cityName"]:
                val = item.get(field)
                if isinstance(val, dict):
                    val = val.get("addressCountry") or val.get("country") or ""
                if val and isinstance(val, str):
                    region = val
                    break
            # флаги в bio как fallback
            if not region:
                bio = item.get("biography") or item.get("bio") or ""
                region = _parse_bio_for_country(bio)
            # langdetect на bio как последний fallback
            if not region:
                bio = item.get("biography") or item.get("bio") or ""
                if bio and len(bio.strip()) >= 5:
                    try:
                        bio_lang = _detect_language(bio)
                        region = _LANG_TO_COUNTRY.get(bio_lang, "")
                    except Exception:
                        pass
            if region:
                results[username] = region
        return results
    except Exception:
        return {}

def _fetch_ig_comments_sync(url: str) -> list[dict]:
    cl = _get_ig_client()
    if not cl:
        return []
    try:
        media_pk = cl.media_pk_from_url(url)
        comments = cl.media_comments(media_pk, amount=100)
        out = []
        for c in comments:
            out.append({
                "comment_id": str(c.pk),
                "post_url":   url,
                "platform":   "Instagram",
                "author":     "@" + (c.user.username if c.user else ""),
                "comment":    c.text or "",
                "likes":      c.like_count or 0,
                "date":       c.created_at_utc.strftime("%d.%m.%Y") if c.created_at_utc else "",
                "is_reply":   bool(getattr(c, "replied_to_id", None)),
            })
        return out
    except Exception:
        return []

_sc_credits = None

def _update_sc_credits(response_data: dict):
    global _sc_credits
    val = response_data.get("credits_remaining")
    if val is not None:
        try:
            _sc_credits = int(val)
        except (TypeError, ValueError):
            pass


# ── Enrich helpers ────────────────────────────────────────────────────────────

def _detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:   return "TikTok"
    if "x.com" in u or "twitter.com" in u: return "X"
    if "instagram.com" in u: return "Instagram"
    if "youtube.com" in u or "youtu.be" in u: return "YouTube"
    return "Unknown"


import re as _re
from datetime import datetime as _dt
from urllib.parse import quote as _urlquote

SOCIAL_URL_MARKERS = ["x.com", "twitter.com", "tiktok.com", "instagram.com", "youtube.com", "youtu.be"]


def _extract_ig_shortcode(url: str):
    m = _re.search(r'/(?:reels?|p|tv)/([A-Za-z0-9_-]+)', url)
    return m.group(1) if m else None


# Rate limiter for direct Instagram API — max 3 requests per second to avoid detection
_ig_semaphore = asyncio.Semaphore(3)

async def _fetch_instagram_direct(client: httpx.AsyncClient, url: str, session_id: str) -> dict:
    """Fetch Instagram Reel metrics directly using session cookie (bypasses login wall)."""
    shortcode = _extract_ig_shortcode(url)
    if not shortcode:
        return {"status": "Not Updated"}
    try:
        async with _ig_semaphore:
            await asyncio.sleep(0.5)  # 500ms pause between requests
            r = await client.get(
                f"https://i.instagram.com/api/v1/media/by_shortcode/?shortcode={shortcode}",
                headers={
                    "Cookie": f"sessionid={session_id}",
                    "User-Agent": "Instagram 219.0.0.12.117 Android (29/10; 420dpi; 1080x2154; Xiaomi; M2004J19C; ginkgo; qcom; ru_RU; 314665256)",
                    "x-ig-app-id": "936619743392459",
                    "Accept": "*/*",
                },
                timeout=15,
            )
        if r.status_code == 200:
            items = r.json().get("items") or []
            if not items:
                return {"status": "Not Updated"}
            item = items[0]
            pub_date = ""
            try:
                ts = item.get("taken_at", 0)
                if ts:
                    pub_date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y")
            except Exception:
                pass
            return {
                "status":    "Active",
                "api_views": item.get("play_count", item.get("view_count", "")),
                "api_date":  pub_date,
                "likes":     item.get("like_count", ""),
                "comments":  item.get("comment_count", ""),
                "shares":    "",
                "saves":     item.get("saved_count", ""),
            }
        if r.status_code == 401:
            return {"status": "Session Expired"}
    except Exception:
        pass
    return {"status": "Not Updated"}


def _extract_youtube_id(url: str):
    patterns = [
        r"youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = _re.search(p, url)
        if m:
            return m.group(1)
    return None

def _parse_contractor_csv(text: str) -> list[dict]:
    rows = []
    for line in csv.reader(io.StringIO(text)):
        # URL-only format: any column contains a social URL
        url_col, views_col, date_col = None, None, None
        for i, cell in enumerate(line):
            cell = cell.strip()
            if any(m in cell.lower() for m in SOCIAL_URL_MARKERS) and cell.startswith("http"):
                url_col = i
                break
        if url_col is None:
            continue

        url = line[url_col].strip()

        # X format: col B = URL, col C = Views
        if any(d in url for d in ["x.com", "twitter.com"]):
            views = line[url_col + 1].strip().replace("\xa0", "").replace(" ", "") if url_col + 1 < len(line) else ""
            rows.append({"url": url, "views": views, "date": ""})
            continue

        # TikTok format: col A = Date, col C = URL, col F = Views
        if "tiktok.com/video" in url:
            date = line[0].strip() if url_col > 0 else ""
            views_idx = 5 if len(line) > 5 else url_col + 1
            views = line[views_idx].strip().replace("\xa0", "").replace(" ", "") if views_idx < len(line) else ""
            rows.append({"url": url, "views": views, "date": date})
            continue

        # Standard enriched-export format: URL | Views | Platform | Date | Likes | Comments | Shares | ...
        # Reads original values so they survive when API can't refresh (private accounts, etc.)
        if url_col == 0 and len(line) >= 2:
            def _clean(v): return v.strip().replace("\xa0", "").replace(" ", "")
            raw_views = _clean(line[1]) if len(line) > 1 else ""
            views     = raw_views if raw_views.lstrip("-").replace(",","").isdigit() else ""
            date      = line[3].strip() if len(line) > 3 else ""
            likes     = _clean(line[4]) if len(line) > 4 else ""
            comments  = _clean(line[5]) if len(line) > 5 else ""
            shares    = _clean(line[6]) if len(line) > 6 else ""
            rows.append({"url": url, "views": views, "date": date,
                         "likes": likes, "comments": comments, "shares": shares})
        else:
            # Generic single-URL format (just a URL per row, no views)
            rows.append({"url": url, "views": "", "date": ""})

    return rows


async def _fetch_metrics(client: httpx.AsyncClient, url: str, api_key: str, fetch_followers: bool = True) -> dict:
    """Returns metrics + status (Active / Non-Active) + api_views."""
    platform = _detect_platform(url)
    try:
        if platform == "X":
            r = await client.get(
                "https://api.scrapecreators.com/v1/twitter/tweet",
                params={"url": url}, headers={"x-api-key": api_key}, timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                _update_sc_credits(data)
                if not data.get("success") or data.get("__typename") == "TweetTombstone":
                    return {"status": "Non-Active"}
                lg = data.get("legacy", {})
                if not lg:
                    return {"status": "Non-Active"}
                pub_date = ""
                try:
                    pub_date = _dt.strptime(lg["created_at"], "%a %b %d %H:%M:%S +0000 %Y").strftime("%d.%m.%Y")
                except Exception:
                    pass
                return {
                    "status":    "Active",
                    "api_views": data.get("views", {}).get("count", ""),
                    "api_date":  pub_date,
                    "likes":     lg.get("favorite_count", ""),
                    "comments":  lg.get("reply_count", ""),
                    "shares":    lg.get("retweet_count", ""),
                    "saves":     lg.get("bookmark_count", ""),
                }
            return {"status": "Non-Active"}

        elif platform == "TikTok":
            r = await client.get(
                "https://api.scrapecreators.com/v2/tiktok/video",
                params={"url": url}, headers={"x-api-key": api_key}, timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                _update_sc_credits(data)
                detail = data.get("aweme_detail")
                if not detail:
                    return {"status": "Non-Active"}
                st = detail.get("statistics", {})
                pub_date = ""
                try:
                    ts = detail.get("create_time", 0)
                    if ts:
                        pub_date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y")
                except Exception:
                    pass
                author_meta = detail.get("author", {})
                return {
                    "status":    "Active",
                    "api_views": st.get("play_count", ""),
                    "api_date":  pub_date,
                    "likes":     st.get("digg_count", ""),
                    "comments":  st.get("comment_count", ""),
                    "shares":    st.get("share_count", ""),
                    "saves":     st.get("collect_count", ""),
                    "author":    author_meta.get("unique_id", ""),
                    "followers": author_meta.get("follower_count", ""),
                }
            return {"status": "Non-Active"}

        elif platform == "Instagram":
            # ScrapeCreators v1 — returns video_play_count, likes, comments
            clean_url = url.split("?")[0].rstrip("/") + "/"
            r = await client.get(
                "https://api.scrapecreators.com/v1/instagram/post",
                params={"url": clean_url}, headers={"x-api-key": api_key}, timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                _update_sc_credits(data)
                media = data.get("data", {}).get("xdt_shortcode_media", {})
                if not media:
                    return {"status": "Not Updated"}
                pub_date = ""
                try:
                    ts = media.get("taken_at_timestamp", 0)
                    if ts:
                        pub_date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y")
                except Exception:
                    pass
                owner = media.get("owner", {})
                ig_username = owner.get("username", "")
                ig_followers = owner.get("edge_followed_by", {}).get("count", "")
                # Профиль запрашиваем только если нужно (fetch_followers=True)
                if ig_username and not ig_followers and fetch_followers:
                    try:
                        rp = await client.get(
                            "https://api.scrapecreators.com/v1/instagram/user",
                            params={"username": ig_username},
                            headers={"x-api-key": api_key},
                            timeout=12,
                        )
                        if rp.status_code == 200:
                            pd = rp.json()
                            u = (pd.get("data") or {}).get("user") or {}
                            ig_followers = (
                                u.get("edge_followed_by", {}).get("count", "")
                                or u.get("follower_count", "")
                                or u.get("followers", "")
                            )
                    except Exception:
                        pass
                return {
                    "status":    "Active",
                    "api_views": media.get("video_play_count", media.get("video_view_count", "")),
                    "api_date":  pub_date,
                    "likes":     media.get("edge_media_preview_like", {}).get("count", ""),
                    "comments":  media.get("edge_media_preview_comment", {}).get("count", ""),
                    "shares":    "",
                    "saves":     "",
                    "author":    ig_username,
                    "followers": ig_followers,
                }
            return {"status": "Not Updated"}

        elif platform == "YouTube":
            video_id = _extract_youtube_id(url)
            if not video_id:
                return {"status": "Non-Active"}
            yt_key = YOUTUBE_API_KEY
            if not yt_key:
                return {"status": "Non-Active"}
            r = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics,status,snippet", "id": video_id, "key": yt_key},
                timeout=15,
            )
            if r.status_code == 200:
                items = r.json().get("items") or []
                if not items:
                    return {"status": "Non-Active"}
                item = items[0]
                priv = item.get("status", {}).get("privacyStatus", "public")
                if priv in ("private", "unlisted"):
                    return {"status": "Non-Active"}
                st = item.get("statistics", {})
                pub_date = ""
                try:
                    raw = item.get("snippet", {}).get("publishedAt", "")
                    if raw:
                        pub_date = _dt.strptime(raw[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
                except Exception:
                    pass
                snippet = item.get("snippet", {})
                channel_id = snippet.get("channelId", "")
                channel_title = snippet.get("channelTitle", "")
                channel_followers = ""
                if channel_id:
                    try:
                        rc = await client.get(
                            "https://www.googleapis.com/youtube/v3/channels",
                            params={"part": "statistics", "id": channel_id, "key": yt_key},
                            timeout=10,
                        )
                        if rc.status_code == 200:
                            ch = (rc.json().get("items") or [{}])[0]
                            channel_followers = ch.get("statistics", {}).get("subscriberCount", "")
                    except Exception:
                        pass
                return {
                    "status":    "Active",
                    "api_views": st.get("viewCount", ""),
                    "api_date":  pub_date,
                    "likes":     st.get("likeCount", ""),
                    "comments":  st.get("commentCount", ""),
                    "shares":    "",
                    "saves":     "",
                    "author":    channel_title,
                    "followers": channel_followers,
                }
            return {"status": "Non-Active"}

    except Exception:
        pass
    return {"status": "Non-Active"}
from app.auth import APP_PASSWORD, check_auth, create_session_token
from app.database import SessionLocal, init_db
from app.models import Campaign, Post, Run, Tag, PostTag, Vertical
from app.notion_sync import push_post_to_notion


def compute_next_run(frequency: str, schedule_days: str, from_time: datetime, schedule_time: str = "10:00"):
    """Вычисляет next_run_at на основе частоты запуска и времени."""
    try:
        h, m = map(int, (schedule_time or "10:00").split(":"))
    except Exception:
        h, m = 10, 0

    if frequency == "hourly":
        return from_time + timedelta(hours=1)

    if frequency == "daily":
        candidate = from_time.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= from_time:
            candidate += timedelta(days=1)
        return candidate

    if frequency == "weekly":
        days_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        chosen = sorted(days_map[d] for d in json.loads(schedule_days or "[]") if d in days_map)
        if not chosen:
            return from_time + timedelta(days=7)
        today_wd = from_time.weekday()
        candidate = from_time.replace(hour=h, minute=m, second=0, microsecond=0)
        for wd in chosen:
            delta = (wd - today_wd) % 7
            t = candidate + timedelta(days=delta)
            if t > from_time:
                return t
        # все дни уже прошли на этой неделе — берём первый на следующей
        delta = (chosen[0] - today_wd) % 7 or 7
        return candidate + timedelta(days=delta)

    return None  # manual
from app.parser import run_campaign
from app.scheduler import start_scheduler
from app.telegram import send_message

app = FastAPI()

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR.parent / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["fromjson"] = json.loads
templates.env.filters["tojson"]   = json.dumps


@app.on_event("startup")
def startup():
    init_db()
    start_scheduler()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if password != APP_PASSWORD:
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Неверный пароль"})
    token    = create_session_token()
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session", token, max_age=86400 * 30, httponly=True)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


@app.get("/overview", response_class=HTMLResponse)
def overview(request: Request, days: int = 30, campaign: str = "all"):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    all_campaigns = db.query(Campaign).order_by(Campaign.name).all()

    posts_q = db.query(Post)
    if campaign != "all":
        try:
            posts_q = posts_q.filter(Post.campaign_id == int(campaign))
        except ValueError:
            pass
    posts = posts_q.order_by(Post.views.desc()).all()

    # attach campaign to post
    camp_map = {c.id: c for c in all_campaigns}
    for p in posts:
        p.campaign = camp_map.get(p.campaign_id)

    stats = {
        "total":     len(posts),
        "tiktok":    sum(1 for p in posts if p.platform == "TikTok"),
        "instagram": sum(1 for p in posts if p.platform == "Instagram"),
        "youtube":   sum(1 for p in posts if p.platform == "YouTube"),
        "avg_views": "{:,.0f}".format(sum(p.views for p in posts) / len(posts)) if posts else "0",
        "avg_er":    round(sum(p.er for p in posts) / len(posts), 2) if posts else 0,
    }

    # chart data - by week
    from collections import defaultdict
    tt_by_week = defaultdict(int); ig_by_week = defaultdict(int); yt_by_week = defaultdict(int)
    for p in posts:
        try:
            pub = datetime.fromisoformat(p.published)
            week = pub.strftime("%b %d")
        except Exception:
            week = "Unknown"
        if p.platform == "TikTok": tt_by_week[week] += 1
        elif p.platform == "Instagram": ig_by_week[week] += 1
        elif p.platform == "YouTube": yt_by_week[week] += 1
    all_weeks = sorted(set(list(tt_by_week) + list(ig_by_week) + list(yt_by_week)))[-12:]

    # tags
    post_ids = [p.id for p in posts]
    all_post_tags = db.query(PostTag).filter(PostTag.post_id.in_(post_ids)).all() if post_ids else []
    used_tag_ids  = list({pt.tag_id for pt in all_post_tags})
    tags_by_id    = {t.id: t for t in db.query(Tag).filter(Tag.id.in_(used_tag_ids)).all()} if used_tag_ids else {}
    post_tags_map: dict[int, list] = {}
    for pt in all_post_tags:
        tag = tags_by_id.get(pt.tag_id)
        if tag:
            post_tags_map.setdefault(pt.post_id, []).append({"id": tag.id, "name": tag.name})
    all_tags = [{"id": t.id, "name": t.name} for t in db.query(Tag).order_by(Tag.name).all()]

    db.close()
    return templates.TemplateResponse(request=request, name="overview.html", context={
        "stats": stats, "posts": posts, "days": days,
        "all_campaigns": all_campaigns, "campaign_filter": campaign,
        "chart_labels": all_weeks,
        "chart_tiktok": [tt_by_week[w] for w in all_weeks],
        "chart_instagram": [ig_by_week[w] for w in all_weeks],
        "chart_youtube": [yt_by_week[w] for w in all_weeks],
        "all_tags_json":  json.dumps(all_tags),
        "post_tags_json": json.dumps({str(k): v for k, v in post_tags_map.items()}),
    })


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()

    all_tags = db.query(Tag).order_by(Tag.name).all()

    # посты у которых есть хотя бы один тег
    tagged_post_ids = list({pt.post_id for pt in db.query(PostTag).all()})
    posts = db.query(Post).filter(Post.id.in_(tagged_post_ids)).order_by(Post.views.desc()).all() if tagged_post_ids else []

    # attach campaign
    all_campaigns = db.query(Campaign).all()
    camp_map = {c.id: c for c in all_campaigns}
    for p in posts:
        p.campaign = camp_map.get(p.campaign_id)

    # post → tags map
    all_post_tags = db.query(PostTag).filter(PostTag.post_id.in_(tagged_post_ids)).all() if tagged_post_ids else []
    tags_by_id = {t.id: t for t in all_tags}
    post_tags_map: dict[int, list] = {}
    for pt in all_post_tags:
        tag = tags_by_id.get(pt.tag_id)
        if tag:
            post_tags_map.setdefault(pt.post_id, []).append({"id": tag.id, "name": tag.name})

    # tag counts
    tag_counts = {}
    for pt in db.query(PostTag).all():
        tag_counts[pt.tag_id] = tag_counts.get(pt.tag_id, 0) + 1

    db.close()
    tags_meta = [{"id": t.id, "name": t.name, "count": tag_counts.get(t.id, 0)} for t in all_tags]
    return templates.TemplateResponse(request=request, name="tags.html", context={
        "posts":          posts,
        "post_tags_map":  post_tags_map,          # dict[int, list] для Jinja
        "all_tags":       tags_meta,
        "all_tags_json":  json.dumps([{"id": t.id, "name": t.name} for t in all_tags]),
        "post_tags_json": json.dumps({str(k): v for k, v in post_tags_map.items()}),
    })


@app.get("/api/preview")
async def api_preview(url: str, platform: str = ""):
    try:
        if platform == "YouTube" or "youtube.com" in url or "youtu.be" in url:
            import re
            m = re.search(r'(?:shorts/|v=|youtu\.be/)([^?&/]+)', url)
            vid = m.group(1) if m else None
            if vid:
                return JSONResponse({"type": "youtube", "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg", "embed": f"https://www.youtube.com/embed/{vid}", "url": url})
        if platform == "TikTok" or "tiktok.com" in url:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"https://www.tiktok.com/oembed?url={url}", headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    d = r.json()
                    return JSONResponse({"type": "tiktok", "thumbnail": d.get("thumbnail_url"), "title": d.get("title","")[:60], "author": d.get("author_name",""), "url": url})
        if platform == "Instagram" or "instagram.com" in url:
            return JSONResponse({"type": "instagram", "thumbnail": None, "url": url})
    except Exception:
        pass
    return JSONResponse({"type": "unknown", "url": url})


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request=request, name="system.html", context={})


@app.get("/", response_class=HTMLResponse)
def campaigns_list(request: Request, q: str = ""):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db   = SessionLocal()
    rows = db.query(Campaign).order_by(Campaign.created_at.desc()).all()
    if q:
        q_lower = q.lower()
        rows = [c for c in rows if q_lower in c.name.lower() or q_lower in (c.vertical or "").lower()]
    data = []
    for c in rows:
        last_run = db.query(Run).filter(Run.campaign_id == c.id).order_by(Run.started_at.desc()).first()
        data.append({
            "campaign": c,
            "posts_count": db.query(Post).filter(Post.campaign_id == c.id).count(),
            "last_run": last_run,
        })
    db.close()
    return templates.TemplateResponse(request=request, name="campaigns.html", context={"campaigns_data": data, "q": q})


@app.post("/verticals/add")
async def vertical_add(request: Request, name: str = Form(...)):
    if not check_auth(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = SessionLocal()
    name = name.strip()
    if name and not db.query(Vertical).filter(Vertical.name == name).first():
        db.add(Vertical(name=name))
        db.commit()
    db.close()
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": True, "name": name})

def get_verticals():
    db = SessionLocal()
    items = [v.name for v in db.query(Vertical).order_by(Vertical.name).all()]
    db.close()
    return items

@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request=request, name="campaign_new.html", context={"verticals": get_verticals()})


@app.post("/campaigns/new")
async def campaign_create(
    request: Request,
    name:               str       = Form(...),
    vertical:           str       = Form(""),
    platforms:          list[str] = Form(...),
    hashtags:           str       = Form(""),
    accounts:           str       = Form(""),
    keywords:           str       = Form(""),
    min_views:          int       = Form(300000),
    min_er:             float     = Form(2.0),
    max_age_days:       int       = Form(180),
    languages:          list[str] = Form([]),
    schedule_frequency: str       = Form("manual"),
    schedule_time:      str       = Form("10:00"),
    schedule_days:      list[str] = Form([]),
    schedule_end_date:  str       = Form(""),
):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    hashtags_list = [h.strip().lstrip("#") for h in hashtags.split(",") if h.strip()]
    accounts_list = [a.strip().lstrip("@") for a in accounts.split(",") if a.strip()]
    keywords_list = [k.strip() for k in keywords.split(",") if k.strip()]
    langs         = languages if languages else ["all"]

    db  = SessionLocal()
    now = datetime.now(tz=timezone.utc)
    end_date = datetime.fromisoformat(schedule_end_date) if schedule_end_date else None
    c   = Campaign(
        name               = name,
        vertical           = vertical,
        platforms          = json.dumps(platforms),
        hashtags           = json.dumps(hashtags_list),
        accounts           = json.dumps(accounts_list),
        keywords           = json.dumps(keywords_list),
        min_views          = min_views,
        min_er             = min_er,
        max_age_days       = max_age_days,
        languages          = json.dumps(langs),
        status             = "active",
        created_at         = now,
        next_run_at        = compute_next_run(schedule_frequency, json.dumps(schedule_days), now, schedule_time),
        schedule_frequency = schedule_frequency,
        schedule_time      = schedule_time,
        schedule_days      = json.dumps(schedule_days),
        schedule_end_date  = end_date,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    cid = c.id
    db.close()

    return RedirectResponse(f"/campaigns/{cid}", status_code=302)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    c  = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        db.close()
        return RedirectResponse("/", status_code=302)
    posts = db.query(Post).filter(Post.campaign_id == campaign_id).order_by(Post.views.desc()).all()
    runs  = db.query(Run).filter(Run.campaign_id == campaign_id).order_by(Run.started_at.desc()).limit(10).all()

    # Build post → tags map
    post_ids = [p.id for p in posts]
    all_post_tags = db.query(PostTag).filter(PostTag.post_id.in_(post_ids)).all() if post_ids else []
    used_tag_ids  = list({pt.tag_id for pt in all_post_tags})
    tags_by_id    = {t.id: t for t in db.query(Tag).filter(Tag.id.in_(used_tag_ids)).all()} if used_tag_ids else {}
    post_tags_map: dict[int, list] = {}
    for pt in all_post_tags:
        tag = tags_by_id.get(pt.tag_id)
        if tag:
            post_tags_map.setdefault(pt.post_id, []).append({"id": tag.id, "name": tag.name})

    all_tags = [{"id": t.id, "name": t.name} for t in db.query(Tag).order_by(Tag.name).all()]

    ctx = {
        "campaign":       c,
        "posts":          posts,
        "runs":           runs,
        "hashtags":       json.loads(c.hashtags  or "[]"),
        "accounts":       json.loads(c.accounts  or "[]"),
        "keywords":       json.loads(c.keywords  or "[]"),
        "platforms":      json.loads(c.platforms or "[]"),
        "languages":      json.loads(c.languages or '["all"]'),
        "all_tags_json":  json.dumps(all_tags),
        "post_tags_json": json.dumps({str(k): v for k, v in post_tags_map.items()}),
    }
    db.close()
    return templates.TemplateResponse(request=request, name="campaign_detail.html", context=ctx)


# ── Inline campaign config patch ─────────────────────────────────────────────

@app.patch("/api/campaigns/{campaign_id}")
async def api_campaign_patch(request: Request, campaign_id: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    db   = SessionLocal()
    c    = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        db.close()
        return JSONResponse({"error": "not found"}, status_code=404)
    if "hashtags"     in data: c.hashtags     = json.dumps(data["hashtags"])
    if "accounts"     in data: c.accounts     = json.dumps(data["accounts"])
    if "keywords"     in data: c.keywords     = json.dumps(data["keywords"])
    if "platforms"    in data: c.platforms    = json.dumps(data["platforms"])
    if "min_views"    in data: c.min_views    = int(data["min_views"])
    if "min_er"       in data: c.min_er       = float(data["min_er"])
    if "max_age_days" in data: c.max_age_days = int(data["max_age_days"])
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


# ── Tags API ──────────────────────────────────────────────────────────────────

@app.get("/api/tags")
def api_tags_list(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db   = SessionLocal()
    tags = [{"id": t.id, "name": t.name} for t in db.query(Tag).order_by(Tag.name).all()]
    db.close()
    return JSONResponse(tags)


@app.post("/api/tags")
async def api_tag_create(request: Request, name: str = Form(...)):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db   = SessionLocal()
    name = name.strip()
    tag  = db.query(Tag).filter(Tag.name == name).first()
    if not tag:
        tag = Tag(name=name)
        db.add(tag)
        db.commit()
        db.refresh(tag)
    result = {"id": tag.id, "name": tag.name}
    db.close()
    return JSONResponse(result)


@app.post("/api/posts/{post_id}/tags/{tag_id}")
def api_post_tag_add(request: Request, post_id: int, tag_id: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db  = SessionLocal()
    exists = db.query(PostTag).filter(PostTag.post_id == post_id, PostTag.tag_id == tag_id).first()
    if not exists:
        db.add(PostTag(post_id=post_id, tag_id=tag_id))
        db.commit()
    db.close()
    return JSONResponse({"ok": True})


@app.delete("/api/posts/{post_id}/tags/{tag_id}")
def api_post_tag_remove(request: Request, post_id: int, tag_id: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = SessionLocal()
    db.query(PostTag).filter(PostTag.post_id == post_id, PostTag.tag_id == tag_id).delete()
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


# ── Bulk delete posts ─────────────────────────────────────────────────────────

@app.post("/campaigns/{campaign_id}/posts/delete")
async def posts_bulk_delete(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    ids  = form.getlist("post_ids")
    db   = SessionLocal()
    for pid in ids:
        try:
            pid_int = int(pid)
            db.query(PostTag).filter(PostTag.post_id == pid_int).delete()
            db.query(Post).filter(Post.id == pid_int, Post.campaign_id == campaign_id).delete()
        except (ValueError, TypeError):
            pass
    db.commit()
    db.close()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@app.post("/api/posts/delete")
async def posts_bulk_delete_global(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    ids  = form.getlist("post_ids")
    redirect = form.get("redirect", "/overview")
    db = SessionLocal()
    for pid in ids:
        try:
            pid_int = int(pid)
            db.query(PostTag).filter(PostTag.post_id == pid_int).delete()
            db.query(Post).filter(Post.id == pid_int).delete()
        except (ValueError, TypeError):
            pass
    db.commit()
    db.close()
    return RedirectResponse(redirect, status_code=302)


def _run_in_background(campaign_id: int):
    db  = SessionLocal()
    now = datetime.now(tz=timezone.utc)
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            return

        run = Run(campaign_id=campaign_id, started_at=now)
        db.add(run)
        db.commit()

        existing_ids = {p.post_id for p in db.query(Post).filter(Post.campaign_id == campaign_id).all()}
        new_posts    = run_campaign(campaign, existing_ids)

        for pd in new_posts:
            db.add(Post(
                campaign_id=campaign_id, post_id=pd["post_id"],
                platform=pd["platform"], account=pd["account"],
                url=pd["url"], views=pd["views"], likes=pd["likes"],
                comments=pd["comments"], shares=pd["shares"],
                er=pd["er"], published=pd["published"], language=pd["language"],
            ))
            push_post_to_notion(pd)

        run.finished_at      = datetime.now(tz=timezone.utc)
        run.posts_added      = len(new_posts)
        run.status           = "done"
        campaign.last_run_at = now
        campaign.next_run_at = compute_next_run(
            campaign.schedule_frequency or "manual",
            campaign.schedule_days or "[]",
            now,
            campaign.schedule_time or "10:00",
        )
        db.commit()

        if new_posts:
            send_message(
                f"✅ <b>TrendWatch</b>\n"
                f"Кампания: <b>{campaign.name}</b>\n"
                f"Найдено новых постов: <b>{len(new_posts)}</b>"
            )
    except Exception as e:
        db.rollback()
        run = db.query(Run).filter(
            Run.campaign_id == campaign_id, Run.status == "running"
        ).order_by(Run.started_at.desc()).first()
        if run:
            run.finished_at = datetime.now(tz=timezone.utc)
            run.status = "error"
            run.error  = str(e)
            db.commit()
    finally:
        db.close()


@app.post("/campaigns/{campaign_id}/run")
def campaign_run(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    threading.Thread(target=_run_in_background, args=[campaign_id], daemon=True).start()
    return RedirectResponse(f"/campaigns/{campaign_id}?running=1", status_code=302)


@app.post("/campaigns/{campaign_id}/toggle")
def campaign_toggle(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    c  = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if c:
        if c.status == "active":
            c.status = "paused"
        elif c.status == "paused":
            c.status = "active"
            c.next_run_at = datetime.now(tz=timezone.utc)
        db.commit()
    db.close()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@app.post("/campaigns/{campaign_id}/archive")
def campaign_archive(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    c  = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if c:
        c.status = "archived"
        db.commit()
    db.close()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@app.post("/campaigns/{campaign_id}/delete")
def campaign_delete(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    db.query(Post).filter(Post.campaign_id == campaign_id).delete()
    db.query(Run).filter(Run.campaign_id  == campaign_id).delete()
    db.query(Campaign).filter(Campaign.id == campaign_id).delete()
    db.commit()
    db.close()
    return RedirectResponse("/", status_code=302)


# ── Balance ───────────────────────────────────────────────────────────────────

_apify_balance = None

def _fetch_sc_credits_sync():
    global _sc_credits
    try:
        import requests as _requests
        r = _requests.get(
            "https://api.scrapecreators.com/v1/twitter/tweet",
            params={"url": "https://x.com/elonmusk/status/1", "cache_max_age": "30d"},
            headers={"x-api-key": SCRAPECREATORS_API_KEY},
            timeout=10,
        )
        data = r.json()
        val = data.get("credits_remaining")
        if val is not None:
            _sc_credits = int(val)
    except Exception:
        pass


def _fetch_apify_balance_sync():
    global _apify_balance
    try:
        import requests as _requests
        r = _requests.get(
            f"https://api.apify.com/v2/users/me",
            params={"token": APIFY_TOKEN},
            timeout=10,
        )
        data = r.json()
        balance = data.get("data", {}).get("plan", {}).get("monthlyUsage", {})
        # Apify returns usage in USD cents
        usage_usd = data.get("data", {}).get("monthlyUsage", 0) or 0
        limit_usd = data.get("data", {}).get("plan", {}).get("monthlyUsageCreditsUsd", 0) or 0
        _apify_balance = {"usage": round(usage_usd, 2), "limit": round(limit_usd, 2)}
    except Exception:
        pass


@app.get("/api/ig-status")
def api_ig_status(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cl = _get_ig_client()
    return JSONResponse({
        "logged_in": cl is not None,
        "username": INSTAGRAM_USERNAME or "not set",
        "error": _ig_client_error,
    })


@app.get("/api/test-tiktok-comments")
async def api_test_tiktok_comments(request: Request, url: str):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN, "timeout": 60},
            json={"postURLs": [url], "commentsPerPost": 5, "scrapeComments": True, "resultsType": "comments"},
            timeout=70,
        )
        items = r.json() if r.status_code in (200, 201) else []
        sample = items[:2] if isinstance(items, list) else items
        return JSONResponse({
            "status": r.status_code,
            "count": len(items) if isinstance(items, list) else 0,
            "sample": sample,
            "all_keys": list(sample[0].keys()) if isinstance(sample, list) and sample else []
        })


@app.get("/api/test-ig-comments")
async def api_test_ig_comments(request: Request, url: str):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    results = {}
    async with httpx.AsyncClient() as client:
        # Test Apify
        if APIFY_TOKEN:
            try:
                r = await client.post(
                    "https://api.apify.com/v2/acts/apify~instagram-comment-scraper/run-sync-get-dataset-items",
                    params={"token": APIFY_TOKEN, "timeout": 60},
                    json={"directUrls": [url], "resultsLimit": 10},
                    timeout=70,
                )
                results["apify_status"] = r.status_code
                body = r.json()
                results["apify_count"] = len(body) if isinstance(body, list) else 0
                results["apify_sample"] = body[:2] if isinstance(body, list) else body
            except Exception as e:
                results["apify_error"] = str(e)
        # Test ScrapeCreators
        if SCRAPECREATORS_API_KEY:
            try:
                r2 = await client.get(
                    "https://api.scrapecreators.com/v2/instagram/post/comments",
                    params={"url": url}, headers={"x-api-key": SCRAPECREATORS_API_KEY}, timeout=20,
                )
                results["sc_status"] = r2.status_code
                body2 = r2.json()
                results["sc_count"] = len(body2.get("comments", []))
                results["sc_sample"] = body2.get("comments", [])[:2]
                results["sc_raw_keys"] = list(body2.keys())
            except Exception as e:
                results["sc_error"] = str(e)
    return JSONResponse(results)


@app.get("/api/test-ig-likers")
async def api_test_ig_likers(request: Request, url: str):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    results = {"url": url, "apify_token_set": bool(APIFY_TOKEN)}
    async with httpx.AsyncClient(timeout=140) as client:
        for actor in ["apify~instagram-post-likers-scraper", "jaroslavhejlek~instagram-post-likers"]:
            try:
                r = await client.post(
                    f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                    params={"token": APIFY_TOKEN, "timeout": 60},
                    json={"directUrls": [url], "resultsLimit": 5},
                    timeout=70,
                )
                body = r.json()
                results[actor] = {
                    "status": r.status_code,
                    "count": len(body) if isinstance(body, list) else 0,
                    "sample": body[:2] if isinstance(body, list) else body,
                }
            except Exception as e:
                results[actor] = {"error": str(e)}
    return JSONResponse(results)


@app.get("/api/test-x-comments")
async def api_test_x_comments(request: Request, url: str):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    results = {"url": url, "apify_token_set": bool(APIFY_TOKEN)}
    async with httpx.AsyncClient(timeout=140) as client:
        # Пробуем разные форматы входных данных
        for input_fmt in [
            {"startUrls": [{"url": url}], "maxReplies": 5},
            {"startUrls": [url], "maxReplies": 5},
            {"tweetUrls": [url], "maxReplies": 5},
            {"urls": [url], "maxReplies": 5},
        ]:
            try:
                r = await client.post(
                    "https://api.apify.com/v2/acts/scraper_one~x-post-replies-scr/run-sync-get-dataset-items",
                    params={"token": APIFY_TOKEN, "timeout": 60},
                    json=input_fmt,
                    timeout=70,
                )
                body = r.json()
                results[str(input_fmt)] = {
                    "status": r.status_code,
                    "count": len(body) if isinstance(body, list) else 0,
                    "sample": body[:1] if isinstance(body, list) else body,
                }
                if r.status_code in (200, 201) and isinstance(body, list) and body:
                    results["working_format"] = input_fmt
                    break
            except Exception as e:
                results[str(input_fmt)] = {"error": str(e)}
    return JSONResponse(results)


@app.get("/api/balance")
def api_balance(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _sc_credits is None and SCRAPECREATORS_API_KEY:
        threading.Thread(target=_fetch_sc_credits_sync, daemon=True).start()
    if _apify_balance is None and APIFY_TOKEN:
        threading.Thread(target=_fetch_apify_balance_sync, daemon=True).start()
    return JSONResponse({"sc_credits": _sc_credits, "apify": _apify_balance})


# ── Enrich Reports ────────────────────────────────────────────────────────────

_enrich_tasks: dict[str, dict] = {}


async def _enrich_rows_async(rows: list[dict], api_key: str, task: dict, update_views: bool = False) -> str:
    sem = asyncio.Semaphore(50)

    async def fetch_one(client, row):
        async with sem:
            try:
                metrics = await _fetch_metrics(client, row["url"], api_key)
            except Exception:
                metrics = {"status": "Non-Active"}
            task["done"] = task.get("done", 0) + 1
            return {**row, **metrics}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[fetch_one(client, r) for r in rows],
            return_exceptions=True,
        )

    results = [r if isinstance(r, dict) else {"url": "", "views": "", "date": "", "status": "Non-Active"} for r in results]

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["URL", "Views", "Platform", "Date", "Likes", "Comments", "Shares", "Saves", "Status"])
    writer.writeheader()
    for r in results:
        views = (r.get("api_views") or r.get("views", "")) if update_views else (r.get("views") or r.get("api_views", ""))
        date  = r.get("api_date") or r.get("date", "")
        writer.writerow({
            "URL": r["url"], "Views": views,
            "Platform": _detect_platform(r["url"]),
            "Date": date,
            "Likes": r.get("likes", ""), "Comments": r.get("comments", ""),
            "Shares": r.get("shares", ""), "Saves": r.get("saves", ""),
            "Status": r.get("status", ""),
        })
    return out.getvalue()


def _run_enrich_task(task_id: str, content: bytes, filename: str, api_key: str, update_views: bool = False):
    task = _enrich_tasks[task_id]
    task["update_views"] = update_views
    try:
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            task.update({"status": "error", "error": "Не удалось прочитать кодировку файла"})
            return
        rows = _parse_contractor_csv(text)
        if not rows:
            task.update({"status": "error", "error": "URL не найдены в файле"})
            return
        task["total"] = len(rows)
        task["status"] = "processing"
        result_csv = asyncio.run(_enrich_rows_async(rows, api_key, task, update_views))
        task.update({"status": "done", "result": result_csv,
                     "filename": filename.replace(".csv", "_enriched.csv")})
    except Exception as e:
        task.update({"status": "error", "error": str(e)})


def _cleanup_old_tasks():
    cutoff = time.time() - 86400  # хранить 24 часа
    for tid in list(_enrich_tasks.keys()):
        if _enrich_tasks[tid].get("ts", 0) < cutoff:
            del _enrich_tasks[tid]
    for tid in list(_comments_tasks.keys()):
        if _comments_tasks[tid].get("ts", 0) < cutoff:
            del _comments_tasks[tid]


@app.get("/api/enrich/history")
def enrich_history(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    items = []
    for tid, t in sorted(_enrich_tasks.items(), key=lambda x: x[1].get("ts", 0), reverse=True)[:20]:
        if t.get("status") in ("done", "error"):
            items.append({
                "task_id":  tid,
                "status":   t.get("status"),
                "filename": t.get("filename", ""),
                "total":    t.get("total", 0),
                "ts":       t.get("ts", 0),
                "error":    t.get("error", ""),
            })
    return JSONResponse(items)


@app.get("/api/comments/history")
def comments_history(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    items = []
    for tid, t in sorted(_comments_tasks.items(), key=lambda x: x[1].get("ts", 0), reverse=True)[:20]:
        if t.get("status") in ("done", "error"):
            items.append({
                "task_id":        tid,
                "status":         t.get("status"),
                "filename":       t.get("filename", ""),
                "comments_count": t.get("comments_count", 0),
                "result_type":    t.get("result_type", ""),
                "ts":             t.get("ts", 0),
                "error":          t.get("error", ""),
            })
    return JSONResponse(items)


@app.get("/enrich", response_class=HTMLResponse)
def enrich_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request=request, name="enrich.html",
                                      context={"active_page": "enrich"})


@app.post("/enrich")
async def enrich_upload(request: Request, file: UploadFile = File(...), views_mode: str = Form("")):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    if not SCRAPECREATORS_API_KEY:
        return JSONResponse({"error": "SCRAPECREATORS_KEY не задан в Railway"}, status_code=500)
    content = await file.read()
    filename = file.filename or "report.csv"
    update_views = (views_mode != "keep")  # default: update all; checked = keep from file
    _cleanup_old_tasks()
    task_id = str(uuid.uuid4())
    _enrich_tasks[task_id] = {"status": "queued", "done": 0, "total": 0, "ts": time.time()}
    threading.Thread(target=_run_enrich_task,
                     args=[task_id, content, filename, SCRAPECREATORS_API_KEY],
                     kwargs={"update_views": update_views},
                     daemon=True).start()
    return RedirectResponse(f"/enrich/task/{task_id}", status_code=302)


@app.get("/enrich/task/{task_id}", response_class=HTMLResponse)
def enrich_task_page(request: Request, task_id: str):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    if task_id not in _enrich_tasks:
        return RedirectResponse("/enrich", status_code=302)
    return templates.TemplateResponse(request=request, name="enrich_task.html",
                                      context={"active_page": "enrich", "task_id": task_id})


@app.get("/api/enrich/status/{task_id}")
def enrich_task_status(request: Request, task_id: str):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task = _enrich_tasks.get(task_id)
    if not task:
        return JSONResponse({"status": "not_found"})
    return JSONResponse({"status": task.get("status"), "done": task.get("done", 0),
                         "total": task.get("total", 0), "error": task.get("error", ""),
                         "filename": task.get("filename", "")})


@app.get("/enrich/download/{task_id}")
def enrich_download(request: Request, task_id: str):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    task = _enrich_tasks.get(task_id)
    if not task or task.get("status") != "done":
        return RedirectResponse("/enrich", status_code=302)
    fname = task["filename"]
    fname_encoded = _urlquote(fname.encode("utf-8"))
    return StreamingResponse(
        io.BytesIO(task["result"].encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname_encoded}"},
    )


# ── Редактировать кампанию ────────────────────────────────────────────────────

@app.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
def campaign_edit_page(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    c  = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    db.close()
    if not c:
        return RedirectResponse("/", status_code=302)
    ctx = {
        "campaign":          c,
        "hashtags":          ", ".join(json.loads(c.hashtags  or "[]")),
        "accounts":          ", ".join(json.loads(c.accounts  or "[]")),
        "keywords":          ", ".join(json.loads(c.keywords  or "[]")),
        "platforms":         json.loads(c.platforms or "[]"),
        "languages":         json.loads(c.languages or '["all"]'),
        "verticals":         get_verticals(),
        "schedule_frequency": c.schedule_frequency or "manual",
        "schedule_time":      c.schedule_time or "10:00",
        "schedule_days":      json.loads(c.schedule_days or "[]"),
        "schedule_end_date":  c.schedule_end_date.strftime("%Y-%m-%d") if c.schedule_end_date else "",
    }
    return templates.TemplateResponse(request=request, name="campaign_edit.html", context=ctx)


@app.post("/campaigns/{campaign_id}/edit")
async def campaign_edit(
    request: Request,
    campaign_id:        int,
    name:               str       = Form(...),
    vertical:           str       = Form(""),
    platforms:          list[str] = Form(...),
    hashtags:           str       = Form(""),
    accounts:           str       = Form(""),
    keywords:           str       = Form(""),
    min_views:          int       = Form(300000),
    min_er:             float     = Form(2.0),
    max_age_days:       int       = Form(180),
    languages:          list[str] = Form([]),
    schedule_frequency: str       = Form("manual"),
    schedule_time:      str       = Form("10:00"),
    schedule_days:      list[str] = Form([]),
    schedule_end_date:  str       = Form(""),
):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    c  = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if c:
        c.name               = name
        c.vertical           = vertical
        c.platforms          = json.dumps(platforms)
        c.hashtags           = json.dumps([h.strip().lstrip("#") for h in hashtags.split(",") if h.strip()])
        c.accounts           = json.dumps([a.strip().lstrip("@") for a in accounts.split(",") if a.strip()])
        c.keywords           = json.dumps([k.strip() for k in keywords.split(",") if k.strip()])
        c.min_views          = min_views
        c.min_er             = min_er
        c.max_age_days       = max_age_days
        c.languages          = json.dumps(languages if languages else ["all"])
        c.schedule_frequency = schedule_frequency
        c.schedule_time      = schedule_time
        c.schedule_days      = json.dumps(schedule_days)
        c.schedule_end_date  = datetime.fromisoformat(schedule_end_date) if schedule_end_date else None
        c.next_run_at        = compute_next_run(schedule_frequency, json.dumps(schedule_days), datetime.now(tz=timezone.utc), schedule_time)
        db.commit()
    db.close()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


# ── Comments ──────────────────────────────────────────────────────────────────

_comments_tasks: dict[str, dict] = {}

# ── Apify TikTok region lookup ────────────────────────────────────────────────

async def _fetch_tiktok_regions_apify(usernames: list[str]) -> dict[str, str]:
    """Batch lookup TikTok profile regions via Apify. Returns {username: region}."""
    if not APIFY_TOKEN or not usernames:
        return {}
    # Strip @ prefix
    handles = [u.lstrip("@") for u in usernames]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.apify.com/v2/acts/clockworks~tiktok-profile-scraper/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN, "timeout": 120},
                json={"profiles": handles, "resultsType": "profiles", "scrapeComments": False},
                timeout=130,
            )
        if r.status_code != 200:
            return {}
        results = r.json()
        out = {}
        for item in results:
            author = item.get("authorMeta", {})
            username = author.get("name", "")
            region = author.get("region", "")
            if username and region:
                out[username.lower()] = region
        return out
    except Exception:
        return {}


# ── Language detection ────────────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    # Короткие тексты langdetect определяет неверно — требуем минимум 20 символов
    if not text or len(text.strip()) < 20:
        return ""
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return ""


# ── Save comments to DB ───────────────────────────────────────────────────────

def _save_comments_to_db(source_id: int, comments: list[dict], platform: str):
    """Save new comments to DB, skip duplicates. Returns count of new comments saved."""
    from app.database import SessionLocal
    from app.models import StoredComment
    db = SessionLocal()
    new_count = 0
    try:
        for c in comments:
            comment_id = str(c.get("comment_id") or "")
            if not comment_id:
                continue
            exists = db.query(StoredComment).filter(
                StoredComment.source_id == source_id,
                StoredComment.comment_id == comment_id,
            ).first()
            if exists:
                continue
            sc = StoredComment(
                source_id=source_id,
                platform=platform,
                comment_id=comment_id,
                author=c.get("author", ""),
                text=c.get("comment", ""),
                likes=int(c.get("likes", 0) or 0),
                date=c.get("date", ""),
                is_reply=bool(c.get("is_reply", False)),
                language=_detect_language(c.get("comment", "")),
                user_region=c.get("user_region", ""),
            )
            db.add(sc)
            new_count += 1
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return new_count


async def _fetch_comments_tiktok(client: httpx.AsyncClient, url: str, sc_key: str, apify_token: str = "") -> list[dict]:
    # 1. Clockworks Apify — возвращает authorRegion прямо в комментарии
    if apify_token:
        try:
            r = await client.post(
                "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items",
                params={"token": apify_token, "timeout": 120},
                json={"postURLs": [url], "commentsPerPost": 200, "scrapeComments": True, "resultsType": "comments"},
                timeout=130,
            )
            if r.status_code in (200, 201):
                items = r.json()
                if items:
                    out = []
                    for c in items:
                        ts = c.get("createTimeISO") or c.get("createTime") or ""
                        try:
                            date = _dt.fromisoformat(ts.replace("Z", "+00:00")).strftime("%d.%m.%Y") if ts else ""
                        except Exception:
                            date = ""
                        author_meta = c.get("authorMeta") or {}
                        out.append({
                            "comment_id": str(c.get("id") or c.get("cid") or ""),
                            "post_url":   url,
                            "platform":   "TikTok",
                            "author":     "@" + (author_meta.get("name") or c.get("uniqueId") or ""),
                            "comment":    c.get("text") or c.get("commentText") or "",
                            "likes":      c.get("diggCount") or c.get("likeCount") or 0,
                            "date":       date,
                            "is_reply":   bool(c.get("isReply") or c.get("replyCommentId")),
                            "user_region": c.get("authorRegion") or "",
                        })
                    if out:
                        return out
        except Exception:
            pass

    # 2. ScrapeCreators fallback (без authorRegion)
    try:
        r = await client.get(
            "https://api.scrapecreators.com/v1/tiktok/video/comments",
            params={"url": url}, headers={"x-api-key": sc_key}, timeout=20,
        )
        if r.status_code in (404, 410):
            raise Exception("media not found")
        if r.status_code != 200:
            return []
        body = r.json()
        err = (body.get("error") or body.get("message") or "").lower()
        if any(x in err for x in ["not found", "deleted", "unavailable", "does not exist", "no longer"]):
            raise Exception("media not found")
        out = []
        for c in body.get("comments", []):
            ts = c.get("create_time", 0)
            date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y") if ts else ""
            out.append({
                "comment_id": str(c.get("cid") or c.get("id") or ""),
                "post_url":   url,
                "platform":   "TikTok",
                "author":     "@" + c.get("user", {}).get("unique_id", ""),
                "comment":    c.get("text", ""),
                "likes":      c.get("digg_count", 0),
                "date":       date,
                "is_reply":   bool(c.get("reply_id")),
            })
        return out
    except Exception as e:
        if "media not found" in str(e):
            raise
    return []


async def _fetch_comments_instagram(client: httpx.AsyncClient, url: str, sc_key: str, apify_token: str = "") -> list[dict]:
    # 1. Apify — самый надёжный, не требует логина в Instagram
    if apify_token:
        try:
            r = await client.post(
                "https://api.apify.com/v2/acts/apify~instagram-comment-scraper/run-sync-get-dataset-items",
                params={"token": apify_token, "timeout": 120},
                json={"directUrls": [url], "resultsLimit": 200},
                timeout=130,
            )
            if r.status_code in (200, 201):
                items = r.json()
                if items:
                    out = []
                    for c in items:
                        ts = c.get("timestamp") or c.get("created_at") or ""
                        try:
                            date = _dt.fromisoformat(ts.replace("Z", "+00:00")).strftime("%d.%m.%Y") if ts else ""
                        except Exception:
                            date = str(ts)[:10] if ts else ""
                        out.append({
                            "comment_id": str(c.get("id") or c.get("commentId") or ""),
                            "post_url":   url,
                            "platform":   "Instagram",
                            "author":     "@" + (c.get("ownerUsername") or c.get("username") or ""),
                            "comment":    c.get("text") or c.get("comment") or "",
                            "likes":      c.get("likesCount") or c.get("likes") or 0,
                            "date":       date,
                            "is_reply":   bool(c.get("repliedToId") or c.get("is_reply")),
                        })
                    if out:
                        return out
        except Exception:
            pass

    # 2. ScrapeCreators fallback
    try:
        r = await client.get(
            "https://api.scrapecreators.com/v2/instagram/post/comments",
            params={"url": url}, headers={"x-api-key": sc_key}, timeout=20,
        )
        if r.status_code in (404, 410):
            raise Exception("media not found")
        if r.status_code == 200:
            body = r.json()
            err = (body.get("error") or body.get("message") or "").lower()
            if any(x in err for x in ["not found", "deleted", "unavailable", "does not exist", "no longer"]):
                raise Exception("media not found")
            raw = body.get("comments", [])
            if raw:
                out = []
                for c in raw:
                    ts = c.get("created_at", 0)
                    date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y") if ts else ""
                    out.append({
                        "comment_id": str(c.get("id") or c.get("pk") or ""),
                        "post_url":   url,
                        "platform":   "Instagram",
                        "author":     "@" + c.get("user", {}).get("username", ""),
                        "comment":    c.get("text", ""),
                        "likes":      c.get("like_count", 0),
                        "date":       date,
                        "is_reply":   bool(c.get("replied_to_author")),
                    })
                return out
    except Exception as e:
        if "media not found" in str(e):
            raise

    return []


async def _fetch_comments_x(client: httpx.AsyncClient, url: str, apify_token: str) -> list[dict]:
    """Fetch replies to an X/Twitter post via Apify scraper_one~x-post-replies-scr."""
    if not apify_token:
        return []
    try:
        r = await client.post(
            "https://api.apify.com/v2/acts/scraper_one~x-post-replies-scr/run-sync-get-dataset-items",
            params={"token": apify_token, "timeout": 120},
            json={"startUrls": [{"url": url}], "maxReplies": 200},
            timeout=130,
        )
        if r.status_code not in (200, 201):
            return []
        items = r.json()
        out = []
        for c in items:
            # Пробуем разные варианты полей — акторы X отличаются по схеме
            author = (
                c.get("author", {}).get("userName") or
                c.get("username") or
                c.get("user", {}).get("screen_name") or ""
            )
            text = c.get("text") or c.get("fullText") or c.get("full_text") or ""
            likes = int(c.get("likeCount") or c.get("favorite_count") or c.get("likes") or 0)
            raw_date = c.get("createdAt") or c.get("created_at") or ""
            date = ""
            try:
                if raw_date:
                    date = _dt.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%d.%m.%Y")
            except Exception:
                try:
                    date = _dt.strptime(raw_date, "%a %b %d %H:%M:%S +0000 %Y").strftime("%d.%m.%Y")
                except Exception:
                    date = str(raw_date)[:10]
            comment_id = str(c.get("id") or c.get("tweetId") or c.get("tweet_id") or "")
            if not comment_id or not text:
                continue
            out.append({
                "comment_id": comment_id,
                "post_url":   url,
                "platform":   "X",
                "author":     "@" + author if author and not author.startswith("@") else author,
                "comment":    text,
                "likes":      likes,
                "date":       date,
                "is_reply":   True,
            })
        return out
    except Exception:
        return []


async def _fetch_comments_youtube(client: httpx.AsyncClient, url: str, sc_key: str) -> list[dict]:
    r = await client.get(
        "https://api.scrapecreators.com/v1/youtube/video/comments",
        params={"url": url}, headers={"x-api-key": sc_key}, timeout=20,
    )
    if r.status_code in (404, 410):
        raise Exception("media not found")
    if r.status_code != 200:
        return []
    body = r.json()
    err = (body.get("error") or body.get("message") or "").lower()
    if any(x in err for x in ["not found", "deleted", "unavailable", "does not exist", "no longer"]):
        raise Exception("media not found")
    out = []
    for c in body.get("comments", []):
        date = ""
        try:
            raw = c.get("publishedTime", "")
            if raw:
                date = _dt.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d.%m.%Y")
        except Exception:
            pass
        out.append({
            "comment_id": str(c.get("id") or ""),
            "post_url":   url,
            "platform":   "YouTube",
            "author":     c.get("author", {}).get("name", ""),
            "comment":    c.get("content", ""),
            "likes":      c.get("engagement", {}).get("likes", 0),
            "date":       date,
            "is_reply":   c.get("replyLevel", 0) > 0,
        })
    return out


async def _collect_all_comments(urls: list[dict], sc_key: str, task: dict) -> list[dict]:
    sem = asyncio.Semaphore(10)
    all_comments = []

    async def fetch_one(client, row):
        async with sem:
            url = row["url"]
            platform = _detect_platform(url)
            try:
                if platform == "TikTok":
                    comments = await _fetch_comments_tiktok(client, url, sc_key)
                elif platform == "Instagram":
                    comments = await _fetch_comments_instagram(client, url, sc_key)
                elif platform == "YouTube":
                    comments = await _fetch_comments_youtube(client, url, sc_key)
                else:
                    comments = []
            except Exception:
                comments = []
            task["done"] = task.get("done", 0) + 1
            return comments

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[fetch_one(client, r) for r in urls], return_exceptions=True)

    for r in results:
        if isinstance(r, list):
            all_comments.extend(r)
    return all_comments


def _comments_to_csv(comments: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["post_url", "platform", "author", "comment", "likes", "date", "is_reply"])
    writer.writeheader()
    for c in comments:
        writer.writerow(c)
    return out.getvalue()


def _generate_audit_html(comments: list[dict], product: str, anthropic_key: str) -> str:
    import anthropic as _anthropic

    total = len(comments)
    replies = sum(1 for c in comments if c.get("is_reply"))
    main_comments = total - replies
    total_likes = sum(int(c.get("likes") or 0) for c in comments)
    platforms = {}
    for c in comments:
        p = c.get("platform", "Unknown")
        platforms[p] = platforms.get(p, 0) + 1

    comments_text = "\n".join(
        f'[{c["platform"]}] @{c["author"]}: {c["comment"]} (лайков: {c["likes"]})'
        for c in comments[:800]
    )

    prompt = f"""Ты аналитик UGC-маркетинга в агентстве UGC Ninja. Проанализируй комментарии к рекламной кампании продукта «{product}».

СТАТИСТИКА:
- Всего комментариев: {total} (основных: {main_comments}, ответов: {replies})
- Лайков на комментарии: {total_likes}
- Платформы: {', '.join(f'{k}: {v}' for k,v in platforms.items())}

КОММЕНТАРИИ:
{comments_text}

Сделай структурированный аудит на русском языке. Верни ТОЛЬКО готовый HTML (без markdown, без ```html блоков) со следующими разделами:

1. Общие показатели (таблица с цифрами)
2. Общий сентимент в % (позитивный / неоднозначный / негативный) с пояснением
3. Ключевые темы и паттерны (топ-5 тем с примерами комментариев и кол-вом упоминаний)
4. Сравнения с конкурентами (если есть)
5. Вопросы от аудитории (типы вопросов и их кол-во)
6. Скептицизм к рекламному формату (если есть)
7. Рекомендации (конкретные, по каждой проблеме)

Используй этот HTML-скелет (заполни данными):

<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Аудит комментариев — {product}</title>
<style>
body{{font-family:Inter,sans-serif;max-width:900px;margin:40px auto;padding:0 24px;color:#1a1a1a;line-height:1.6}}
h1{{font-size:2rem;margin-bottom:4px}}
.subtitle{{color:#666;margin-bottom:32px;font-size:.95rem}}
h2{{font-size:1.2rem;border-bottom:2px solid #eee;padding-bottom:8px;margin-top:40px}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin:20px 0}}
.stat-card{{background:#f8f9fa;border-radius:10px;padding:16px;text-align:center}}
.stat-num{{font-size:1.8rem;font-weight:700;color:#0d6efd}}
.stat-label{{font-size:.8rem;color:#666;margin-top:4px}}
.sentiment{{display:flex;gap:16px;margin:16px 0}}
.sent-block{{flex:1;border-radius:8px;padding:12px 16px;text-align:center}}
.sent-pos{{background:#d1e7dd}}.sent-mix{{background:#fff3cd}}.sent-neg{{background:#f8d7da}}
.sent-pct{{font-size:1.6rem;font-weight:700}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}
th{{background:#f8f9fa;padding:10px;text-align:left;font-size:.85rem;border-bottom:2px solid #dee2e6}}
td{{padding:10px;border-bottom:1px solid #f0f0f0;font-size:.9rem}}
.quote{{background:#f8f9fa;border-left:3px solid #0d6efd;padding:8px 12px;margin:8px 0;border-radius:0 6px 6px 0;font-style:italic;font-size:.9rem}}
.rec{{background:#e8f4fd;border-radius:8px;padding:12px 16px;margin:8px 0}}
.badge{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:.75rem;font-weight:600}}
.badge-pos{{background:#d1e7dd;color:#0f5132}}.badge-neg{{background:#f8d7da;color:#842029}}.badge-neu{{background:#fff3cd;color:#664d03}}
</style>
</head>
<body>
<!-- ЗАПОЛНИ ЗДЕСЬ ВЕСЬ КОНТЕНТ -->
</body>
</html>"""

    client = _anthropic.Anthropic(api_key=anthropic_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def _run_comments_task(task_id: str, content: bytes, product: str, mode: str, sc_key: str, anthropic_key: str):
    task = _comments_tasks[task_id]
    try:
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            task.update({"status": "error", "error": "Не удалось прочитать файл"})
            return

        urls = []
        for line in csv.reader(io.StringIO(text)):
            for cell in line:
                cell = cell.strip()
                if cell.startswith("http") and any(m in cell.lower() for m in SOCIAL_URL_MARKERS):
                    urls.append({"url": cell})
                    break

        if not urls:
            task.update({"status": "error", "error": "URL не найдены в файле"})
            return

        task["total"] = len(urls)
        task["status"] = "collecting"

        comments = asyncio.run(_collect_all_comments(urls, sc_key, task))
        task["comments_count"] = len(comments)

        if mode == "audit":
            task["status"] = "auditing"
            result = _generate_audit_html(comments, product, anthropic_key)
            task.update({"status": "done", "result": result, "result_type": "html",
                         "filename": f"{product}_audit.html"})
        else:
            result = _comments_to_csv(comments)
            task.update({"status": "done", "result": result, "result_type": "csv",
                         "filename": f"{product}_comments.csv"})
    except Exception as e:
        task.update({"status": "error", "error": str(e)})


@app.get("/comments", response_class=HTMLResponse)
def comments_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request=request, name="comments.html",
                                      context={"active_page": "comments"})


@app.post("/comments")
async def comments_upload(request: Request, file: UploadFile = File(...),
                          product: str = Form(""), mode: str = Form("comments")):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    if not SCRAPECREATORS_API_KEY:
        return JSONResponse({"error": "SCRAPECREATORS_KEY не задан"}, status_code=500)
    if mode == "audit" and not ANTHROPIC_API_KEY:
        return JSONResponse({"error": "ANTHROPIC_API_KEY не задан"}, status_code=500)

    content = await file.read()
    product_name = product.strip() or "Продукт"
    _cleanup_old_tasks()
    task_id = str(uuid.uuid4())
    _comments_tasks[task_id] = {"status": "queued", "done": 0, "total": 0, "ts": time.time()}
    threading.Thread(
        target=_run_comments_task,
        args=[task_id, content, product_name, mode, SCRAPECREATORS_API_KEY, ANTHROPIC_API_KEY],
        daemon=True,
    ).start()
    return RedirectResponse(f"/comments/task/{task_id}", status_code=302)


@app.get("/comments/task/{task_id}", response_class=HTMLResponse)
def comments_task_page(request: Request, task_id: str):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    if task_id not in _comments_tasks:
        return RedirectResponse("/comments", status_code=302)
    return templates.TemplateResponse(request=request, name="comments_task.html",
                                      context={"active_page": "comments", "task_id": task_id})


@app.get("/api/comments/status/{task_id}")
def comments_task_status(request: Request, task_id: str):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task = _comments_tasks.get(task_id)
    if not task:
        return JSONResponse({"status": "not_found"})
    return JSONResponse({
        "status":         task.get("status"),
        "done":           task.get("done", 0),
        "total":          task.get("total", 0),
        "comments_count": task.get("comments_count", 0),
        "result_type":    task.get("result_type", ""),
        "filename":       task.get("filename", ""),
        "error":          task.get("error", ""),
    })


@app.get("/comments/download/{task_id}")
def comments_download(request: Request, task_id: str):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    task = _comments_tasks.get(task_id)
    if not task or task.get("status") != "done":
        return RedirectResponse("/comments", status_code=302)
    fname = task["filename"]
    result_type = task.get("result_type", "csv")
    if result_type == "html":
        return StreamingResponse(
            io.BytesIO(task["result"].encode("utf-8")),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(fname.encode('utf-8'))}"},
        )
    return StreamingResponse(
        io.BytesIO(task["result"].encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(fname.encode('utf-8'))}"},
    )


# ── Comment Projects ───────────────────────────────────────────────────────────

from app.models import CommentProject, CommentSource, StoredComment as _StoredComment


@app.get("/comments/projects", response_class=HTMLResponse)
def comment_projects_list(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    projects = db.query(CommentProject).order_by(CommentProject.created_at.desc()).all()
    result = []
    for p in projects:
        sources = db.query(CommentSource).filter(CommentSource.project_id == p.id).all()
        total_comments = sum(s.comments_count for s in sources)
        last_fetched = max((s.last_fetched_at for s in sources if s.last_fetched_at), default=None)
        result.append({"project": p, "sources_count": len(sources), "comments_count": total_comments, "last_fetched": last_fetched})
    db.close()
    return templates.TemplateResponse(request=request, name="comment_projects.html",
                                      context={"active_page": "comments", "projects": result})


@app.post("/comments/projects")
async def comment_project_create(request: Request, name: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    existing = db.query(CommentProject).filter(CommentProject.name == name).first()
    if not existing:
        db.add(CommentProject(name=name))
        db.commit()
        p = db.query(CommentProject).filter(CommentProject.name == name).first()
    else:
        p = existing
    pid = p.id
    db.close()
    return RedirectResponse(f"/comments/projects/{pid}", status_code=302)


@app.get("/comments/projects/{pid}", response_class=HTMLResponse)
def comment_project_detail(request: Request, pid: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    project = db.query(CommentProject).filter(CommentProject.id == pid).first()
    if not project:
        db.close()
        return RedirectResponse("/comments/projects", status_code=302)
    sources = db.query(CommentSource).filter(CommentSource.project_id == pid).all()

    # GEO stats
    all_comments = db.query(_StoredComment).filter(
        _StoredComment.source_id.in_([s.id for s in sources])
    ).all() if sources else []

    lang_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}       # только реальный user_region из профиля
    lang_region_counts: dict[str, int] = {}  # приблизительно: язык → страна
    for c in all_comments:
        if c.language:
            lang_counts[c.language] = lang_counts.get(c.language, 0) + 1
        if c.user_region:
            region_counts[c.user_region] = region_counts.get(c.user_region, 0) + 1
        if c.language:
            approx = _LANG_TO_COUNTRY.get(c.language)
            if approx:
                lang_region_counts[approx] = lang_region_counts.get(approx, 0) + 1

    def _lang_label(code: str) -> str:
        name = _LANG_NAMES.get(code) or code
        country = _LANG_TO_COUNTRY.get(code)
        return f"{name} · {country}" if country else name
    # 3-tuple: (display_label, iso_code, count) — iso_code нужен для фильтра комментов
    lang_stats = [(_lang_label(k), k, v) for k, v in sorted(lang_counts.items(), key=lambda x: -x[1])[:10]]
    region_stats = sorted(region_counts.items(), key=lambda x: -x[1])[:10]
    lang_region_stats_comments = sorted(lang_region_counts.items(), key=lambda x: -x[1])[:10]
    total = len(all_comments)

    # Лайкеры
    from app.models import StoredLiker as _StoredLiker
    source_ids = [s.id for s in sources]
    all_likers = db.query(_StoredLiker).filter(
        _StoredLiker.source_id.in_(source_ids)
    ).all() if sources else []
    liker_region_counts: dict[str, int] = {}
    liker_per_source: dict[int, int] = {}
    username_sources: dict[str, set] = {}
    for lk in all_likers:
        if lk.user_region:
            liker_region_counts[lk.user_region] = liker_region_counts.get(lk.user_region, 0) + 1
        liker_per_source[lk.source_id] = liker_per_source.get(lk.source_id, 0) + 1
        if lk.username:
            username_sources.setdefault(lk.username, set()).add(lk.source_id)
    liker_region_stats = sorted(liker_region_counts.items(), key=lambda x: -x[1])[:10]
    total_likers = len(set(lk.username for lk in all_likers if lk.username))
    # Топ лайкеров (все, сортировка по кол-ву постов)
    top_likers = sorted(
        [(u, len(srcs)) for u, srcs in username_sources.items()],
        key=lambda x: -x[1]
    )[:30]
    # Повторяющиеся лайкеры (лайкнули 2+ постов в проекте)
    repeated_likers = [(u, c) for u, c in top_likers if c > 1]

    author_counts: dict[str, int] = {}
    for c in all_comments:
        if c.author:
            author_counts[c.author] = author_counts.get(c.author, 0) + 1
    top_commenters = sorted(author_counts.items(), key=lambda x: -x[1])[:30]

    # Статистика по подрядчикам
    source_comments = {}
    for c in all_comments:
        source_comments[c.source_id] = source_comments.get(c.source_id, 0) + 1

    provider_stats = {}
    for s in sources:
        p = s.provider or "—"
        if p not in provider_stats:
            provider_stats[p] = {"posts": 0, "comments": 0, "active": 0, "deleted": 0}
        provider_stats[p]["posts"] += 1
        provider_stats[p]["comments"] += source_comments.get(s.id, 0)
        if s.status == "deleted":
            provider_stats[p]["deleted"] += 1
        else:
            provider_stats[p]["active"] += 1
    provider_stats = sorted(provider_stats.items(), key=lambda x: -x[1]["comments"])

    providers = sorted({s.provider for s in sources if s.provider})

    db.close()
    return templates.TemplateResponse(request=request, name="comment_project_detail.html", context={
        "active_page": "comments",
        "project": project,
        "sources": sources,
        "total_comments": total,
        "lang_stats": lang_stats,
        "region_stats": region_stats,
        "top_commenters": top_commenters,
        "provider_stats": provider_stats,
        "providers": providers,
        "total_likers": total_likers,
        "liker_region_stats": liker_region_stats,
        "lang_region_stats_comments": lang_region_stats_comments,
        "top_likers": top_likers,
        "repeated_likers": repeated_likers,
        "liker_per_source": liker_per_source,
    })


@app.post("/comments/projects/{pid}/sources")
async def comment_project_add_sources(request: Request, pid: int, urls: str = Form(""), file: UploadFile = File(None)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    project = db.query(CommentProject).filter(CommentProject.id == pid).first()
    if not project:
        db.close()
        return RedirectResponse("/comments/projects", status_code=302)

    raw_lines = []
    if file and file.filename:
        content = await file.read()
        raw_lines += content.decode("utf-8-sig", errors="ignore").splitlines()
    if urls.strip():
        raw_lines += urls.strip().splitlines()

    added = 0
    for line in raw_lines:
        line = line.strip().strip(",").strip()
        if not line.startswith("http"):
            continue
        platform = _detect_platform(line)
        if platform == "Unknown":
            continue
        existing = db.query(CommentSource).filter(
            CommentSource.project_id == pid, CommentSource.url == line
        ).first()
        if not existing:
            db.add(CommentSource(project_id=pid, url=line, platform=platform))
            added += 1
    db.commit()
    db.close()
    return RedirectResponse(f"/comments/projects/{pid}", status_code=302)


@app.post("/comments/projects/{pid}/sources/upload")
async def comment_project_upload_sources(request: Request, pid: int, file: UploadFile = File(...)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    project = db.query(CommentProject).filter(CommentProject.id == pid).first()
    if not project:
        db.close()
        return RedirectResponse("/comments/projects", status_code=302)
    import csv as _csv
    content = await file.read()
    text = content.decode("utf-8-sig", errors="ignore")
    try:
        reader = _csv.DictReader(text.splitlines())
        headers = [h.lower().strip() for h in (reader.fieldnames or [])]
        # Ищем колонку с URL
        url_col = next((h for h in (reader.fieldnames or []) if h.lower().strip() in ("post url", "url", "link")), None)
        provider_col = next((h for h in (reader.fieldnames or []) if h.lower().strip() == "provider"), None)
        creator_col = next((h for h in (reader.fieldnames or []) if h.lower().strip() == "creator"), None)

        if url_col:
            for row in reader:
                url = (row.get(url_col) or "").strip()
                if not url.startswith("http"):
                    continue
                platform = _detect_platform(url)
                if platform == "Unknown":
                    continue
                provider = (row.get(provider_col) or "").strip() if provider_col else ""
                creator = (row.get(creator_col) or "").strip() if creator_col else ""
                existing = db.query(CommentSource).filter(
                    CommentSource.project_id == pid, CommentSource.url == url
                ).first()
                if not existing:
                    db.add(CommentSource(project_id=pid, url=url, platform=platform,
                                        provider=provider, creator=creator))
                else:
                    # Обновляем метаданные если уже есть
                    if provider: existing.provider = provider
                    if creator: existing.creator = creator
        else:
            # Fallback — файл без заголовков, просто ссылки
            for row in reader:
                for cell in row.values():
                    url = (cell or "").strip()
                    if not url.startswith("http"):
                        continue
                    platform = _detect_platform(url)
                    if platform == "Unknown":
                        continue
                    existing = db.query(CommentSource).filter(
                        CommentSource.project_id == pid, CommentSource.url == url
                    ).first()
                    if not existing:
                        db.add(CommentSource(project_id=pid, url=url, platform=platform))
    except Exception:
        pass
    db.commit()
    db.close()
    return RedirectResponse(f"/comments/projects/{pid}", status_code=302)


@app.post("/comments/projects/{pid}/delete-source")
async def comment_project_delete_source(request: Request, pid: int, source_id: int = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    db.query(_StoredComment).filter(_StoredComment.source_id == source_id).delete()
    db.query(CommentSource).filter(CommentSource.id == source_id, CommentSource.project_id == pid).delete()
    db.commit()
    db.close()
    return RedirectResponse(f"/comments/projects/{pid}", status_code=302)


@app.get("/comments/projects/{pid}/comments-by")
async def comments_by_filter(request: Request, pid: int, type: str = "", value: str = ""):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from app.database import SessionLocal
    from app.models import CommentSource
    from sqlalchemy import or_
    db = SessionLocal()
    source_ids = [s.id for s in db.query(CommentSource).filter(CommentSource.project_id == pid).all()]
    if not source_ids:
        db.close()
        return JSONResponse([])
    q = db.query(_StoredComment).filter(_StoredComment.source_id.in_(source_ids))
    if type == "language":
        q = q.filter(_StoredComment.language == value)
    elif type == "region":
        lang_codes = [k for k, v in _LANG_TO_COUNTRY.items() if v == value]
        conds = [_StoredComment.user_region == value]
        if lang_codes:
            conds.append(_StoredComment.language.in_(lang_codes))
        q = q.filter(or_(*conds))
    comments = q.order_by(_StoredComment.fetched_at.desc()).limit(100).all()
    db.close()
    return JSONResponse([{
        "author": c.author, "text": c.text, "platform": c.platform,
        "date": c.date, "language": c.language, "user_region": c.user_region,
    } for c in comments])


@app.post("/comments/projects/{pid}/recalc-languages")
async def recalc_languages(request: Request, pid: int):
    """Пересчитать язык всех комментариев проекта по текущим правилам (мин. 20 символов)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from app.database import SessionLocal
    from app.models import CommentSource
    db = SessionLocal()
    source_ids = [s.id for s in db.query(CommentSource).filter(CommentSource.project_id == pid).all()]
    comments = db.query(_StoredComment).filter(_StoredComment.source_id.in_(source_ids)).all()
    updated = 0
    for c in comments:
        new_lang = _detect_language(c.text or "")
        if new_lang != (c.language or ""):
            c.language = new_lang
            updated += 1
    db.commit()
    db.close()
    return JSONResponse({"updated": updated, "total": len(comments)})


@app.get("/comments/projects/{pid}/sources/{sid}/likers")
async def comment_source_likers(request: Request, pid: int, sid: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from app.database import SessionLocal
    from app.models import StoredLiker
    db = SessionLocal()
    likers = db.query(StoredLiker).filter(StoredLiker.source_id == sid).order_by(StoredLiker.fetched_at.desc()).all()
    db.close()
    return JSONResponse([{"username": l.username, "user_region": l.user_region or ""} for l in likers])


@app.get("/comments/projects/{pid}/sources/{sid}/comments")
async def comment_source_comments(request: Request, pid: int, sid: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from app.database import SessionLocal
    db = SessionLocal()
    comments = db.query(_StoredComment).filter(_StoredComment.source_id == sid).order_by(_StoredComment.fetched_at.desc()).all()
    db.close()
    return JSONResponse([{
        "author": c.author, "text": c.text, "likes": c.likes,
        "date": c.date, "is_reply": c.is_reply, "language": c.language, "user_region": c.user_region,
    } for c in comments])


def _run_project_comments_task(task_id: str, pid: int, sc_key: str, apify_token: str):
    async def _inner():
        from app.database import SessionLocal
        from app.models import CommentProject, CommentSource
        db = SessionLocal()
        sources = db.query(CommentSource).filter(CommentSource.project_id == pid).all()
        db.close()

        task = _comments_tasks[task_id]
        # X/Twitter собираем через Apify если токен есть, иначе пропускаем
        active_sources = sources if apify_token else [s for s in sources if (s.platform or _detect_platform(s.url)) not in ("X", "Twitter")]
        task["total"] = len(active_sources)
        task["status"] = "collecting"

        sem = asyncio.Semaphore(5)  # 5 постов параллельно

        async def process_source(client: httpx.AsyncClient, source):
            async with sem:
                if task.get("status") == "cancelled":
                    return
                platform = source.platform or _detect_platform(source.url)
                try:
                    if platform == "TikTok":
                        comments = await _fetch_comments_tiktok(client, source.url, sc_key, apify_token)
                    elif platform == "Instagram":
                        comments = await _fetch_comments_instagram(client, source.url, sc_key, apify_token)
                    elif platform == "YouTube":
                        comments = await _fetch_comments_youtube(client, source.url, sc_key)
                    elif platform in ("X", "Twitter"):
                        comments = await _fetch_comments_x(client, source.url, apify_token)
                    else:
                        comments = []

                    _save_comments_to_db(source.id, comments, platform)

                    db2 = SessionLocal()
                    src = db2.query(CommentSource).filter(CommentSource.id == source.id).first()
                    if src:
                        src.last_fetched_at = datetime.utcnow()
                        src.comments_count = db2.query(_StoredComment).filter(_StoredComment.source_id == source.id).count()
                        db2.commit()
                    db2.close()

                except Exception as fetch_err:
                    err_str = str(fetch_err).lower()
                    if any(x in err_str for x in ["not found", "404", "deleted", "media not found", "no media", "media_not_found"]):
                        try:
                            db_err = SessionLocal()
                            src_err = db_err.query(CommentSource).filter(CommentSource.id == source.id).first()
                            if src_err:
                                src_err.status = "deleted"
                                db_err.commit()
                            db_err.close()
                        except Exception:
                            pass
                finally:
                    task["done"] = task.get("done", 0) + 1

        async with httpx.AsyncClient(timeout=140) as client:
            await asyncio.gather(*[process_source(client, s) for s in active_sources])

        # Метрики постов (views, likes, comments) → ER
        task["status"] = "fetching_metrics"
        try:
            async with httpx.AsyncClient(timeout=20) as mc:
                async def fetch_and_save_metrics(source):
                    try:
                        m = await _fetch_metrics(mc, source.url, sc_key,
                                                  fetch_followers=not bool(source.post_followers))
                        views          = int(m.get("api_views") or 0)
                        likes          = int(m.get("likes") or 0)
                        comments_total = int(m.get("comments") or 0)
                        er             = round((likes + comments_total) / views * 100, 2) if views > 0 else None
                        author         = str(m.get("author") or "")
                        followers      = int(m.get("followers") or 0)
                        db_m = SessionLocal()
                        src_m = db_m.query(CommentSource).filter(CommentSource.id == source.id).first()
                        if src_m:
                            # Пост удалён — ставим статус deleted
                            if m.get("status") == "Non-Active":
                                src_m.status = "deleted"
                            else:
                                if views:          src_m.post_views = views
                                if likes:          src_m.post_likes = likes
                                if comments_total: src_m.post_comments_total = comments_total
                                if er is not None: src_m.post_er = er
                                if author:         src_m.post_author = author
                                # Подписчики собираем только один раз
                                if followers and not src_m.post_followers:
                                    src_m.post_followers = followers
                            db_m.commit()
                        db_m.close()
                    except Exception:
                        pass
                sem_m = asyncio.Semaphore(10)
                async def _m_guarded(s):
                    async with sem_m:
                        await fetch_and_save_metrics(s)
                # Метрики для ВСЕХ постов включая Twitter/X
                await asyncio.wait_for(
                    asyncio.gather(*[_m_guarded(s) for s in sources], return_exceptions=True),
                    timeout=180  # 3 минуты максимум
                )
        except Exception:
            pass

        # GEO батчем в конце — один вызов на всех авторов
        task["status"] = "geo_lookup"
        try:
            from app.models import StoredComment as _SC2
            db_bf = SessionLocal()
            source_ids = [s.id for s in db_bf.query(CommentSource).filter(CommentSource.project_id == pid).all()]

            # TikTok: fallback для тех у кого нет authorRegion
            tk_no_region = (db_bf.query(_SC2)
                .filter(_SC2.source_id.in_(source_ids), _SC2.platform == "TikTok")
                .filter((_SC2.user_region == None) | (_SC2.user_region == "")).all())
            if tk_no_region and apify_token:
                tk_authors = list({c.author.lstrip("@") for c in tk_no_region if c.author})
                tk_regions = await _fetch_tiktok_regions_apify(tk_authors)
                for c in tk_no_region:
                    r = tk_regions.get((c.author or "").lstrip("@").lower(), "")
                    if r: c.user_region = r
                db_bf.commit()

            # Instagram: Apify profile scraper (About country) + instagrapi bio fallback
            ig_no_region = (db_bf.query(_SC2)
                .filter(_SC2.source_id.in_(source_ids), _SC2.platform == "Instagram")
                .filter((_SC2.user_region == None) | (_SC2.user_region == "")).all())
            if ig_no_region:
                ig_authors = list({c.author.lstrip("@") for c in ig_no_region if c.author})
                async with httpx.AsyncClient(timeout=140) as geo_client:
                    apify_regions = await _fetch_ig_regions_apify(geo_client, ig_authors, apify_token) if apify_token else {}
                # instagrapi fallback пропускаем — не работает на Railway (висит на логине)
                for c in ig_no_region:
                    r = apify_regions.get((c.author or "").lstrip("@").lower(), "")
                    if r: c.user_region = r
                db_bf.commit()

            db_bf.close()
        except Exception:
            pass

        # Лайкеры — только Instagram, только если есть Apify токен
        task["status"] = "collecting_likers"
        try:
            from app.models import StoredLiker as _SL
            ig_active = [s for s in active_sources if (s.platform or _detect_platform(s.url)) == "Instagram"]
            if ig_active and apify_token:
                async def collect_likers(lk_client: httpx.AsyncClient, source):
                    likers = await _fetch_likers_instagram(lk_client, source.url, apify_token)
                    _save_likers_to_db(source.id, likers, "Instagram")
                    db_lk = SessionLocal()
                    src_lk = db_lk.query(CommentSource).filter(CommentSource.id == source.id).first()
                    if src_lk:
                        src_lk.likers_count = db_lk.query(_SL).filter(_SL.source_id == source.id).count()
                        db_lk.commit()
                    db_lk.close()

                async with httpx.AsyncClient(timeout=140) as lk_client:
                    sem_lk = asyncio.Semaphore(3)
                    async def _lk_guarded(s):
                        async with sem_lk:
                            await collect_likers(lk_client, s)
                    await asyncio.gather(*[_lk_guarded(s) for s in ig_active[:30]])

                # Apify profile lookup для лайкеров без региона
                db_geo2 = SessionLocal()
                lk_src_ids = [s.id for s in ig_active]
                lk_no_region = (db_geo2.query(_SL)
                    .filter(_SL.source_id.in_(lk_src_ids))
                    .filter((_SL.user_region == None) | (_SL.user_region == "")).all())
                if lk_no_region and apify_token:
                    lk_usernames = list({l.username.lstrip("@") for l in lk_no_region if l.username})
                    async with httpx.AsyncClient(timeout=140) as geo2_client:
                        lk_apify_regions = await _fetch_ig_regions_apify(geo2_client, lk_usernames, apify_token)
                    for l in lk_no_region:
                        r = lk_apify_regions.get((l.username or "").lstrip("@").lower(), "")
                        if r:
                            l.user_region = r
                    db_geo2.commit()
                db_geo2.close()
        except Exception:
            pass

        # Total comments
        db3 = SessionLocal()
        srcs = db3.query(CommentSource).filter(CommentSource.project_id == pid).all()
        total = sum(s.comments_count for s in srcs)
        db3.close()
        task["comments_count"] = total
        task["status"] = "done"

    asyncio.run(_inner())


@app.post("/comments/projects/{pid}/run")
async def comment_project_run(request: Request, pid: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    _cleanup_old_tasks()
    task_id = str(uuid.uuid4())
    _comments_tasks[task_id] = {"status": "queued", "done": 0, "total": 0, "ts": time.time(), "project_id": pid}
    threading.Thread(
        target=_run_project_comments_task,
        args=[task_id, pid, SCRAPECREATORS_API_KEY, APIFY_TOKEN],
        daemon=True,
    ).start()
    return RedirectResponse(f"/comments/projects/{pid}", status_code=302)


@app.post("/comments/projects/{pid}/cancel")
async def comment_project_cancel(request: Request, pid: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    for task in _comments_tasks.values():
        if task.get("project_id") == pid and task.get("status") not in ("done", "cancelled", "error"):
            task["status"] = "cancelled"
    return RedirectResponse(f"/comments/projects/{pid}", status_code=302)


@app.get("/api/comments/projects/{pid}/task-status")
def comment_project_task_status(request: Request, pid: int):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    for tid, task in sorted(_comments_tasks.items(), key=lambda x: -x[1].get("ts", 0)):
        if task.get("project_id") == pid:
            return JSONResponse({
                "task_id": tid,
                "status": task.get("status"),
                "done": task.get("done", 0),
                "total": task.get("total", 0),
                "skipped_x": task.get("skipped_x", 0),
                "comments_count": task.get("comments_count", 0),
            })
    return JSONResponse({"status": "idle"})


@app.get("/comments/projects/{pid}/download")
def comment_project_download(request: Request, pid: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    from app.database import SessionLocal
    db = SessionLocal()
    project = db.query(CommentProject).filter(CommentProject.id == pid).first()
    sources = db.query(CommentSource).filter(CommentSource.project_id == pid).all()
    comments = db.query(_StoredComment).filter(
        _StoredComment.source_id.in_([s.id for s in sources])
    ).order_by(_StoredComment.fetched_at.desc()).all() if sources else []
    db.close()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["post_url", "platform", "author", "comment", "likes", "date", "is_reply", "language", "user_region"])
    for c in comments:
        src = next((s for s in sources if s.id == c.source_id), None)
        writer.writerow([src.url if src else "", c.platform, c.author, c.text, c.likes, c.date, c.is_reply, c.language, c.user_region])

    fname = f"{project.name}_comments.csv" if project else "comments.csv"
    return StreamingResponse(
        io.BytesIO(out.getvalue().encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(fname.encode('utf-8'))}"},
    )
