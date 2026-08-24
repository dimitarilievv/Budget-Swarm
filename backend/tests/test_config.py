"""Unit tests for typed settings loading (BS-015)."""
import pytest
from pydantic import ValidationError

from config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("ORS_API_KEY", "secret-key")

    s = Settings(_env_file=None)  # ignore any local .env; read process env only

    assert s.database_url.endswith("/x")
    assert s.ors_api_key == "secret-key"
    # Optional keys default to None; LLM provider defaults to ollama.
    assert s.travelpayouts_token is None
    assert s.llm_provider == "ollama"
    # CORS parses to a clean list with no wildcard.
    assert "http://localhost:5173" in s.cors_origins_list


def test_missing_required_var_raises_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    # The error names the offending field so the failure is actionable.
    assert "database_url" in str(exc_info.value).lower()
