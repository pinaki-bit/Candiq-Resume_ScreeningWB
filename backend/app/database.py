"""
backend/app/database.py

SQLAlchemy 2.x async-compatible engine factory and session management.
Supports SQLite (dev) and PostgreSQL (prod) via DATABASE_URL env var.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _make_engine():
    settings = get_settings()
    url = settings.database_url

    connect_args = {}
    if url.startswith("sqlite"):
        # SQLite requires check_same_thread=False when used with FastAPI's
        # thread pool (sync workers).
        connect_args["check_same_thread"] = False

    engine = create_engine(
        url,
        connect_args=connect_args,
        echo=(settings.app_env == "development"),
        pool_pre_ping=True,
    )

    # Enable WAL mode for SQLite — better concurrency
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _make_engine()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# ORM base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Dependency — FastAPI dependency injection
# ---------------------------------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    """
    Yield a database session and ensure it is closed after the request,
    even if an exception occurs.

    Usage in a router:
        db: Session = Depends(get_db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_all_tables() -> None:
    """Create all tables that are registered on Base.metadata and ensure columns exist."""
    # Import models here so their metadata is registered before create_all.
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # Auto-migration safeguard for SQLite databases missing Phase 25 columns
    try:
        with engine.begin() as conn:
            # Check if resumes table exists
            check_table = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='resumes'")
            ).fetchone()
            if check_table:
                info = conn.execute(text("PRAGMA table_info(resumes)")).fetchall()
                existing_cols = {row[1] for row in info}
                missing_cols = [
                    ("classification_status", "VARCHAR(32)"),
                    ("review_required", "BOOLEAN"),
                    ("ood_status", "VARCHAR(64)"),
                    ("policy_version", "VARCHAR(64)"),
                    ("policy_reason", "VARCHAR(128)"),
                ]
                for col_name, col_type in missing_cols:
                    if col_name not in existing_cols:
                        conn.execute(text(f"ALTER TABLE resumes ADD COLUMN {col_name} {col_type}"))
    except Exception:
        # Ignore if non-sqlite or already migrated
        pass


def check_db_connection() -> bool:
    """Return True if the DB can be reached, False otherwise."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
