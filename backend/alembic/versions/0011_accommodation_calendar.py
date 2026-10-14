"""accommodation_calendar + accommodations.listing_id (BS-calendar)

Stores Inside Airbnb per-date calendar snapshots for date-based availability + pricing:
one row per (snapshot_label, listing_id, night) with availability, snapshot nightly
price, and min/max-night restrictions. Adds ``listing_id`` to ``accommodations`` so a
normalized listing links to its calendar. Snapshot data is point-in-time, NOT live.

Revision ID: 0011_accom_calendar
Revises: 0010_accom_stats_source
Create Date: 2026-10-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_accom_calendar"
down_revision: str | None = "0010_accom_stats_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE accommodations ADD COLUMN listing_id VARCHAR(64);")
    bind.exec_driver_sql(
        "CREATE INDEX idx_accommodations_listing ON accommodations (destination_id, listing_id);"
    )
    bind.exec_driver_sql("""
        CREATE TABLE accommodation_calendar (
            id BIGSERIAL PRIMARY KEY,
            snapshot_label VARCHAR(60) NOT NULL,
            listing_id VARCHAR(64) NOT NULL,
            night DATE NOT NULL,
            available BOOLEAN NOT NULL,
            price NUMERIC(10, 2),
            minimum_nights INTEGER,
            maximum_nights INTEGER,
            CONSTRAINT accommodation_calendar_snapshot_listing_night_key
                UNIQUE (snapshot_label, listing_id, night)
        );
        """)
    # Range lookup for one listing over the requested overnight window.
    bind.exec_driver_sql(
        "CREATE INDEX idx_accom_calendar_listing_night "
        "ON accommodation_calendar (listing_id, night);"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS accommodation_calendar CASCADE;")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_accommodations_listing;")
    bind.exec_driver_sql("ALTER TABLE accommodations DROP COLUMN IF EXISTS listing_id;")
