"""Booking window and break-after: mentor defaults, per-offering overrides.

Settled decision #204, Session Types frontend #13. Four nullable integer columns:
``booking_window_days`` and ``break_after_minutes`` on ``mentor_profiles`` (the
mentor's defaults) and on ``session_type_booking_configs`` (an offering's
override). Null means inherit: offering, then mentor, then the platform's own
(the full horizon, no break). The CHECKs are sanity only; the product ranges are
the API's (#104).

Additive and nullable, metadata-only; old code never reads the columns, and a
null everywhere reproduces today's slots exactly, so both versions serve.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6c148bef8b52"
down_revision: str | Sequence[str] | None = "390e11db5980"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Each table and its two constraint names, **written out**: a name built at run
#: time appears nowhere in source, and the identifier test that catches names
#: truncated past 63 bytes could not find it.
TABLES = {
    "mentor_profiles": (
        "ck_mentor_profiles_booking_window_days_sane",
        "ck_mentor_profiles_break_after_minutes_sane",
    ),
    "session_type_booking_configs": (
        "ck_session_type_booking_configs_booking_window_days_sane",
        "ck_session_type_booking_configs_break_after_minutes_sane",
    ),
}


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for table, (window_check, break_check) in TABLES.items():
        op.add_column(table, sa.Column("booking_window_days", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("break_after_minutes", sa.Integer(), nullable=True))
        op.create_check_constraint(
            op.f(window_check),
            table,
            "booking_window_days IS NULL OR booking_window_days BETWEEN 1 AND 365",
        )
        op.create_check_constraint(
            op.f(break_check),
            table,
            "break_after_minutes IS NULL OR break_after_minutes BETWEEN 0 AND 1440",
        )


def downgrade() -> None:
    """Drop the columns. Every offering goes back to the full horizon and no break."""
    op.execute("SET lock_timeout = '3s'")
    for table, (window_check, break_check) in TABLES.items():
        op.drop_constraint(op.f(break_check), table, type_="check")
        op.drop_constraint(op.f(window_check), table, type_="check")
        op.drop_column(table, "break_after_minutes")
        op.drop_column(table, "booking_window_days")
