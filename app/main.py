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


async def _fetch_metrics(client: httpx.AsyncClient, url: str, api_key: str) -> dict:
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
                return {
                    "status":    "Active",
                    "api_views": st.get("play_count", ""),
                    "api_date":  pub_date,
                    "likes":     st.get("digg_count", ""),
                    "comments":  st.get("comment_count", ""),
                    "shares":    st.get("share_count", ""),
                    "saves":     st.get("collect_count", ""),
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
                return {
                    "status":    "Active",
                    "api_views": media.get("video_play_count", media.get("video_view_count", "")),
                    "api_date":  pub_date,
                    "likes":     media.get("edge_media_preview_like", {}).get("count", ""),
                    "comments":  media.get("edge_media_preview_comment", {}).get("count", ""),
                    "shares":    "",
                    "saves":     "",
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
                return {
                    "status":    "Active",
                    "api_views": st.get("viewCount", ""),
                    "api_date":  pub_date,
                    "likes":     st.get("likeCount", ""),
                    "comments":  st.get("commentCount", ""),
                    "shares":    "",
                    "saves":     "",
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


@app.get("/api/balance")
def api_balance(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _sc_credits is None and SCRAPECREATORS_API_KEY:
        threading.Thread(target=_fetch_sc_credits_sync, daemon=True).start()
    return JSONResponse({"sc_credits": _sc_credits})


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


async def _fetch_comments_tiktok(client: httpx.AsyncClient, url: str, sc_key: str) -> list[dict]:
    r = await client.get(
        "https://api.scrapecreators.com/v1/tiktok/video/comments",
        params={"url": url}, headers={"x-api-key": sc_key}, timeout=20,
    )
    if r.status_code != 200:
        return []
    out = []
    for c in r.json().get("comments", []):
        ts = c.get("create_time", 0)
        date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y") if ts else ""
        out.append({
            "post_url":  url,
            "platform":  "TikTok",
            "author":    "@" + c.get("user", {}).get("unique_id", ""),
            "comment":   c.get("text", ""),
            "likes":     c.get("digg_count", 0),
            "date":      date,
            "is_reply":  bool(c.get("reply_id")),
        })
    return out


async def _fetch_comments_instagram(client: httpx.AsyncClient, url: str, sc_key: str) -> list[dict]:
    r = await client.get(
        "https://api.scrapecreators.com/v2/instagram/post/comments",
        params={"url": url}, headers={"x-api-key": sc_key}, timeout=20,
    )
    if r.status_code != 200:
        return []
    out = []
    for c in r.json().get("comments", []):
        ts = c.get("created_at", 0)
        date = _dt.utcfromtimestamp(ts).strftime("%d.%m.%Y") if ts else ""
        out.append({
            "post_url":  url,
            "platform":  "Instagram",
            "author":    "@" + c.get("user", {}).get("username", ""),
            "comment":   c.get("text", ""),
            "likes":     c.get("like_count", 0),
            "date":      date,
            "is_reply":  bool(c.get("replied_to_author")),
        })
    return out


async def _fetch_comments_youtube(client: httpx.AsyncClient, url: str, sc_key: str) -> list[dict]:
    r = await client.get(
        "https://api.scrapecreators.com/v1/youtube/video/comments",
        params={"url": url}, headers={"x-api-key": sc_key}, timeout=20,
    )
    if r.status_code != 200:
        return []
    out = []
    for c in r.json().get("comments", []):
        date = ""
        try:
            raw = c.get("publishedTime", "")
            if raw:
                date = _dt.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d.%m.%Y")
        except Exception:
            pass
        out.append({
            "post_url":  url,
            "platform":  "YouTube",
            "author":    c.get("author", {}).get("name", ""),
            "comment":   c.get("content", ""),
            "likes":     c.get("engagement", {}).get("likes", 0),
            "date":      date,
            "is_reply":  c.get("replyLevel", 0) > 0,
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
