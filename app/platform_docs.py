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
            "cost": "SC кредиты (~1-2 кредита на запрос). YouTube — бесплатно",
            "cache": "Нет (дубли фильтруются по post_id)",
            "schedule": "manual / hourly / daily / weekly — настраивается на странице кампании. Хранится next_run_at в UTC",
            "filters": "min_views, min_er (%), max_age_days, languages",
            "saved_fields": "post_id, platform, url, views, likes, comments, shares, er, published, language, thumbnail_url",
            "platforms": {
                "TikTok (аккаунт)": "SC GET /v3/tiktok/profile/videos (param: handle)",
                "TikTok (хэштег)": "SC GET /v1/tiktok/search/hashtag",
                "TikTok (ключевое слово)": "SC GET /v1/tiktok/search/keyword",
                "Instagram (аккаунт)": "SC GET /v2/instagram/user/posts",
                "Instagram (хэштег)": "SC GET /v1/instagram/search/hashtag",
                "YouTube (ключевое слово)": "YouTube Data API GET /v3/search → /v3/videos (бесплатно, 10K units/день)",
            }
        },
        {
            "id": "tags",
            "title": "Tags",
            "icon": "🏷",
            "description": "Ручная категоризация постов. Фильтрация и группировка по тегам на странице Tags.",
            "api": "Internal DB (нет внешних API)",
            "cost": "Бесплатно",
            "cache": "Нет",
            "platforms": {
                "Все платформы": "Теги привязываются к постам через PostTag (junction table). Поиск по нескольким тегам — AND-логика"
            },
            "collection_flow": [
                "Создание тега: POST /api/tags",
                "Привязка к посту: POST /api/posts/{post_id}/tags/{tag_id}",
                "Отвязка: DELETE /api/posts/{post_id}/tags/{tag_id}",
                "Просмотр: /tags — все посты с тегами, мультифильтр"
            ]
        },
        {
            "id": "enrich",
            "title": "Enrich",
            "icon": "⚡",
            "description": "Обогащение CSV-файла подрядчика актуальными метриками. Загружаешь список URL — получаешь обновлённые данные.",
            "api": "ScrapeCreators",
            "cost": "~1-2 SC кредита на URL (до 50 запросов параллельно)",
            "cache": "30 дней (история задач в памяти)",
            "output_columns": "URL | Platform | Type | Author | Followers | Views | Likes | Comments | Shares | Saves | Date | Status",
            "platforms": {
                "TikTok": "SC GET /v2/tiktok/video → play_count, digg_count, comment_count, share_count, collect_count, author{unique_id, follower_count}",
                "Instagram": "SC GET /v1/instagram/post → xdt_shortcode_media{video_play_count, like_count, comment_count, __typename(Video/Carousel/Image)}",
                "X/Twitter": "SC GET /v1/twitter/tweet → views{count}, favorite_count, reply_count, retweet_count, bookmark_count",
                "YouTube": "YouTube Data API GET /v3/videos → viewCount, likeCount, commentCount (бесплатно)"
            },
            "notes": "Просмотры для Instagram image/carousel = 0 (платформа не отдаёт). Type (Video/Carousel/Image) — только для Instagram"
        },
        {
            "id": "comments_onetime",
            "title": "Comments (One-time)",
            "icon": "💬",
            "description": "Разовый сбор комментариев из CSV. Без сохранения в базу. Два режима вывода.",
            "api": "Apify (primary) + ScrapeCreators (fallback)",
            "cost": "Apify TikTok: ~$0.003-0.005/комментарий. Instagram: $2.10/1000. SC fallback: кредиты",
            "limit": "200 комментариев на пост",
            "cache": "30 дней (история задач в памяти)",
            "collection_flow": [
                "Режим 1 — Comments Only: CSV с полями author, text, likes, date, is_reply, language, user_region",
                "Режим 2 — Comments + Audit: HTML-отчёт через Claude AI (claude-sonnet-4-6) — тональность, темы, рекомендации",
                "Фильтрация: описания постов (автор URL = автор комментария) → исключаются автоматически"
            ],
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
            }
        },
        {
            "id": "projects",
            "title": "Projects",
            "icon": "📁",
            "description": "Постоянный мониторинг с накоплением в базе. Загружаешь список URL, запускаешь Update — данные накапливаются.",
            "api": "Apify (primary) + ScrapeCreators (fallback)",
            "cost": "Зависит от объёма. Metрики: ~1 SC кредит/пост. Комменты: Apify $. Лайкеры: $0.001311/пользователь",
            "cache": "Комментарии: 36ч. Метрики: 7 дней. Лайкеры: только посты с likers_count=0",
            "collection_flow": [
                "1. Update (▶): сбор комментариев (5 постов параллельно, Apify → SC fallback)",
                "2. Update: сбор метрик (10 постов параллельно, SC) — views, likes, er, date, author, followers",
                "3. Лайкеры (♥ Collect Likers): Instagram only, Apify datadoping, только посты с likers_count=0",
                "4. Clear & Re-collect: сброс всех данных + кэша, пересбор с нуля"
            ],
            "platforms": {
                "TikTok комменты": "Apify clockworks~tiktok-scraper → SC /v1/tiktok/video/comments (fallback)",
                "Instagram комменты": "Apify apify~instagram-comment-scraper → SC /v2/instagram/post/comments (fallback)",
                "YouTube комменты": "SC /v1/youtube/video/comments (только SC)",
                "X/Twitter комменты": "Apify scraper_one~x-post-replies-scraper (нет fallback)",
                "Метрики (все платформы)": "SC /v1/instagram/post, /v2/tiktok/video, /v1/twitter/tweet, YouTube API",
                "Лайкеры (Instagram)": "Apify datadoping~instagram-likes-scraper, max 100/пост"
            },
            "post_statuses": "active | deleted (удалён) | unavailable (приватный) | comments_disabled (автор закрыл)",
            "metrics_er": "(likes + comments) / views × 100. Fallback для Instagram image/carousel: (likes + comments) / followers × 100"
        },
        {
            "id": "accounts",
            "title": "Accounts",
            "icon": "👤",
            "description": "Полный аудит одного аккаунта: профиль + последние 50 постов + комментарии + лайкеры. Одна кнопка ▶ Full Audit.",
            "api": "ScrapeCreators + Apify",
            "cost": "Профиль: ~1 SC. Посты: ~1-2 SC. Комменты: Apify $. Метрики 50 постов: ~50 SC. Лайкеры (IG): Apify $",
            "limit": "50 постов на аккаунт",
            "cache": "Метрики: без кэша (force_metrics=True каждый раз). Лайкеры: только посты с likers_count=0",
            "collection_flow": [
                "1. Fetch Profile — SC /v1/{platform}/profile → username, followers, bio, verified, avatar",
                "2. Fetch Posts (до 50, пагинация) — SC /v2/{platform}/user/posts",
                "3. Collect Comments — Apify → SC fallback, 200/пост",
                "4. Collect Metrics — SC, без 7-дневного кэша (force_metrics=True)",
                "5. Collect Likers — Apify datadoping (только Instagram, только likers_count=0)"
            ],
            "platforms": {
                "TikTok профиль": "SC GET /v1/tiktok/profile (param: handle) → username, followers, following, posts, likes_total, region, verified",
                "TikTok посты": "SC GET /v3/tiktok/profile/videos (param: handle) → aweme_list[{aweme_id, webVideoUrl}]",
                "Instagram профиль": "SC GET /v1/instagram/user (param: username, fallback: handle) → followers, following, posts, bio, is_business",
                "Instagram посты": "SC GET /v2/instagram/user/posts (param: handle, пагинация next_max_id) → url из поля url",
                "YouTube профиль": "YouTube Data API GET /v3/channels (forHandle) → subscribers, videos, views_total, country",
                "YouTube посты": "YouTube Data API GET /v3/search (channelId, order=date)",
                "X/Twitter профиль": "SC GET /v1/twitter/user (param: username) → followers, following, tweets, is_blue_verified",
                "X/Twitter посты": "SC GET /v1/twitter/user/tweets (param: username, count=50)"
            },
            "notes": "Просмотры Instagram image/carousel = 0 (ограничение платформы). ER для них считается через followers"
        }
    ],
    "cost_summary": {
        "ScrapeCreators": "Кредиты (шапка ✦ N SC). ~1-2 кредита на запрос. Текущий баланс виден в шапке",
        "Apify TikTok комменты": "clockworks~tiktok-scraper: ~$0.003-0.005 на комментарий",
        "Apify Instagram комменты": "apify~instagram-comment-scraper: $2.10/1000 комментариев",
        "Apify X реплаи": "scraper_one~x-post-replies-scraper: $0.25/1000 элементов",
        "Apify Instagram лайкеры": "datadoping~instagram-likes-scraper: $1.30/1000 пользователей, MAX 100/пост",
        "YouTube API": "Бесплатно, 10 000 units/день. 1 поиск = 100 units, 1 видео = 1 unit"
    },
    "apify_actors": {
        "clockworks~tiktok-scraper": "Комментарии TikTok + authorRegion (бесплатно вместе с комментами)",
        "apify~instagram-comment-scraper": "Комментарии Instagram",
        "scraper_one~x-post-replies-scraper": "Реплаи X/Twitter (нет SC fallback)",
        "datadoping~instagram-likes-scraper": "Лайкеры Instagram (max 100/пост, только likers_count=0)"
    },
    "sc_endpoints_used": {
        "/v1/tiktok/profile": "Профиль TikTok (Accounts) — param: handle",
        "/v3/tiktok/profile/videos": "Посты аккаунта TikTok (Accounts + Campaigns) — param: handle → aweme_list",
        "/v3/tiktok/profile/videos": "Посты по аккаунту (Campaigns)",
        "/v1/tiktok/search/hashtag": "Посты по хэштегу TikTok (Campaigns)",
        "/v1/tiktok/search/keyword": "Посты по ключевому слову TikTok (Campaigns)",
        "/v2/tiktok/video": "Метрики видео TikTok (Enrich)",
        "/v1/tiktok/video/comments": "Комментарии TikTok (fallback)",
        "/v1/instagram/user": "Профиль Instagram (Accounts) — param: username, fallback: handle",
        "/v2/instagram/user/posts": "Посты аккаунта Instagram (Accounts, Campaigns) — param: handle",
        "/v1/instagram/search/hashtag": "Посты по хэштегу Instagram (Campaigns)",
        "/v1/instagram/post": "Метрики поста Instagram (Enrich, Projects)",
        "/v2/instagram/post/comments": "Комментарии Instagram (fallback)",
        "/v1/twitter/user": "Профиль X/Twitter (Accounts) — param: username",
        "/v1/twitter/tweet": "Метрики твита (Enrich)",
        "/v1/twitter/user/tweets": "Твиты аккаунта (Accounts) — param: username",
        "/v1/youtube/video/comments": "Комментарии YouTube (Comments, Projects)"
    }
}
