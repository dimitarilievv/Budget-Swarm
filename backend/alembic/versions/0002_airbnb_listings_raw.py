"""airbnb_listings_raw — raw Inside Airbnb import staging (BS-033)

Adds the ``airbnb_listings_raw`` staging table that holds Inside Airbnb listings
exactly as downloaded — the full CSV row preserved as JSONB, *before* any
cleaning or normalization. BS-034 reads this table to validate and normalize the
usable rows into ``accommodations``; keeping a raw layer means the lossy steps
(price parsing, EUR conversion, dropping rows with no price) never destroy the
source, and a re-run of the importer is idempotent per snapshot.

``snapshot_label`` (``inside-airbnb-<city>-<date>``) records the exact Inside
Airbnb snapshot each row came from; ``(snapshot_label, listing_id)`` is unique so
re-importing the same snapshot upserts in place rather than duplicating.

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
CREATE TABLE airbnb_listings_raw (
    id             BIGSERIAL PRIMARY KEY,
    snapshot_label VARCHAR(60) NOT NULL,      -- 'inside-airbnb-<city>-<date>'
    city           VARCHAR(80) NOT NULL,      -- Inside Airbnb city slug, e.g. 'riga'
    snapshot_date  DATE NOT NULL,             -- the snapshot's scrape date
    listing_id     VARCHAR(32) NOT NULL,      -- Inside Airbnb 'id' (kept as text)
    raw            JSONB NOT NULL,            -- full original CSV row, uncleaned
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_label, listing_id)
);
CREATE INDEX idx_airbnb_raw_snapshot ON airbnb_listings_raw(snapshot_label);
CREATE INDEX idx_airbnb_raw_city     ON airbnb_listings_raw(city);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS airbnb_listings_raw CASCADE;")
