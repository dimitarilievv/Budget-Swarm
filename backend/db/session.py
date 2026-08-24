"""Database engine + session factory + connectivity check (BS-018).

Provides the SQLAlchemy engine/session built from DATABASE_URL, a FastAPI
dependency (`get_db`), the declarative `Base` for ORM models (mapped in BS-028),
and a lightweight liveness check used by GET /api/health.
"""

from collections.abc import Iterator
from functools import lru_cache

from config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# Declarative base for ORM models. Table mappings are added in BS-028; Alembic's
# baseline migration applies the raw DDL from docs/schema.sql independently.
Base = declarative_base()

# Session factory. Bound lazily (see get_db) so importing this module never
# forces a connection — important for tests and offline tooling.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False)


@lru_cache
def get_engine() -> Engine:
    """Create the engine lazily on first use.

    Short connect timeout + pool_pre_ping keep the app responsive even when the
    database is unreachable.
    """
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session, closed after the request."""
    db = SessionLocal(bind=get_engine())
    try:
        yield db
    finally:
        db.close()


def ping() -> bool:
    """Return True if a trivial query against the database succeeds."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # Any failure (connection refused, auth, timeout) means "down".
        return False
