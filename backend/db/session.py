"""Database engine + connectivity check (BS-014).

Table mapping and the ORM session live in BS-028 (db/models.py). For now this
module only provides a lightweight liveness check used by GET /api/health.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config import DATABASE_URL

# A small pool with a short connect timeout so the health endpoint stays
# responsive even when the database is unreachable.
_engine: Engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 2},
)


def ping() -> bool:
    """Return True if a trivial query against the database succeeds."""
    try:
        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # Any failure (connection refused, auth, timeout) means "down".
        return False
