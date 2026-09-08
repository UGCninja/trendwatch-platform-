"""
Accounts — аудит аккаунта: профиль + 50 постов + комментарии + лайкеры
"""
import csv
import io
import json
import threading
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.requests import Request
from starlette.responses import JSONResponse

router = APIRouter()


# ── helpers ──────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:    return "TikTok"
    if "instagram.com" in u: return "Instagram"
    if "youtube.com" in u or "youtu.be" in u: return "YouTube"
    if "x.com" in u or "twitter.com" in u: return "X"
    return ""


def extract_handle(url: str, platform: str) -> str:
    import re
    u = url.strip().rstrip("/")
    pats = {
        "TikTok":    r"tiktok\.com/@([^/?]+)",
        "Instagram": r"instagram\.com/([^/?]+)",
        "YouTube":   r"youtube\.com/(?:@|channel/|c/)([^/?]+)",
        "X":         r"(?:x|twitter)\.com/([^/?]+)",
    }
    m = re.search(pats.get(platform, ""), u)
    h = m.group(1) if m else ""
    if platform == "Instagram" and h in ("p", "reel", "tv", "stories", ""):
        return ""
    return h


async def fetch_profile(handle: str, platform: str, sc_key: str, yt_key: str) -> dict:
    result = {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            if platform == "TikTok":
                r = await c.get("https://api.scrapecreators.com/v1/tiktok/profile",
                                params={"username": handle}, headers={"x-api-key": sc_key})
                if r.status_code == 200:
                    d = r.json()
                    u = d.get("userInfo", {}).get("user", d.get("user", d))
                    s = d.get("userInfo", {}).get("stats", d.get("stats", {}))
                    result = {
                        "username": u.get("uniqueId", handle),
                        "nickname": u.get("nickname", ""),
                        "bio":      u.get("signature", ""),
                        "verified": bool(u.get("verified")),
                        "avatar":   u.get("avatarLarger") or u.get("avatarMedium", ""),
                        "followers": s.get("followerCount", 0),
                        "following": s.get("followingCount", 0),
                        "posts":    s.get("videoCount", 0),
                        "likes_total": s.get("heartCount", s.get("heart", 0)),
                        "region":   u.get("region", ""),
                    }
            elif platform == "Instagram":
                r = await c.get("https://api.scrapecreators.com/v1/instagram/user",
                                params={"username": handle}, headers={"x-api-key": sc_key})
                if r.status_code == 200:
                    d = r.json()
                    u = (d.get("data") or {}).get("user", d)
                    result = {
                        "username":  u.get("username", handle),
                        "nickname":  u.get("full_name", ""),
                        "bio":       u.get("biography", ""),
                        "verified":  bool(u.get("is_verified")),
                        "avatar":    u.get("profile_pic_url_hd", u.get("profile_pic_url", "")),
                        "followers": (u.get("edge_followed_by") or {}).get("count", u.get("follower_count", 0)),
                        "following": (u.get("edge_follow") or {}).get("count", u.get("following_count", 0)),
                        "posts":     (u.get("edge_owner_to_timeline_media") or {}).get("count", u.get("media_count", 0)),
                        "is_business": bool(u.get("is_business_account")),
                        "category":  u.get("business_category_name", ""),
                    }
            elif platform == "YouTube" and yt_key:
                r = await c.get("https://www.googleapis.com/youtube/v3/channels",
                                params={"part": "snippet,statistics", "forHandle": handle, "key": yt_key})
                if r.status_code == 200:
                    items = r.json().get("items") or []
                    if items:
                        ch = items[0]; sn = ch.get("snippet", {}); st = ch.get("statistics", {})
                        result = {
                            "username":   sn.get("title", handle),
                            "nickname":   sn.get("title", ""),
                            "bio":        sn.get("description", "")[:200],
                            "avatar":     (sn.get("thumbnails", {}).get("high") or {}).get("url", ""),
                            "followers":  int(st.get("subscriberCount", 0)),
                            "posts":      int(st.get("videoCount", 0)),
                            "views_total": int(st.get("viewCount", 0)),
                            "country":    sn.get("country", ""),
                            "created":    sn.get("publishedAt", "")[:10],
                        }
            elif platform == "X":
                r = await c.get("https://api.scrapecreators.com/v1/twitter/user",
                                params={"username": handle}, headers={"x-api-key": sc_key})
                if r.status_code == 200:
                    d = r.json(); u = d.get("data", d); lg = u.get("legacy", u)
                    result = {
                        "username":  lg.get("screen_name", handle),
                        "nickname":  lg.get("name", ""),
                        "bio":       lg.get("description", ""),
                        "verified":  bool(u.get("is_blue_verified") or lg.get("verified")),
                        "avatar":    lg.get("profile_image_url_https", "").replace("_normal", ""),
                        "followers": lg.get("followers_count", 0),
                        "following": lg.get("friends_count", 0),
                        "posts":     lg.get("statuses_count", 0),
                        "created":   lg.get("created_at", "")[:10],
                    }
    except Exception:
        pass
    return result


async def fetch_posts(handle: str, platform: str, sc_key: str, yt_key: str, limit: int = 50) -> list[str]:
    urls: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if platform == "TikTok":
                r = await c.get("https://api.scrapecreators.com/v2/tiktok/user/posts",
                                params={"username": handle, "limit": limit}, headers={"x-api-key": sc_key})
                if r.status_code == 200:
                    data = r.json()
                    items = data if isinstance(data, list) else (data.get("data") or data.get("posts") or data.get("items") or [])
                    for item in items[:limit]:
                        vid = item.get("aweme_id") or item.get("id")
                        url = item.get("webVideoUrl") or item.get("url") or (f"https://www.tiktok.com/@{handle}/video/{vid}" if vid else "")
                        if url: urls.append(url)
            elif platform == "Instagram":
                r = await c.get("https://api.scrapecreators.com/v2/instagram/user/posts",
                                params={"username": handle}, headers={"x-api-key": sc_key})
                if r.status_code == 200:
                    data = r.json()
                    items = data if isinstance(data, list) else (data.get("data") or data.get("posts") or data.get("items") or [])
                    for item in items[:limit]:
                        sc = item.get("shortCode") or item.get("shortcode") or item.get("id")
                        url = item.get("url") or item.get("link") or (f"https://www.instagram.com/p/{sc}/" if sc else "")
                        if url: urls.append(url)
            elif platform == "YouTube" and yt_key:
                r2 = await c.get("https://www.googleapis.com/youtube/v3/channels",
                                 params={"part": "id", "forHandle": handle, "key": yt_key})
                channel_id = ""
                if r2.status_code == 200:
                    it = r2.json().get("items") or []
                    channel_id = it[0]["id"] if it else ""
                if channel_id:
                    r3 = await c.get("https://www.googleapis.com/youtube/v3/search",
                                     params={"part": "id", "channelId": channel_id, "type": "video",
                                             "maxResults": limit, "order": "date", "key": yt_key})
                    if r3.status_code == 200:
                        for item in r3.json().get("items") or []:
                            vid = item.get("id", {}).get("videoId")
                            if vid: urls.append(f"https://www.youtube.com/watch?v={vid}")
            elif platform == "X":
                r = await c.get("https://api.scrapecreators.com/v1/twitter/user/tweets",
                                params={"username": handle, "count": limit}, headers={"x-api-key": sc_key})
                if r.status_code == 200:
                    data = r.json()
                    items = data if isinstance(data, list) else (data.get("tweets") or data.get("data") or [])
                    for item in items[:limit]:
                        tid = item.get("id") or item.get("id_str") or item.get("rest_id")
                        if tid: urls.append(f"https://x.com/{handle}/status/{tid}")
    except Exception:
        pass
    return urls


# ── routes ───────────────────────────────────────────────────

@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    from app.main import check_auth, templates, SessionLocal, _AccountProject
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    aps = db.query(_AccountProject).order_by(_AccountProject.created_at.desc()).all()
    result = [{"ap": ap, "profile": json.loads(ap.profile_data) if ap.profile_data else {}} for ap in aps]
    db.close()
    return templates.TemplateResponse(request=request, name="account_projects.html",
                                      context={"active_page": "accounts", "accounts": result})


@router.post("/accounts")
async def accounts_create(request: Request, name: str = Form(...), account_url: str = Form(...)):
    from app.main import check_auth, SessionLocal, _AccountProject, CommentProject
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    platform = detect_platform(account_url)
    db = SessionLocal()
    cp = CommentProject(name=f"[Account] {name}")
    db.add(cp); db.flush()
    ap = _AccountProject(name=name, account_url=account_url.strip(), platform=platform, comment_project_id=cp.id)
    db.add(ap); db.commit()
    aid = ap.id
    db.close()
    return RedirectResponse(f"/accounts/{aid}", status_code=302)


@router.get("/accounts/{aid}", response_class=HTMLResponse)
async def account_detail(request: Request, aid: int):
    from app.main import check_auth, templates, SessionLocal, _AccountProject, CommentSource, _StoredComment, _LANG_NAMES, _LANG_TO_COUNTRY
    from app.models import StoredLiker as _StoredLiker
    from sqlalchemy import func as _func
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    if not ap:
        db.close()
        return RedirectResponse("/accounts", status_code=302)
    pid = ap.comment_project_id
    profile = json.loads(ap.profile_data) if ap.profile_data else {}
    sources = db.query(CommentSource).filter(CommentSource.project_id == pid).all()
    src_ids = [s.id for s in sources]

    total_comments = db.query(_StoredComment).filter(_StoredComment.source_id.in_(src_ids)).count() if src_ids else 0
    total_likers = db.query(_StoredLiker.username).filter(_StoredLiker.source_id.in_(src_ids)).distinct().count() if src_ids else 0

    top_commenters, top_likers, lang_stats, region_stats, liker_per_source = [], [], [], [], {}
    if src_ids:
        rows = (db.query(_StoredComment.author, _func.count(_StoredComment.id).label("c"), _func.max(_StoredComment.platform).label("p"))
                .filter(_StoredComment.source_id.in_(src_ids), _StoredComment.author != None, _StoredComment.author != "")
                .group_by(_StoredComment.author).order_by(_func.count(_StoredComment.id).desc()).limit(100).all())
        top_commenters = [(a, c, p) for a, c, p in rows]

        lk = (db.query(_StoredLiker.username, _func.count(_func.distinct(_StoredLiker.source_id)).label("p"))
              .filter(_StoredLiker.source_id.in_(src_ids))
              .group_by(_StoredLiker.username).order_by(_func.count(_func.distinct(_StoredLiker.source_id)).desc()).limit(100).all())
        top_likers = [(u, c) for u, c in lk]

        def _lbl(code):
            n = _LANG_NAMES.get(code) or code
            ct = _LANG_TO_COUNTRY.get(code)
            return f"{n} · {ct}" if ct else n
        for k, v in (db.query(_StoredComment.language, _func.count(_StoredComment.id))
                     .filter(_StoredComment.source_id.in_(src_ids), _StoredComment.language != None, _StoredComment.language != "")
                     .group_by(_StoredComment.language).order_by(_func.count(_StoredComment.id).desc()).limit(20).all()):
            lang_stats.append((_lbl(k), k, v))
        region_stats = [(r, c) for r, c in (db.query(_StoredComment.user_region, _func.count(_StoredComment.id))
            .filter(_StoredComment.source_id.in_(src_ids), _StoredComment.user_region != None, _StoredComment.user_region != "")
            .group_by(_StoredComment.user_region).order_by(_func.count(_StoredComment.id).desc()).limit(20).all())]
        for sid, cnt in db.query(_StoredLiker.source_id, _func.count(_StoredLiker.id)).filter(_StoredLiker.source_id.in_(src_ids)).group_by(_StoredLiker.source_id).all():
            liker_per_source[sid] = cnt

    db.close()
    return templates.TemplateResponse(request=request, name="account_project_detail.html", context={
        "active_page": "accounts", "ap": ap, "pid": pid, "profile": profile,
        "sources": sources, "total_comments": total_comments, "total_likers": total_likers,
        "top_commenters": top_commenters, "top_likers": top_likers,
        "lang_stats": lang_stats, "region_stats": region_stats,
        "liker_region_stats": [], "lang_region_stats_comments": [],
        "liker_per_source": liker_per_source, "providers": [],
    })


@router.post("/accounts/{aid}/fetch-posts")
async def account_fetch_posts(request: Request, aid: int):
    from app.main import (check_auth, SessionLocal, _AccountProject, CommentSource,
                          SCRAPECREATORS_API_KEY, YOUTUBE_API_KEY, _detect_platform)
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    if not ap:
        db.close()
        return RedirectResponse("/accounts", status_code=302)
    handle = extract_handle(ap.account_url, ap.platform or "")

    # Сохраняем профиль отдельным коммитом чтобы не потерять при ошибках URL
    profile = await fetch_profile(handle, ap.platform or "", SCRAPECREATORS_API_KEY, YOUTUBE_API_KEY)
    if profile:
        ap.profile_data = json.dumps(profile)
        db.commit()

    urls = await fetch_posts(handle, ap.platform or "", SCRAPECREATORS_API_KEY, YOUTUBE_API_KEY, limit=50)

    # Получаем уже существующие URL чтобы не делать дубли
    existing = {s.url for s in db.query(CommentSource.url).filter(
        CommentSource.project_id == ap.comment_project_id).all()}

    for url in urls:
        if url in existing:
            continue
        try:
            src = CommentSource(project_id=ap.comment_project_id, url=url,
                                platform=ap.platform or _detect_platform(url), creator=handle)
            db.add(src)
            db.flush()
            existing.add(url)
        except Exception:
            db.rollback()

    ap.last_fetched_at = datetime.utcnow()
    ap.posts_count = db.query(CommentSource).filter(
        CommentSource.project_id == ap.comment_project_id).count()
    db.commit()
    db.close()
    return RedirectResponse(f"/accounts/{aid}", status_code=302)


@router.post("/accounts/{aid}/run")
async def account_run(request: Request, aid: int):
    from app.main import (check_auth, SessionLocal, _AccountProject, _comments_tasks,
                          _run_project_comments_task, SCRAPECREATORS_API_KEY, APIFY_TOKEN)
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    pid = ap.comment_project_id if ap else None
    db.close()
    if not pid:
        return RedirectResponse("/accounts", status_code=302)
    task_id = str(uuid.uuid4())
    _comments_tasks[task_id] = {"status": "queued", "done": 0, "total": 0, "ts": time.time(), "project_id": pid}
    threading.Thread(target=_run_project_comments_task, args=[task_id, pid, SCRAPECREATORS_API_KEY, APIFY_TOKEN], daemon=True).start()
    return RedirectResponse(f"/accounts/{aid}", status_code=302)


@router.post("/accounts/{aid}/cancel")
async def account_cancel(request: Request, aid: int):
    from app.main import check_auth, SessionLocal, _AccountProject, _comments_tasks
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    pid = ap.comment_project_id if ap else None
    db.close()
    if pid:
        for task in _comments_tasks.values():
            if task.get("project_id") == pid and task.get("status") not in ("done", "cancelled", "error"):
                task["status"] = "cancelled"
    return RedirectResponse(f"/accounts/{aid}", status_code=302)


@router.post("/accounts/{aid}/collect-likers")
async def account_collect_likers(request: Request, aid: int):
    from app.main import (check_auth, SessionLocal, _AccountProject,
                          _comments_tasks, _run_collect_likers_task, APIFY_TOKEN)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not APIFY_TOKEN:
        return JSONResponse({"error": "no apify token"}, status_code=400)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    pid = ap.comment_project_id if ap else None
    db.close()
    if not pid:
        return JSONResponse({"error": "not found"}, status_code=404)
    task_id = str(uuid.uuid4())
    _comments_tasks[task_id] = {"status": "queued", "done": 0, "total": 0, "ts": time.time(), "project_id": pid}
    threading.Thread(target=_run_collect_likers_task, args=[task_id, pid, APIFY_TOKEN], daemon=True).start()
    return JSONResponse({"ok": True, "task_id": task_id})


@router.post("/accounts/{aid}/recalc-languages")
async def account_recalc_languages(request: Request, aid: int):
    from app.main import check_auth, SessionLocal, _AccountProject
    from app.main import recalc_languages
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    pid = ap.comment_project_id if ap else None
    db.close()
    if not pid:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await recalc_languages(request, pid)


@router.post("/accounts/{aid}/export-sheets")
async def account_export_sheets(request: Request, aid: int):
    from app.main import check_auth, SessionLocal, _AccountProject
    from app.main import comment_project_export_sheets
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    pid = ap.comment_project_id if ap else None
    db.close()
    if not pid:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await comment_project_export_sheets(request, pid)


@router.get("/accounts/{aid}/download")
def account_download(request: Request, aid: int):
    from app.main import check_auth, SessionLocal, _AccountProject, CommentSource, _StoredComment
    from urllib.parse import quote as _q
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    if not ap:
        db.close()
        return RedirectResponse("/accounts", status_code=302)
    sources = db.query(CommentSource).filter(CommentSource.project_id == ap.comment_project_id).all()
    comments = db.query(_StoredComment).filter(_StoredComment.source_id.in_([s.id for s in sources])).all() if sources else []
    db.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["post_url", "platform", "author", "comment", "likes", "date", "is_reply", "language", "user_region"])
    for cm in comments:
        src = next((s for s in sources if s.id == cm.source_id), None)
        w.writerow([src.url if src else "", cm.platform, cm.author, cm.text, cm.likes, cm.date, cm.is_reply, cm.language, cm.user_region])
    fname = f"{ap.name}_comments.csv"
    return StreamingResponse(io.BytesIO(out.getvalue().encode("utf-8-sig")), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_q(fname.encode('utf-8'))}"})


