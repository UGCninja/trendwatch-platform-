import json
import os
import re
import requests
from datetime import datetime, timedelta, timezone
from langdetect import detect as detect_lang, LangDetectException

SCRAPECREATORS_KEY = os.getenv("SCRAPECREATORS_KEY", "")
YOUTUBE_API_KEY    = os.getenv("YOUTUBE_API_KEY", "")
SC_HEADERS = {"x-api-key": SCRAPECREATORS_KEY}
YT_BASE    = "https://www.googleapis.com/youtube/v3"

KNOWN_LANGS = {"en", "ru", "de", "fr", "es", "pt", "tr"}


def detect_language(text: str) -> str:
    if not text or len(text.strip()) < 15:
        return "other"
    try:
        lang = detect_lang(text)
        return lang if lang in KNOWN_LANGS else "other"
    except LangDetectException:
        return "other"


def calc_er(likes, comments, shares, views) -> float:
    base = views if views > 0 else likes
    return round((likes + comments + shares) / base * 100, 2) if base > 0 else 0.0


def parse_unix(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def parse_iso(s) -> str:
    return s[:19] + "+00:00" if s else ""


def normalize_tiktok(v: dict, source_label: str) -> dict:
    stats    = v.get("statistics", {})
    views    = stats.get("play_count", 0) or 0
    likes    = stats.get("digg_count", 0) or 0
    comments = stats.get("comment_count", 0) or 0
    shares   = stats.get("share_count", 0) or 0
    aweme_id = v.get("aweme_id", "")
    url = v.get("share_url") or v.get("share_info", {}).get("share_url") or ""
    if not url:
        author = v.get("author", {}).get("unique_id") or source_label
        url = f"https://www.tiktok.com/@{author}/video/{aweme_id}"
    caption = (v.get("desc") or "")[:300]
    return {
        "post_id":   aweme_id,
        "platform":  "TikTok",
        "account":   source_label,
        "caption":   caption,
        "url":       url,
        "views":     views,
        "likes":     likes,
        "comments":  comments,
        "shares":    shares,
        "er":        calc_er(likes, comments, shares, views),
        "published": parse_unix(v.get("create_time", 0)),
        "language":  detect_language(caption),
    }


def fetch_tiktok_account(handle: str) -> list:
    resp = requests.get(
        "https://api.scrapecreators.com/v3/tiktok/profile/videos",
        headers=SC_HEADERS,
        params={"handle": handle, "limit": 30},
        timeout=30,
    )
    resp.raise_for_status()
    return [normalize_tiktok(v, handle) for v in resp.json().get("aweme_list", [])]


def fetch_tiktok_hashtag(hashtag: str) -> list:
    label, posts, cursor = f"#{hashtag}", [], None
    for _ in range(3):
        params = {"hashtag": hashtag, "limit": 30}
        if cursor is not None:
            params["cursor"] = cursor
        resp = requests.get(
            "https://api.scrapecreators.com/v1/tiktok/search/hashtag",
            headers=SC_HEADERS, params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        posts.extend(normalize_tiktok(v, label) for v in data.get("aweme_list", []))
        if not data.get("has_more"):
            break
        cursor = data.get("cursor")
    return posts


def fetch_instagram_account(handle: str) -> list:
    resp = requests.get(
        "https://api.scrapecreators.com/v2/instagram/user/posts",
        headers=SC_HEADERS,
        params={"handle": handle, "limit": 30},
        timeout=30,
    )
    resp.raise_for_status()
    posts = []
    for item in resp.json().get("items", []):
        cap_obj  = item.get("caption") or {}
        caption  = (cap_obj.get("text", "") if isinstance(cap_obj, dict) else str(cap_obj))[:300]
        views    = item.get("view_count") or item.get("play_count") or 0
        likes    = item.get("like_count", 0) or 0
        comments = item.get("comment_count", 0) or 0
        shares   = item.get("share_count", 0) or 0
        code     = item.get("code", "")
        url      = item.get("url") or (f"https://www.instagram.com/p/{code}/" if code else "")
        posts.append({
            "post_id":   str(item.get("pk", "")),
            "platform":  "Instagram",
            "account":   handle,
            "caption":   caption,
            "url":       url,
            "views":     views,
            "likes":     likes,
            "comments":  comments,
            "shares":    shares,
            "er":        calc_er(likes, comments, shares, views),
            "published": parse_unix(item.get("taken_at", 0)),
            "language":  detect_language(caption),
        })
    return posts


def fetch_instagram_hashtag(hashtag: str) -> list:
    all_posts, cursor = [], None
    for _ in range(3):
        params = {"hashtag": hashtag, "limit": 30}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            "https://api.scrapecreators.com/v1/instagram/search/hashtag",
            headers=SC_HEADERS, params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("posts", []):
            caption  = (item.get("caption") or "")[:300]
            views    = item.get("video_play_count") or item.get("video_view_count") or 0
            likes    = item.get("like_count", 0) or 0
            comments = item.get("comment_count", 0) or 0
            url      = item.get("url") or ""
            taken_at = item.get("taken_at", "")
            owner    = item.get("owner", {}).get("username") or f"#{hashtag}"
            all_posts.append({
                "post_id":   str(item.get("id", "")),
                "platform":  "Instagram",
                "account":   f"#{hashtag} (@{owner})",
                "caption":   caption,
                "url":       url,
                "views":     views,
                "likes":     likes,
                "comments":  comments,
                "shares":    0,
                "er":        calc_er(likes, comments, 0, views),
                "published": parse_iso(taken_at) if isinstance(taken_at, str) else parse_unix(taken_at),
                "language":  detect_language(caption),
            })
        cursor = data.get("cursor")
        if not cursor:
            break
    return all_posts


def fetch_tiktok_keyword(query: str) -> list:
    label, posts, cursor = f'"{query}"', [], None
    for _ in range(3):
        params = {"query": query, "limit": 30}
        if cursor is not None:
            params["cursor"] = cursor
        resp = requests.get(
            "https://api.scrapecreators.com/v1/tiktok/search/keyword",
            headers=SC_HEADERS, params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("search_item_list", []):
            aweme = item.get("aweme_info", {})
            if aweme:
                posts.append(normalize_tiktok(aweme, label))
        if not data.get("has_more"):
            break
        cursor = data.get("cursor")
    return posts


def fetch_youtube_keyword(query: str) -> list:
    if not YOUTUBE_API_KEY:
        return []
    r = requests.get(f"{YT_BASE}/search", params={
        "part": "id", "q": f"{query} #shorts",
        "type": "video", "videoDuration": "short",
        "maxResults": 50, "order": "viewCount",
        "key": YOUTUBE_API_KEY,
    }, timeout=30)
    r.raise_for_status()
    video_ids = [i["id"]["videoId"] for i in r.json().get("items", []) if i.get("id", {}).get("videoId")]
    if not video_ids:
        return []
    rv = requests.get(f"{YT_BASE}/videos", params={
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }, timeout=30)
    rv.raise_for_status()
    posts = []
    for item in rv.json().get("items", []):
        if not _yt_is_short(item.get("contentDetails", {}).get("duration", "")):
            continue
        snippet  = item.get("snippet", {})
        stats    = item.get("statistics", {})
        views    = int(stats.get("viewCount",    0) or 0)
        likes    = int(stats.get("likeCount",    0) or 0)
        comments = int(stats.get("commentCount", 0) or 0)
        pub_raw  = snippet.get("publishedAt", "")
        try:
            published = datetime.fromisoformat(pub_raw.replace("Z", "+00:00")).isoformat()
        except Exception:
            published = datetime.now(tz=timezone.utc).isoformat()
        caption = (snippet.get("title") or "")[:300]
        posts.append({
            "post_id":   item["id"],
            "platform":  "YouTube",
            "account":   f'"{query}"',
            "url":       f"https://www.youtube.com/shorts/{item['id']}",
            "views":     views,
            "likes":     likes,
            "comments":  comments,
            "shares":    0,
            "er":        round((likes + comments) / views * 100, 2) if views > 0 else 0.0,
            "published": published,
            "language":  detect_language(caption),
        })
    return posts


def _yt_is_short(duration: str) -> bool:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not m:
        return False
    h, mn, s = int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3) or 0)
    return h == 0 and mn == 0 and s <= 60


def fetch_youtube_hashtag(hashtag: str) -> list:
    if not YOUTUBE_API_KEY:
        return []
    r = requests.get(f"{YT_BASE}/search", params={
        "part": "id", "q": f"#{hashtag} #shorts",
        "type": "video", "videoDuration": "short",
        "maxResults": 50, "order": "viewCount",
        "key": YOUTUBE_API_KEY,
    }, timeout=30)
    r.raise_for_status()
    video_ids = [i["id"]["videoId"] for i in r.json().get("items", []) if i.get("id", {}).get("videoId")]
    if not video_ids:
        return []
    rv = requests.get(f"{YT_BASE}/videos", params={
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }, timeout=30)
    rv.raise_for_status()
    posts = []
    for item in rv.json().get("items", []):
        if not _yt_is_short(item.get("contentDetails", {}).get("duration", "")):
            continue
        snippet  = item.get("snippet", {})
        stats    = item.get("statistics", {})
        views    = int(stats.get("viewCount",    0) or 0)
        likes    = int(stats.get("likeCount",    0) or 0)
        comments = int(stats.get("commentCount", 0) or 0)
        pub_raw  = snippet.get("publishedAt", "")
        try:
            published = datetime.fromisoformat(pub_raw.replace("Z", "+00:00")).isoformat()
        except Exception:
            published = datetime.now(tz=timezone.utc).isoformat()
        caption = (snippet.get("title") or "")[:300]
        posts.append({
            "post_id":   item["id"],
            "platform":  "YouTube",
            "account":   f"#{hashtag}",
            "url":       f"https://www.youtube.com/shorts/{item['id']}",
            "views":     views,
            "likes":     likes,
            "comments":  comments,
            "shares":    0,
            "er":        round((likes + comments) / views * 100, 2) if views > 0 else 0.0,
            "published": published,
            "language":  detect_language(caption),
        })
    return posts


def meets_threshold(post: dict, min_views: int, min_er: float, max_age_days: int, languages: list) -> bool:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    try:
        published = datetime.fromisoformat(post["published"])
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except Exception:
        published = cutoff
    if post["views"] <= min_views:
        return False
    if post["er"] < min_er:
        return False
    if published < cutoff:
        return False
    if languages and "all" not in languages and post.get("language", "other") not in languages:
        return False
    return True


def run_campaign(campaign, existing_post_ids: set) -> list:
    platforms = json.loads(campaign.platforms or '["tiktok","instagram"]')
    hashtags  = json.loads(campaign.hashtags  or '[]')
    accounts  = json.loads(campaign.accounts  or '[]')
    keywords  = json.loads(getattr(campaign, 'keywords', None) or '[]')
    languages = json.loads(campaign.languages or '["all"]')

    all_posts = []

    if "tiktok" in platforms:
        for handle in accounts:
            try:
                all_posts.extend(fetch_tiktok_account(handle))
            except Exception as e:
                print(f"TikTok @{handle}: {e}")
        for tag in hashtags:
            try:
                all_posts.extend(fetch_tiktok_hashtag(tag))
            except Exception as e:
                print(f"TikTok #{tag}: {e}")
        for kw in keywords:
            try:
                all_posts.extend(fetch_tiktok_keyword(kw))
            except Exception as e:
                print(f"TikTok keyword '{kw}': {e}")

    if "instagram" in platforms:
        for handle in accounts:
            try:
                all_posts.extend(fetch_instagram_account(handle))
            except Exception as e:
                print(f"Instagram @{handle}: {e}")
        for tag in hashtags:
            try:
                all_posts.extend(fetch_instagram_hashtag(tag))
            except Exception as e:
                print(f"Instagram #{tag}: {e}")

    if "youtube" in platforms:
        for tag in hashtags:
            try:
                all_posts.extend(fetch_youtube_hashtag(tag))
            except Exception as e:
                print(f"YouTube #{tag}: {e}")
        for kw in keywords:
            try:
                all_posts.extend(fetch_youtube_keyword(kw))
            except Exception as e:
                print(f"YouTube keyword '{kw}': {e}")

    new_posts = []
    for post in all_posts:
        if post["post_id"] in existing_post_ids:
            continue
        if not meets_threshold(post, campaign.min_views, campaign.min_er, campaign.max_age_days, languages):
            continue
        new_posts.append(post)
        existing_post_ids.add(post["post_id"])

    return new_posts
