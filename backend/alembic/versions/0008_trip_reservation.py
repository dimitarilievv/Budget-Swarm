"""trips — reservation status + booked flags (lock a booked trip)

A saved trip can be marked **reserved** once the user confirms they've booked transport and
stay (manual, since we only link out to providers). A reserved trip is locked against
price/destination re-planning — only the weather may still be refreshed.

Adds ``status`` ('planned'|'reserved', default 'planned'), ``transport_reserved`` and
``stay_reserved`` (default false).

Revision ID: 0008_trip_reservation
Revises: 0007_trip_snapshot
Create Date: 2026-10-07

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_trip_reservation"
down_revision: str | None = "0007_trip_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
ALTER TABLE trips ADD COLUMN status VARCHAR(10) NOT NULL DEFAULT 'planned'
    CHECK (status IN ('planned','reserved'));
ALTER TABLE trips ADD COLUMN transport_reserved BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE trips ADD COLUMN stay_reserved BOOLEAN NOT NULL DEFAULT false;
"""

DOWNGRADE_DDL = """
ALTER TABLE trips DROP COLUMN IF EXISTS stay_reserved;
ALTER TABLE trips DROP COLUMN IF EXISTS transport_reserved;
ALTER TABLE trips DROP COLUMN IF EXISTS status;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_DDL)
