"""Per-offering scheduling windows, which **replace** general availability.

An offering with windows is bookable in *those* and nowhere else. An offering
with none uses ``availability_rules``, exactly as everything did before this
migration — which is the regression that matters and is asserted as
byte-identical output rather than as a feature test.

**Intersecting was the obvious reading and is wrong**, and the mock's own example
shows why: Wednesday 5-8pm and Thursday 9am-1pm read as deliberate evening and
morning slots, and intersected with normal working hours they yield **zero**
slots — an empty calendar with nothing to explain it.

**``availability_exceptions`` still subtract, always.** Windows replace
*availability*, not *unavailability*: a mentor who blocked a date blocked it for
every offering.

**No canonical schema.** Per-session-type scheduling is one of only three things
in the UI the package does not specify, so this diverges by having no counterpart
rather than by contradicting one. See ADR 0023, landing in this pull request.

**Same shape as ``availability_rules``, deliberately.** ``bookable()`` takes a
list of weekly windows and does not care which table produced them, so "replace"
is a swap of the source rather than a second path through the slot maths — which
is what keeps a tested read path tested.

The exclusion constraint is scoped to the **offering** rather than the mentor,
which is the one meaningful difference: two offerings may legitimately cover the
same hours, and two windows on one offering covering the same hours is a
duplicate the grid would count twice.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2d64f87b1e5"
down_revision: str | Sequence[str] | None = "f1c83d5a2e94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One table, additive. Nothing existing changes shape."""
    op.create_table(
        "session_type_scheduling_windows",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("session_type_id", sa.Uuid(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_type_id"],
            ["session_types.id"],
            name=op.f("fk_session_type_scheduling_windows_session_type_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_type_scheduling_windows")),
        sa.CheckConstraint(
            "day_of_week BETWEEN 0 AND 6",
            name=op.f("ck_session_type_scheduling_windows_day_of_week_valid"),
        ),
        sa.CheckConstraint(
            "end_time > start_time",
            name=op.f("ck_session_type_scheduling_windows_scheduling_window_ordered"),
        ),
    )
    op.create_index(
        "ix_session_type_scheduling_windows_offering",
        "session_type_scheduling_windows",
        ["session_type_id", "day_of_week"],
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )
    # `timerange` is this schema's own range type, created in M3 alongside
    # `availability_rules_no_overlap`. `btree_gist` is what lets a uuid sit in a
    # GiST index beside a range, and M3 installed it.
    op.execute(
        "ALTER TABLE session_type_scheduling_windows "
        "ADD CONSTRAINT session_type_scheduling_windows_no_overlap "
        "EXCLUDE USING gist ("
        "  session_type_id WITH =, day_of_week WITH =, "
        "  timerange(start_time, end_time, '[)') WITH &&"
        ") WHERE (is_active AND deleted_at IS NULL)"
    )
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON session_type_scheduling_windows "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    """The table goes whole. Every offering falls back to general availability,
    which is what an offering with no windows already does — so a downgrade
    changes *which* slots are offered without leaving anything unreadable."""
    op.drop_table("session_type_scheduling_windows")
