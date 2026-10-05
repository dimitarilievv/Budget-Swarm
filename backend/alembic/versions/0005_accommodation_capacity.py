"""accommodations — capacity (accommodates) column (BS-104)

Persists each listing's guest capacity (Inside Airbnb ``accommodates``), so StayAgent can
select a place that fits the party size instead of one chosen on price alone. Optional
(a listing without a capacity, or a price-stats estimate, has none).

Revision ID: 0005_accom_capacity
Revises: 0004_accom_details
Create Date: 2026-09-29

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_accom_capacity"
down_revision: str | None = "0004_accom_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE accommodations ADD COLUMN accommodates INTEGER;")


def downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE accommodations DROP COLUMN IF EXISTS accommodates;")
