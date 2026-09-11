"""
TRENDWATCH PLATFORM — ТЕХНИЧЕСКОЕ ОПИСАНИЕ
Обновляется при каждом изменении платформы.
Последнее обновление: 2026-09-11
"""

DOCS = {
    "version": "2026-09-11",
    "sections": [
        {
            "id": "campaigns",
            "title": "Campaigns",
            "icon": "📋",
            "description": "Автоматический сбор постов по хэштегам, аккаунтам и ключевым словам.",
            "api": "ScrapeCreators + YouTube Data API (бесплатно)",
            "cost": "SC кредиты (~1-2 кредита на запрос)",
            "schedule": "Каждые N часов/дней/недель — настраивается на странице кампании",
            "cache": "Нет",
            "platforms": {
                "TikTok": {
                    "по аккаунту": "SC GET /v3/tiktok/profile/videos",
                    "по хэштегу": "SC GET /v1/tiktok/search/hashtag",
                    "по ключевому слову": "SC GET /v1/tiktok/search/keyword",
                    "поля": "aweme_id, create_time, play_count, digg_count, comment_count, share_count, follower_count, thumbnail_url, desc, language"
                },
                "Instagram": {
                    "по аккаунту": "SC GET /v2/instagram/user/posts",
                    "по хэштегу": "SC GET /v1/instagram/search/hashtag",
                    "поля": "url, code, like_count, comment_count, taken_at, thumbnail_url"
                },
                "YouTube": {
                    "по ключевому слову": "YouTube Data API GET /v3/search → /v3/videos",
                    "поля": "viewCount, likeCount, commentCount, publishedAt, title",
                    "cost": "бесплатно (10 000 units/день)"
                }
            },
            "filters": "min_views, min_er (%), max_age_days, languages",
            "saved_fields": "post_id, platform, url, views, likes, comments, shares, er, published, language, thumbnail_url"
        },
        {
            "id": "enrich",
            "title": "Enrich",
            "icon": "⚡",
            "description": "Обогащение CSV-файла подрядчика актуальными метриками.",
            "api": "ScrapeCreators",
            "cost": "~1-2 SC кредита на URL",
            "cache": "30 дней (история в памяти)",
            "input": "CSV с URL постов",
            "output_columns": "URL | Platform | Type | Author | Followers | Views | Likes | Comments | Shares | Saves | Date | Status",
            "platforms": {
                "TikTok": "SC GET /v2/tiktok/video → statistics{play_count, digg_count, comment_count, share_count, collect_count}, author{unique_id, follower_count}",
                "Instagram": "SC GET /v1/instagram/post → xdt_shortcode_media{video_play_count, edge_media_preview_like, edge_media_preview_comment, __typename}",
                "X/Twitter": "SC GET /v1/twitter/tweet → legacy{favorite_count, reply_count, retweet_count, bookmark_count}, views{count}",
                "YouTube": "YouTube Data API GET /v3/videos → statistics{viewCount, likeCount, commentCount} (бесплатно)"
            },
            "type_field": "Только Instagram: Video/Carousel/Image из __typename",
            "notes": "Просмотры для Instagram image/carousel недоступны (ограничение платформы)"
        },
        {
            "id": "comments_onetime",
            "title": "Comments (One-time)",
            "icon": "💬",
            "description": "Разовый сбор комментариев из CSV. Без сохранения в базу.",
            "modes": {
                "Comments Only": "CSV с текстами комментариев",
                "Comments + Audit": "HTML-отчёт с анализом тональности через Claude AI (claude-sonnet-4-6)"
            },
            "limit": "200 комментариев на пост",
            "cache": "30 дней (история задач в памяти)",
            "platforms": {
                "TikTok": {
                    "primary": "Apify clockworks~tiktok-scraper",
                    "input": "{postURLs, commentsPerPost: 200, scrapeComments: true, resultsType: 'comments'}",
                    "fallback": "SC GET /v1/tiktok/video/comments",
                    "fields": "id, authorMeta{name, region}, text, diggCount, createTimeISO, isReply"
                },
                "Instagram": {
                    "primary": "Apify apify~instagram-comment-scraper",
                    "input": "{directUrls, resultsLimit: 200}",
                    "fallback": "SC GET /v2/instagram/post/comments",
                    "fields": "id, ownerUsername, text, likesCount, timestamp"
                },
                "YouTube": {
                    "primary": "SC GET /v1/youtube/video/comments (только SC, без Apify)",
                    "fields": "id, author{name}, content, engagement{likes}, publishedTime"
                },
                "X/Twitter": {
                    "primary": "Apify scraper_one~x-post-replies-scraper",
                    "input": "{postUrls, maxReplies: 200}",
                    "fallback": "нет (только Apify)"
                }
            },
            "filtering": "Описания постов (caption автора) фильтруются — автор URL = автор комментария → пропуск"
        },
        {
            "id": "projects",
            "title": "Projects",
            "icon": "📁",
            "description": "Постоянный мониторинг с накоплением в базе. Несколько URL на проект.",
            "api": "Apify (primary) + ScrapeCreators (fallback)",
            "cache_rules": {
                "Комментарии": "36 часов — пост пропускается если last_fetched_at < 36ч назад",
                "Метрики": "7 дней — не обновляются если metrics_updated_at < 7д назад",
                "Лайкеры": "Только для постов с likers_count = 0 (один раз)"
            },
            "collection_flow": [
                "1. Сбор комментариев (5 постов параллельно, Apify → SC fallback)",
                "2. Сбор метрик (10 постов параллельно, SC)",
                "3. Лайкеры — отдельная кнопка ♥ Collect Likers (Instagram only, Apify)"
            ],
            "post_statuses": {
                "active": "норма",
                "deleted": "пост удалён с платформы",
                "unavailable": "приватный/заблокирован",
                "comments_disabled": "автор закрыл комментарии"
            },
            "likers": {
                "actor": "Apify datadoping~instagram-likes-scraper",
                "input": "{posts: [url], max_count: 100}",
                "cost": "$0.001311 на пользователя",
                "trigger": "Только вручную кнопкой ♥, только посты с likers_count = 0"
            },
            "metrics_er": "(likes + comments) / views × 100, fallback: (likes + comments) / followers × 100 (для Instagram image/carousel)"
        },
        {
            "id": "accounts",
            "title": "Accounts",
            "icon": "👤",
            "description": "Аудит одного аккаунта: профиль + последние 50 постов + комментарии + лайкеры.",
            "api": "ScrapeCreators + Apify",
            "limit": "50 постов на аккаунт",
            "full_audit_steps": [
                "1. Fetch Profile — SC /v1/{platform}/profile → сохраняем JSON (username, followers, bio, verified, avatar)",
                "2. Fetch Posts — SC /v2/{platform}/user/posts (пагинация до 50) → сохраняем как CommentSource",
                "3. Collect Comments — Apify → SC fallback, 200/пост, force_metrics=True (кэш 7д обходится)",
                "4. Collect Likers — Apify datadoping (Instagram only), только для likers_count = 0"
            ],
            "profile_endpoints": {
                "TikTok": "SC GET /v1/tiktok/profile → username, followers, following, posts, likes_total, region, verified, avatar",
                "Instagram": "SC GET /v1/instagram/user (username param, fallback: handle param) → username, followers, following, posts, bio, verified, is_business",
                "YouTube": "YouTube Data API GET /v3/channels (forHandle) → title, subscribers, videos, views_total, country, created_at",
                "X/Twitter": "SC GET /v1/twitter/user → screen_name, followers, following, tweets, bio, is_blue_verified"
            },
            "posts_endpoints": {
                "TikTok": "SC GET /v2/tiktok/user/posts",
                "Instagram": "SC GET /v2/instagram/user/posts (handle param, next_max_id pagination)",
                "YouTube": "YouTube Data API GET /v3/search → /v3/videos",
                "X/Twitter": "SC GET /v1/twitter/user/tweets"
            },
            "notes": "force_metrics=True — метрики обновляются при каждом Full Audit без 7-дневного кэша"
        }
    ],
    "cost_summary": {
        "ScrapeCreators": "Кредиты (отображаются в шапке ✦ N SC). ~1-2 кредита на запрос",
        "Apify_TikTok_comments": "clockworks~tiktok-scraper: ~$0.003-0.005 на комментарий",
        "Apify_Instagram_comments": "apify~instagram-comment-scraper: $2.10/1000 комментариев",
        "Apify_X_replies": "scraper_one~x-post-replies-scraper: $0.25/1000 элементов",
        "Apify_Instagram_likers": "datadoping~instagram-likes-scraper: $1.30/1000 пользователей, MAX 100 на пост",
        "YouTube_API": "бесплатно (10 000 units/день)"
    },
    "apify_actors": {
        "clockworks~tiktok-scraper": "Комментарии TikTok + authorRegion",
        "apify~instagram-comment-scraper": "Комментарии Instagram",
        "scraper_one~x-post-replies-scraper": "Реплаи X/Twitter",
        "datadoping~instagram-likes-scraper": "Лайкеры Instagram (max 100/пост)"
    },
    "sc_endpoints_used": {
        "/v1/tiktok/profile": "Профиль TikTok (Accounts)",
        "/v2/tiktok/user/posts": "Посты аккаунта TikTok",
        "/v1/tiktok/video/comments": "Комментарии TikTok (fallback)",
        "/v2/tiktok/video": "Метрики видео TikTok (Enrich)",
        "/v1/instagram/user": "Профиль Instagram",
        "/v2/instagram/user/posts": "Посты аккаунта Instagram",
        "/v1/instagram/post": "Метрики поста Instagram (Enrich)",
        "/v2/instagram/post/comments": "Комментарии Instagram (fallback)",
        "/v1/twitter/user": "Профиль X/Twitter",
        "/v1/twitter/tweet": "Метрики твита (Enrich)",
        "/v1/twitter/user/tweets": "Твиты аккаунта",
        "/v1/youtube/video/comments": "Комментарии YouTube"
    }
}
