"""Application configuration (BS-014).

Reads settings from the environment. Keep this thin — only what the app
needs to boot. Provider keys and richer config arrive in later tickets.
"""
import os


def _split_origins(raw: str) -> list[str]:
    """Parse a comma-separated origins list into a clean list."""
    return [o.strip() for o in raw.split(",") if o.strip()]


# Database connection string. Matches the compose service ('db') by default.
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://budgetswarm:change_me@db:5432/budgetswarm",
)

# Allowed frontend origin(s) for CORS. No wildcard — see docs/api.md §CORS.
# Override with CORS_ORIGINS="https://foo.com,https://bar.com".
CORS_ORIGINS: list[str] = _split_origins(
    os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
)
