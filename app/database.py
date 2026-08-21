import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trendwatch.db")

# Railway отдаёт postgres://, SQLAlchemy хочет postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine)


DEFAULT_VERTICALS = ["StrategyGames", "RewardsApps", "CryptoCasino", "SolitaireRefs"]

def _run_migration(sql: str):
    try:
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
    except Exception:
        pass


def init_db():
    Base.metadata.create_all(bind=engine)
    _run_migration("ALTER TABLE campaigns ADD COLUMN vertical VARCHAR DEFAULT ''")
    _run_migration("ALTER TABLE campaigns ADD COLUMN keywords TEXT DEFAULT '[]'")
    _run_migration("ALTER TABLE posts ADD COLUMN added_at DATETIME")
    _run_migration("ALTER TABLE campaigns ADD COLUMN schedule_frequency VARCHAR DEFAULT 'manual'")
    _run_migration("ALTER TABLE campaigns ADD COLUMN schedule_time VARCHAR DEFAULT '10:00'")
    _run_migration("ALTER TABLE campaigns ADD COLUMN schedule_days TEXT DEFAULT '[]'")
    _run_migration("ALTER TABLE campaigns ADD COLUMN schedule_end_date TIMESTAMP")
    _run_migration("ALTER TABLE campaigns ADD COLUMN schedule_end_date DATETIME")
    _run_migration("CREATE TABLE IF NOT EXISTS comment_projects (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE NOT NULL, created_at TIMESTAMP, schedule VARCHAR DEFAULT 'manual', schedule_time VARCHAR DEFAULT '10:00')")
    _run_migration("CREATE TABLE IF NOT EXISTS comment_sources (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, url VARCHAR NOT NULL, platform VARCHAR, last_fetched_at TIMESTAMP, comments_count INTEGER DEFAULT 0, UNIQUE(project_id, url))")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_location VARCHAR")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_location_lat FLOAT")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_location_lng FLOAT")
    _run_migration("CREATE TABLE IF NOT EXISTS stored_comments (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, platform VARCHAR, comment_id VARCHAR, author VARCHAR, text TEXT, likes INTEGER DEFAULT 0, date VARCHAR, is_reply BOOLEAN DEFAULT 0, language VARCHAR, user_region VARCHAR, fetched_at TIMESTAMP, UNIQUE(source_id, comment_id))")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN status VARCHAR DEFAULT 'active'")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN provider VARCHAR")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN creator VARCHAR")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN likers_count INTEGER DEFAULT 0")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_date VARCHAR")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_views INTEGER")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_likes INTEGER")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_comments_total INTEGER")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_er FLOAT")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_author VARCHAR")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN post_followers INTEGER")
    _run_migration("CREATE TABLE IF NOT EXISTS stored_likers (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, platform VARCHAR, username VARCHAR, user_region VARCHAR, fetched_at TIMESTAMP, UNIQUE(source_id, username))")
    _run_migration("ALTER TABLE stored_likers ADD COLUMN full_name VARCHAR")
    _run_migration("ALTER TABLE stored_likers ADD COLUMN is_verified BOOLEAN DEFAULT FALSE")
    _run_migration("ALTER TABLE stored_likers ADD COLUMN is_private BOOLEAN DEFAULT FALSE")
    _run_migration("ALTER TABLE comment_projects ADD COLUMN last_sc_spend INTEGER")
    _run_migration("ALTER TABLE comment_projects ADD COLUMN last_apify_spend FLOAT")
    _run_migration("ALTER TABLE comment_sources ADD COLUMN metrics_updated_at DATETIME")

    from app.models import Vertical
    db = SessionLocal()
    for name in DEFAULT_VERTICALS:
        if not db.query(Vertical).filter(Vertical.name == name).first():
            db.add(Vertical(name=name))
    db.commit()
    db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
