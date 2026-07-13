import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv(Path(__file__).parent.parent / ".env")

import httpx
from fastapi.responses import JSONResponse
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

    db.close()
    return templates.TemplateResponse(request=request, name="overview.html", context={
        "stats": stats, "posts": posts, "days": days,
        "all_campaigns": all_campaigns, "campaign_filter": campaign,
        "chart_labels": all_weeks,
        "chart_tiktok": [tt_by_week[w] for w in all_weeks],
        "chart_instagram": [ig_by_week[w] for w in all_weeks],
        "chart_youtube": [yt_by_week[w] for w in all_weeks],
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
