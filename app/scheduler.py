from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta, timezone

scheduler = BackgroundScheduler(timezone="UTC")


def _execute_campaign(campaign_id: int):
    from app.database import SessionLocal
    from app.models import Campaign, Post, Run
    from app.parser import run_campaign
    from app.notion_sync import push_post_to_notion
    from app.telegram import send_message

    db  = SessionLocal()
    now = datetime.now(tz=timezone.utc)
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign or campaign.status != "active":
            return

        run = Run(campaign_id=campaign_id, started_at=now)
        db.add(run)
        db.commit()

        existing_ids = {
            p.post_id for p in db.query(Post).filter(Post.campaign_id == campaign_id).all()
        }

        new_posts = run_campaign(campaign, existing_ids)

        for post_data in new_posts:
            db.add(Post(
                campaign_id=campaign_id,
                post_id=post_data["post_id"],
                platform=post_data["platform"],
                account=post_data["account"],
                url=post_data["url"],
                views=post_data["views"],
                likes=post_data["likes"],
                comments=post_data["comments"],
                shares=post_data["shares"],
                er=post_data["er"],
                published=post_data["published"],
                language=post_data["language"],
            ))
            push_post_to_notion(post_data)

        run.finished_at  = datetime.now(tz=timezone.utc)
        run.posts_added  = len(new_posts)
        run.status       = "done"
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


def _check_and_run():
    from app.database import SessionLocal
    from app.models import Campaign

    db  = SessionLocal()
    now = datetime.now(tz=timezone.utc)
    try:
        due = db.query(Campaign).filter(
            Campaign.status     == "active",
            Campaign.next_run_at <= now,
        ).all()
        ids = [c.id for c in due]
    finally:
        db.close()

    for campaign_id in ids:
        scheduler.add_job(_execute_campaign, args=[campaign_id], id=f"run_{campaign_id}_{now.timestamp()}")


def start_scheduler():
    scheduler.add_job(_check_and_run, "interval", hours=1, id="check_campaigns")
    scheduler.start()
