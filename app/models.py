from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, UniqueConstraint, ForeignKey
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Vertical(Base):
    __tablename__ = "verticals"
    id   = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"

    id           = Column(Integer, primary_key=True)
    name         = Column(String, nullable=False)
    vertical     = Column(String, default="")
    platforms    = Column(Text)   # JSON: ["tiktok", "instagram"]
    hashtags     = Column(Text)   # JSON: ["ClashOfClans", ...]
    accounts     = Column(Text)   # JSON: ["handle", ...]
    keywords     = Column(Text, default="[]")  # JSON: ["bestplay app", ...]
    min_views    = Column(Integer, default=300000)
    min_er       = Column(Float,   default=2.0)
    max_age_days = Column(Integer, default=180)
    languages    = Column(Text)   # JSON: ["en", "ru", ...] or ["all"]
    status             = Column(String, default="active")   # active / stopped
    created_at         = Column(DateTime, default=datetime.utcnow)
    last_run_at        = Column(DateTime, nullable=True)
    next_run_at        = Column(DateTime, nullable=True)
    schedule_frequency = Column(String, default="manual")   # manual/hourly/daily/weekly
    schedule_time      = Column(String, default="10:00")     # HH:MM — время запуска
    schedule_days      = Column(Text, default="[]")          # JSON: ["mon","wed","fri"]
    schedule_end_date  = Column(DateTime, nullable=True)     # None = indefinitely


class Post(Base):
    __tablename__ = "posts"

    id          = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    post_id     = Column(String,  nullable=False)
    platform    = Column(String)
    account     = Column(String)
    url         = Column(String)
    views       = Column(Integer, default=0)
    likes       = Column(Integer, default=0)
    comments    = Column(Integer, default=0)
    shares      = Column(Integer, default=0)
    er          = Column(Float,   default=0.0)
    published   = Column(String)
    language    = Column(String)
    added_at    = Column(DateTime, default=datetime.utcnow)


class Tag(Base):
    __tablename__ = "tags"
    id   = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)


class PostTag(Base):
    __tablename__ = "post_tags"
    id      = Column(Integer, primary_key=True)
    post_id = Column(Integer, nullable=False, index=True)
    tag_id  = Column(Integer, nullable=False, index=True)
    __table_args__ = (UniqueConstraint("post_id", "tag_id"),)


class Run(Base):
    __tablename__ = "runs"

    id          = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    started_at  = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    posts_added = Column(Integer,  default=0)
    status      = Column(String,   default="running")  # running / done / error
    error       = Column(Text,     nullable=True)


class CommentProject(Base):
    __tablename__ = "comment_projects"

    id         = Column(Integer, primary_key=True)
    name       = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    schedule   = Column(String, default="manual")   # manual / daily / weekly
    schedule_time = Column(String, default="10:00")


class CommentSource(Base):
    __tablename__ = "comment_sources"

    id              = Column(Integer, primary_key=True)
    project_id      = Column(Integer, ForeignKey("comment_projects.id"), nullable=False, index=True)
    url             = Column(String, nullable=False)
    platform        = Column(String)
    last_fetched_at = Column(DateTime, nullable=True)
    comments_count  = Column(Integer, default=0)
    __table_args__  = (UniqueConstraint("project_id", "url"),)


class StoredComment(Base):
    __tablename__ = "stored_comments"

    id          = Column(Integer, primary_key=True)
    source_id   = Column(Integer, ForeignKey("comment_sources.id"), nullable=False, index=True)
    platform    = Column(String)
    comment_id  = Column(String)        # уникальный ID от платформы
    author      = Column(String)
    text        = Column(Text)
    likes       = Column(Integer, default=0)
    date        = Column(String)
    is_reply    = Column(Boolean, default=False)
    language    = Column(String)        # определяется через langdetect
    user_region = Column(String)        # TikTok only, через Apify
    fetched_at  = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("source_id", "comment_id"),)
