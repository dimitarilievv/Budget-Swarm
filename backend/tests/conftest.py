"""Test fixtures/bootstrap (BS-015).

Ensure a DATABASE_URL is present before app modules import, so importing
`main` (which validates settings at startup) succeeds without a real .env.
The URL points at a non-running host on purpose — DB-touching tests patch the
connectivity check instead of relying on a live database.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
