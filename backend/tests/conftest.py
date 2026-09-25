"""Test fixtures/bootstrap (BS-015).

Ensure a DATABASE_URL is present before app modules import, so importing
`main` (which validates settings at startup) succeeds without a real .env.
The URL points at a non-running host on purpose — DB-touching tests patch the
connectivity check instead of relying on a live database.

Also put the repository root on ``sys.path`` so tests can import the top-level
``ai_engine`` package (pytest already adds ``backend/`` for ``db``/``scripts``,
but not the repo root — BS-038).
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")

# Keep the per-client rate limit effectively unlimited for the general suite (many
# tests POST /api/generate-trip from the same TestClient host in quick succession);
# the dedicated rate-limit test overrides the limiter to a small window on purpose.
os.environ.setdefault("RATE_LIMIT_REQUESTS", "1000000")
