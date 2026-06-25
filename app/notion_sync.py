import os
from datetime import datetime, timezone
from notion_client import Client

NOTION_TOKEN  = os.getenv("NOTION_TOKEN", "")
NOTION_DB_ID  = "38491845-2151-80ff-923d-eb0dc676356b"


def push_post_to_notion(post: dict):
    if not NOTION_TOKEN:
        return
    notion = Client(auth=NOTION_TOKEN)
    name = (post.get("caption") or f"{post['platform']} {post['account']}")[:200]
    week_start = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties={
                "Name":       {"title":     [{"text": {"content": name}}]},
                "Platform":   {"select":    {"name": post["platform"]}},
                "Account":    {"rich_text": [{"text": {"content": post["account"]}}]},
                "URL":        {"url":        post["url"] or None},
                "Views":      {"number":    post["views"]},
                "Likes":      {"number":    post["likes"]},
                "Comments":   {"number":    post["comments"]},
                "Shares":     {"number":    post["shares"]},
                "ER%":        {"number":    round(post["er"] / 100, 4)},
                "Published":  {"date":      {"start": post["published"]}},
                "Post ID":    {"rich_text": [{"text": {"content": post["post_id"]}}]},
                "Week Added": {"date":      {"start": week_start}},
                "Language":   {"select":    {"name": post.get("language", "other")}},
            },
        )
    except Exception:
        pass
