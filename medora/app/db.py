import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def _database_url() -> str:
    """DATABASE_URL (or the POSTGRES_URL a Vercel/Neon integration sets), else local SQLite.
    'postgres://' and 'postgresql://' are pointed at the psycopg 3 driver."""
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or f"sqlite:///{(BASE_DIR / 'medora.db').as_posix()}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = _database_url()
if DATABASE_URL.startswith("sqlite"):
    _kwargs = {"connect_args": {"check_same_thread": False}}
elif os.getenv("VERCEL"):
    # Serverless: each instance is short-lived, so don't hold a pool open; the provider's pooler does that.
    _kwargs = {"poolclass": NullPool}
else:
    _kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}
if DATABASE_URL.startswith("postgresql+psycopg"):
    # Neon's pooled URL runs PgBouncer in transaction mode: no server-side prepared statements.
    _kwargs["connect_args"] = {"prepare_threshold": None}
engine = create_engine(DATABASE_URL, **_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
