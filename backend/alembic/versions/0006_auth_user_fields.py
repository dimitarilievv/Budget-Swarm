"""auth: user profile fields, refresh tokens, saved-trip labels (auth)

Extends the account surface for authentication/authorization:

* ``users`` gains an optional ``name`` and an ``updated_at`` timestamp (mirroring the
  pattern used elsewhere: ``NOT NULL DEFAULT now()``, bumped by the ORM ``onupdate``).
* a new ``refresh_tokens`` table stores **hashed**, revocable refresh tokens (one row
  per issued token), so refresh tokens can be rotated and revoked server-side.
* ``trips`` gains optional ``title``/``notes`` so an owner can label a saved trip
  (PATCH /api/trips/{id}).

``trips.user_id`` is intentionally left **nullable** (ADR-012): the application now
stamps every API-created trip with its owner and scopes all access by it, so no new
unowned rows are created, while any pre-existing anonymous rows stay valid and simply
unreachable through the API. This is the safe, non-destructive migration — no backfill
or NOT NULL rewrite that would break historical rows.

Revision ID: 0006_auth_user_fields
Revises: 0005_accom_capacity
Create Date: 2026-10-06

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_auth_user_fields"
down_revision: str | None = "0005_accom_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
ALTER TABLE users ADD COLUMN name VARCHAR(120);
ALTER TABLE users ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE refresh_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  VARCHAR(64) NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX idx_refresh_tokens_user ON refresh_tokens(user_id);

ALTER TABLE trips ADD COLUMN title VARCHAR(120);
ALTER TABLE trips ADD COLUMN notes TEXT;
"""

DOWNGRADE_DDL = """
ALTER TABLE trips DROP COLUMN IF EXISTS notes;
ALTER TABLE trips DROP COLUMN IF EXISTS title;

DROP TABLE IF EXISTS refresh_tokens CASCADE;

ALTER TABLE users DROP COLUMN IF EXISTS updated_at;
ALTER TABLE users DROP COLUMN IF EXISTS name;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_DDL)
