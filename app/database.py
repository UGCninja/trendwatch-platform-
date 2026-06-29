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


def init_db():
    Base.metadata.create_all(bind=engine)
    # Add new columns if they don't exist yet
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN vertical VARCHAR DEFAULT ''"))
            conn.commit()
        except Exception:
            pass  # column already exists


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
