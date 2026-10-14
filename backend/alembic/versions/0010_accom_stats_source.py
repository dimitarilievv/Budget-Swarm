"""accommodation_price_stats — provenance (source) column (BS-estimate)

Distinguishes real Inside Airbnb price statistics from modeled WhereNext cost-of-living
estimates. Real rows (from ``normalize_accommodations``) are stamped
``source = 'inside-airbnb'`` with a non-zero ``sample_size``; modeled rows (from
``estimate_accommodation_prices``) are ``source = 'wherenext-cost-model'`` with
``sample_size = 0``. Nullable for backward compatibility with any pre-existing rows.

Revision ID: 0010_accom_stats_source
Revises: 0009_destination_active
Create Date: 2026-10-14

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_accom_stats_source"
down_revision: str | None = "0009_destination_active"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE accommodation_price_stats ADD COLUMN source VARCHAR(40);"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE accommodation_price_stats DROP COLUMN IF EXISTS source;"
    )
