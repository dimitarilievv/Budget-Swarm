"""accommodations — listing detail columns (BS-104)

Surfaces detail already present in the ingested Inside Airbnb rows (kept in
``raw_accommodation_listings.raw`` but previously dropped at normalization) so the UI can
show a richer, real accommodation card: the listing's Airbnb URL, a photo, the review
count, the location sub-score (0–5), the neighbourhood, and a short amenities list. All are
optional (a price-stats-estimate option has none of them).

Revision ID: 0004_accom_details
Revises: 0003_accom_price_stats
Create Date: 2026-09-29

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_accom_details"
down_revision: str | None = "0003_accom_price_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
ALTER TABLE accommodations
    ADD COLUMN listing_url    VARCHAR(300),
    ADD COLUMN picture_url    VARCHAR(500),
    ADD COLUMN review_count   INTEGER,
    ADD COLUMN location_score NUMERIC(3,2),
    ADD COLUMN neighbourhood  VARCHAR(120),
    ADD COLUMN amenities      JSONB;
"""

DOWNGRADE_DDL = """
ALTER TABLE accommodations
    DROP COLUMN IF EXISTS listing_url,
    DROP COLUMN IF EXISTS picture_url,
    DROP COLUMN IF EXISTS review_count,
    DROP COLUMN IF EXISTS location_score,
    DROP COLUMN IF EXISTS neighbourhood,
    DROP COLUMN IF EXISTS amenities;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_DDL)
