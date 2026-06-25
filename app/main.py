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

from app.auth import APP_PASSWORD, check_auth, create_session_token
from app.database import SessionLocal, init_db
from app.models import Campaign, Post, Run
from app.notion_sync import push_post_to_notion
from app.parser import run_campaign
from app.scheduler import start_scheduler
from app.telegram import send_message

app = FastAPI()

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR.parent / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.on_event("startup")
def startup():
    init_db()
    start_scheduler()


# ── Авторизация ──────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if password != APP_PASSWORD:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"})
    token    = create_session_token()
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session", token, max_age=86400 * 30, httponly=True)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# ── Список кампаний ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def campaigns_list(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db   = SessionLocal()
    rows = db.query(Campaign).order_by(Campaign.created_at.desc()).all()
    data = [
        {"campaign": c, "posts_count": db.query(Post).filter(Post.campaign_id == c.id).count()}
        for c in rows
    ]
    db.close()
    return templates.TemplateResponse("campaigns.html", {"request": request, "campaigns_data": data})


# ── Создать кампанию ─────────────────────────────────────────────────────────

@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("campaign_new.html", {"request": request})


@app.post("/campaigns/new")
async def campaign_create(
    request: Request,
    name:         str       = Form(...),
    platforms:    list[str] = Form(...),
    hashtags:     str       = Form(""),
    accounts:     str       = Form(""),
    min_views:    int       = Form(300000),
    min_er:       float     = Form(2.0),
    max_age_days: int       = Form(180),
    languages:    list[str] = Form([]),
):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)

    hashtags_list = [h.strip().lstrip("#") for h in hashtags.split(",") if h.strip()]
    accounts_list = [a.strip().lstrip("@") for a in accounts.split(",") if a.strip()]
    langs         = languages if languages else ["all"]

    db  = SessionLocal()
    now = datetime.now(tz=timezone.utc)
    c   = Campaign(
        name         = name,
        platforms    = json.dumps(platforms),
        hashtags     = json.dumps(hashtags_list),
        accounts     = json.dumps(accounts_list),
        min_views    = min_views,
        min_er       = min_er,
        max_age_days = max_age_days,
        languages    = json.dumps(langs),
        status       = "active",
        created_at   = now,
        next_run_at  = now,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    cid = c.id
    db.close()

    return RedirectResponse(f"/campaigns/{cid}", status_code=302)


# ── Детали кампании ──────────────────────────────────────────────────────────

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
    ctx   = {
        "request":   request,
        "campaign":  c,
        "posts":     posts,
        "runs":      runs,
        "hashtags":  json.loads(c.hashtags  or "[]"),
        "accounts":  json.loads(c.accounts  or "[]"),
        "platforms": json.loads(c.platforms or "[]"),
        "languages": json.loads(c.languages or '["all"]'),
    }
    db.close()
    return templates.TemplateResponse("campaign_detail.html", ctx)


# ── Запустить вручную (в фоне, чтобы не блокировать браузер) ─────────────────

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
        campaign.next_run_at = now + timedelta(days=3)
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


# ── Остановить / возобновить ─────────────────────────────────────────────────

@app.post("/campaigns/{campaign_id}/toggle")
def campaign_toggle(request: Request, campaign_id: int):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    c  = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if c:
        c.status = "stopped" if c.status == "active" else "active"
        if c.status == "active":
            c.next_run_at = datetime.now(tz=timezone.utc)
        db.commit()
    db.close()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


# ── Удалить кампанию ─────────────────────────────────────────────────────────

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
