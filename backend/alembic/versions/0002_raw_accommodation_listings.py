"""raw_accommodation_listings — raw accommodation import staging (BS-033)

Adds the source-agnostic ``raw_accommodation_listings`` staging table that holds
accommodation listings exactly as downloaded — the full CSV row preserved as
JSONB, *before* any cleaning — from any source (Inside Airbnb today; another open
dataset tomorrow). BS-034 reads this table to validate and normalize usable rows
into ``accommodations``; the raw layer means the lossy steps never destroy the
source and a re-import is idempotent.

``source`` names the provider; ``snapshot_label`` (``<source>-<city>-<date>``)
records the exact snapshot each row came from; ``(snapshot_label, listing_id)`` is
unique so re-importing a snapshot upserts in place rather than duplicating.

Revision ID: 0002_airbnb_raw
Revises: 0001_baseline
Create Date: 2026-08-29

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_airbnb_raw"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
CREATE TABLE raw_accommodation_listings (
    id             BIGSERIAL PRIMARY KEY,
    source         VARCHAR(40) NOT NULL,      -- provider name, e.g. 'inside-airbnb'
    snapshot_label VARCHAR(60) NOT NULL,      -- '<source>-<city>-<date>'
    city           VARCHAR(80) NOT NULL,      -- city slug, e.g. 'riga'
    snapshot_date  DATE NOT NULL,             -- the snapshot's scrape date
    listing_id     VARCHAR(64) NOT NULL,      -- source's native listing id (kept as text)
    raw            JSONB NOT NULL,            -- full original CSV row, uncleaned
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_label, listing_id)
);
CREATE INDEX idx_raw_accom_source_snapshot ON raw_accommodation_listings(source, snapshot_label);
CREATE INDEX idx_raw_accom_city            ON raw_accommodation_listings(city);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS raw_accommodation_listings CASCADE;")
