"""
SQLAlchemy engine + session setup.

DATABASE_URL should point at Postgres in any real environment (e.g. a Neon
connection string: postgresql://user:pass@ep-xxx.neon.tech/dbname). Falls
back to a local SQLite file only if DATABASE_URL isn't set, purely so this
can be imported/tested without a live Postgres instance — don't rely on
that fallback for anything beyond local scratch testing.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Loaded here directly (not just relied on via config.py) — this module is
# also imported standalone by alembic/env.py when running migrations from
# the CLI, which never touches config.py and would otherwise silently miss
# .env, falling back to the SQLite default below even when a real
# DATABASE_URL is set.
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./local_dev.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a session, closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()