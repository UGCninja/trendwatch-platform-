from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Campaign(Base):
    __tablename__ = "campaigns"

    id           = Column(Integer, primary_key=True)
    name         = Column(String, nullable=False)
    vertical     = Column(String, default="")
    platforms    = Column(Text)   # JSON: ["tiktok", "instagram"]
    hashtags     = Column(Text)   # JSON: ["ClashOfClans", ...]
    accounts     = Column(Text)   # JSON: ["handle", ...]
    min_views    = Column(Integer, default=300000)
    min_er       = Column(Float,   default=2.0)
    max_age_days = Column(Integer, default=180)
    languages    = Column(Text)   # JSON: ["en", "ru", ...] or ["all"]
    status       = Column(String, default="active")   # active / stopped
    created_at   = Column(DateTime, default=datetime.utcnow)
    last_run_at  = Column(DateTime, nullable=True)
    next_run_at  = Column(DateTime, nullable=True)


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


class Run(Base):
    __tablename__ = "runs"

    id          = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    started_at  = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    posts_added = Column(Integer,  default=0)
    status      = Column(String,   default="running")  # running / done / error
    error       = Column(Text,     nullable=True)
