"""Typed application settings (BS-015).

All configuration and secrets are loaded from the environment (and a local
`.env` in dev) via pydantic-settings. Keys live server-side only and are never
exposed to the frontend. Required vars fail fast at startup with a clear error.

Documented vars mirror the table in docs/README.md §Environment variables.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Required ---------------------------------------------------------
    # No default: a missing DATABASE_URL raises a clear ValidationError at
    # startup rather than failing later with an obscure connection error.
    database_url: str

    # Secret used to sign/verify JWT access + refresh tokens (BS-110, auth). No
    # default — a missing JWT_SECRET fails fast at startup rather than silently
    # signing tokens with a predictable key. Must be a long random string in prod.
    jwt_secret: str

    # --- Auth / JWT -------------------------------------------------------
    # Access tokens are short-lived (carried in the Authorization header); refresh
    # tokens are long-lived, stored hashed, and rotated on each use. All tunable via
    # env so deployments can tighten/loosen without a code change.
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # --- CORS -------------------------------------------------------------
    # Comma-separated allowed frontend origins. No wildcard (docs/api.md §CORS).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Provider keys (server-side only) --------------------------------
    # Conditionally required at request time (car / air mode), not at startup,
    # so they are optional here and validated by the services that use them.
    ors_api_key: str | None = None  # OpenRouteService — car routing
    travelpayouts_token: str | None = None  # flight prices — air mode
    public_transport_api_key: str | None = None  # bus/train — fallback exists
    opentripmap_api_key: str | None = None  # POIs — Overpass fallback needs no key
    apify_token: str | None = None  # Inside Airbnb scraping

    # --- LLM (explanation layer; Ollama needs no key) --------------------
    llm_provider: str = "ollama"
    llm_api_key: str | None = None

    # --- Engine wiring (BS-081) ------------------------------------------
    # Off by default: the app serves the stub engine until the real pipeline is
    # switched on here. The real engine additionally requires a *seeded* reference
    # database (curated destinations/features/attractions/accommodations); with an
    # empty DB it has no candidates to rank. Trip persistence is wired independently
    # whenever the database is reachable, regardless of this flag.
    enable_real_engine: bool = False

    # --- Security: per-client rate limiting (BS-093, technical-doc §24) ----
    # A sliding window per client IP on POST /api/generate-trip, to stop one
    # client from exhausting the backend's own upstream free tiers. These are
    # plain tuning knobs (no secrets); set RATE_LIMIT_ENABLED=false to disable.
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 20
    rate_limit_window_seconds: int = 60

    # A separate, stricter sliding window for the credential endpoints (POST
    # /api/auth/login and /register) to blunt brute-force/enumeration attempts:
    # at most ``auth_rate_limit_requests`` per ``auth_rate_limit_window_seconds``
    # per client IP. Governed by the same RATE_LIMIT_ENABLED master switch.
    auth_rate_limit_requests: int = 5
    auth_rate_limit_window_seconds: int = 60

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS origins as a clean list for the FastAPI middleware."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached after first load)."""
    return Settings()


class _MigrationSettings(BaseSettings):
    """Minimal settings for migrations/DB tooling — only the database URL is required.

    Running ``alembic`` must not depend on the *application's* secrets (e.g. ``JWT_SECRET``):
    a migration only needs ``DATABASE_URL``. Validating the full :class:`Settings` there
    would make ``alembic upgrade`` fail wherever the app secrets aren't set (notably CI's
    migration step, which provides only ``DATABASE_URL``). This loads the same env/.env but
    validates nothing beyond the connection string.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str


def get_database_url() -> str:
    """The database URL for migrations/tooling, without requiring the rest of the config."""
    return _MigrationSettings().database_url
