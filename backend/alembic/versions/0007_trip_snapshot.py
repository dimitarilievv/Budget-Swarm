"""trips — full plan snapshot + travel dates (saved-trip fidelity)

A saved trip now keeps the full generated plan (``plan_snapshot``) and the request that
produced it (``request_snapshot``) as JSONB, so opening it shows exactly what the user saw
(transport, stay, booking links, weather, ranked candidates, dates) and it can be re-priced
from the same inputs. ``start_date``/``end_date`` mirror the plan's travel window for the
list/ticket view. All nullable — legacy rows fall back to the header+activities
reconstruction and have no stored dates.

Revision ID: 0007_trip_snapshot
Revises: 0006_auth_user_fields
Create Date: 2026-10-06

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_trip_snapshot"
down_revision: str | None = "0006_auth_user_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_DDL = """
ALTER TABLE trips ADD COLUMN plan_snapshot JSONB;
ALTER TABLE trips ADD COLUMN request_snapshot JSONB;
ALTER TABLE trips ADD COLUMN start_date DATE;
ALTER TABLE trips ADD COLUMN end_date DATE;
"""

DOWNGRADE_DDL = """
ALTER TABLE trips DROP COLUMN IF EXISTS end_date;
ALTER TABLE trips DROP COLUMN IF EXISTS start_date;
ALTER TABLE trips DROP COLUMN IF EXISTS request_snapshot;
ALTER TABLE trips DROP COLUMN IF EXISTS plan_snapshot;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_DDL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_DDL)