@router.get("/api/accounts/{aid}/task-status")
def account_task_status(request: Request, aid: int):
    from app.main import check_auth, SessionLocal, _AccountProject, _comments_tasks
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    pid = ap.comment_project_id if ap else None
    db.close()
    if not pid:
        return JSONResponse({"status": "idle"})
    for _, task in sorted(_comments_tasks.items(), key=lambda x: -x[1].get("ts", 0)):
        if task.get("project_id") == pid:
            return JSONResponse(task)
    return JSONResponse({"status": "idle"})


@router.post("/accounts/{aid}/delete")
async def account_delete(request: Request, aid: int):
    from app.main import check_auth, SessionLocal, _AccountProject, CommentProject, CommentSource, _StoredComment
    from app.models import StoredLiker as _StoredLiker
    if not check_auth(request):
        return RedirectResponse("/login", status_code=302)
    db = SessionLocal()
    ap = db.query(_AccountProject).filter(_AccountProject.id == aid).first()
    if ap:
        pid = ap.comment_project_id
        src_ids = [s.id for s in db.query(CommentSource).filter(CommentSource.project_id == pid).all()]
        if src_ids:
            db.query(_StoredLiker).filter(_StoredLiker.source_id.in_(src_ids)).delete(synchronize_session=False)
            db.query(_StoredComment).filter(_StoredComment.source_id.in_(src_ids)).delete(synchronize_session=False)
            db.query(CommentSource).filter(CommentSource.project_id == pid).delete(synchronize_session=False)
        db.query(CommentProject).filter(CommentProject.id == pid).delete(synchronize_session=False)
        db.query(_AccountProject).filter(_AccountProject.id == aid).delete(synchronize_session=False)
        db.commit()
    db.close()
    return RedirectResponse("/accounts", status_code=302)


# /comments/projects/{pid}/sources/{sid}/comments already defined in main.py
