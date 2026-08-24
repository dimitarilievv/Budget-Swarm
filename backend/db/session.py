"""Database engine + connectivity check (BS-014, settings via BS-015).

Table mapping and the ORM session live in BS-028 (db/models.py). For now this
module only provides a lightweight liveness check used by GET /api/health.
"""

from functools import lru_cache

from config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


@lru_cache
def _get_engine() -> Engine:
    """Create the engine lazily on first use.

    Short connect timeout + pool_pre_ping keep the health endpoint responsive
    even when the database is unreachable.
    """
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


def ping() -> bool:
    """Return True if a trivial query against the database succeeds."""
    try:
        with _get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # Any failure (connection refused, auth, timeout) means "down".
        return False
