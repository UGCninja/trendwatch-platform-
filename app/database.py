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
