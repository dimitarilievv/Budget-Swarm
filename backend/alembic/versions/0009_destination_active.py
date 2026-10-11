"""destinations — active flag (soft enable/disable a candidate city)

Lets an operator take a destination out of the candidate pool without deleting it
or its data (the opposite of the onboarding command's prune, which is for a
never-usable new city). An inactive destination is skipped by ScoutAgent's
candidate load and the public listing, but its rows stay for later re-enabling.

Adds ``active`` (BOOLEAN NOT NULL DEFAULT true) — every existing city stays active.

Revision ID: 0009_destination_active
Revises: 0008_trip_reservation
Create Date: 2026-10-11

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_destination_active"
down_revision: str | None = "0008_trip_reservation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
ALTER TABLE destinations ADD COLUMN active BOOLEAN NOT NULL DEFAULT true;
"""

DOWNGRADE_DDL = """
ALTER TABLE destinations DROP COLUMN IF EXISTS active;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_DDL)
