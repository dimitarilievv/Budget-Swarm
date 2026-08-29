"""accommodation_price_stats — per-destination price statistics (BS-035)

Adds the ``accommodation_price_stats`` feature table: one row per destination
holding the precomputed EUR price distribution (count + min/Q1/median/Q3/max) of
its normalized ``accommodations``. StayAgent (BS-050) uses these to pick
representative options (a budget / mid / well-rated choice) without scanning
thousands of rows, and — for a city with no Inside Airbnb file — as the
``estimated`` price fallback (per-city statistics estimate, technical doc §10.2).

Keyed by ``destination_id`` (one current stats row per destination, upserted when
accommodations are (re)normalized).

Revision ID: 0003_accom_price_stats
Revises: 0002_airbnb_raw
Create Date: 2026-08-29

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_accom_price_stats"
down_revision: str | None = "0002_airbnb_raw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
CREATE TABLE accommodation_price_stats (
    destination_id INTEGER PRIMARY KEY REFERENCES destinations(id) ON DELETE CASCADE,
    sample_size    INTEGER NOT NULL,
    currency       CHAR(3) NOT NULL DEFAULT 'EUR',
    price_min      NUMERIC(10,2),
    price_q1       NUMERIC(10,2),
    price_median   NUMERIC(10,2),
    price_q3       NUMERIC(10,2),
    price_max      NUMERIC(10,2),
    snapshot_label VARCHAR(60),
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS accommodation_price_stats CASCADE;")
