"""M3 availability - recurring rules, dated exceptions, and btree_gist

Twelve legacy `CalendarSettings` columns become six. Four of them were the same
fact in different display formats — `12hr-localStartTime-TXT`,
`12hr-localEndTime-TXT` and their 24-hour twins — and a pre-formatted local
string for a *recurring* rule is exactly what breaks across DST.

That is measured here, not quoted from the package. In the dev export the four
TXT columns disagree with the stored time by **five hours on 12 of 24 rows**,
and those 12 are precisely the rows written before the calendar feature was
rewritten (created 2024-11 to 2025-02; the other 12 run 2025-09 onward and carry
a `timeZone` the older ones leave blank). Which of the two readings is a
mentor's real availability cannot be settled from dev data — no mentor there
owns both an old rule and a booking — so PR 3 quarantines them and the decision
is taken at the production extract, where 1,073 bookings can adjudicate it.

WHAT THIS MIGRATION IS AND IS NOT
=================================
Schema only. No transform, no loader, no endpoint. The tables land empty.

`calendar_connections` is **deliberately absent**, against the M1 migration's
statement that it "ships with M3". Nothing in M3 reads or writes it: ADR 0004
puts the free/busy read at slot render and at confirmation, both M4, and ADR
0012 — which decides the OAuth arrangement its columns encode — is still
Proposed and names two behaviours it has not tested. Settled decision #21
governs over #26's phase label: a table nothing writes is schema asserting an
implementation we do not have, which is the argument that already removed
`password_hash` and `auth_codes`.

btree_gist
==========
`ix_availability_exceptions_range` is GiST over `(uuid, daterange)`, and uuid has
**no GiST operator class** without `btree_gist`. Verified before relying on it,
in all three places the chain runs: the local `postgres:17` container (installs
clean, v1.7), the CI image (identical), and Supabase (already installed, v1.7,
in `public`, with `gist_uuid_ops` visible from the default search_path).

`CREATE EXTENSION IF NOT EXISTS` because Supabase already has it and the other
two do not. The downgrade does **not** drop it, following the precedent
`c707912915a9` set for pgcrypto and for the same reason: dropping an extension
is not symmetric with creating it, because other objects may have come to depend
on it in between.

DEPARTURES FROM THE PACKAGE, EACH DELIBERATE
============================================
1. `availability_exceptions` gains `deleted_at`. The package gives it to rules
   and withholds it from exceptions, which contradicts its own "soft delete
   everywhere" rule rather than deciding anything.
2. Index names take this repository's `ix_` prefix rather than the package's
   `idx_`, matching every index already in the chain.
3. `legacy_bubble_id` on exceptions is not a bare Bubble id. One `CalendarExtra`
   row holds a list of *discontiguous* dates — the dev export has one carrying
   Jan 13, Jan 19, Jan 21, Jun 21 and Jul 5 — so it fans out to one row per
   date and the anchor is `{bubble_id}:{iso_date}`. The field mapping's
   `block-Date(s) (list) -> date_range` implies 1:1 and the data is 1:N.

WHAT THE GATE CANNOT SEE
========================
`alembic check` reads tables, columns, types and regular indexes. Four objects
here sit outside that: two CHECK constraints per table, the partial predicate on
`ix_availability_rules_mentor`, and the GiST index's operator class.
`test_availability_schema` asserts each one, with an accepting case beside every
rejecting one.

Revision ID: a3f7c21d9e08
Revises: f2a8c31b7e45
Create Date: 2026-08-10 14:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a3f7c21d9e08"
down_revision: str | Sequence[str] | None = "f2a8c31b7e45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("availability_rules", "availability_exceptions")


def upgrade() -> None:
    """Upgrade schema."""
    # Both new tables are empty, so no ALTER waits on anything and no index build
    # blocks a write. The timeouts are set anyway: this same file runs against a
    # populated production database at cutover, where a lock queue behind a long
    # transaction stalls every SELECT on the table rather than only this
    # statement.
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '30s'")

    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # Created by hand and referenced with `create_type=False`, the pattern the
    # lookups migration records: without it `create_table` emits a second
    # `CREATE TYPE` and fails, and the matching `DROP TYPE` in downgrade would be
    # implied rather than visible in this file.
    op.execute("CREATE TYPE availability_exception_type AS ENUM ('block', 'override')")
    exception_type = postgresql.ENUM(
        "block", "override", name="availability_exception_type", create_type=False
    )

    op.create_table(
        "availability_rules",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_availability_rules")),
        sa.UniqueConstraint(
            "legacy_bubble_id", name=op.f("uq_availability_rules_legacy_bubble_id")
        ),
        sa.CheckConstraint(
            "day_of_week BETWEEN 0 AND 6", name=op.f("ck_availability_rules_day_of_week_valid")
        ),
        sa.CheckConstraint(
            "end_time > start_time",
            name=op.f("ck_availability_rules_availability_window_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_availability_rules_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_availability_rules_mentor",
        "availability_rules",
        ["mentor_user_id", "day_of_week"],
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    op.create_table(
        "availability_exceptions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("mentor_user_id", sa.Uuid(), nullable=False),
        sa.Column("type", exception_type, nullable=False),
        sa.Column("date_range", postgresql.DATERANGE(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("max_sessions_per_day", sa.Integer(), nullable=True),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("legacy_bubble_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_availability_exceptions")),
        sa.UniqueConstraint(
            "legacy_bubble_id", name=op.f("uq_availability_exceptions_legacy_bubble_id")
        ),
        sa.CheckConstraint(
            "(start_time IS NULL) = (end_time IS NULL)",
            name=op.f("ck_availability_exceptions_exception_times_paired"),
        ),
        sa.CheckConstraint(
            "start_time IS NULL OR end_time > start_time",
            name=op.f("ck_availability_exceptions_exception_window_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["mentor_user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_availability_exceptions_mentor_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_availability_exceptions_range",
        "availability_exceptions",
        ["mentor_user_id", "date_range"],
        postgresql_using="gist",
    )

    # Per table, in the migration that creates it (settled decision #23). A
    # blanket scanner is non-deterministic across environments and invisible in
    # a diff; this loop can be forgotten, which is why a test sweeps pg_trigger
    # for every table carrying `updated_at` rather than trusting it.
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    """Downgrade schema.

    Fully reversible — both tables land empty in this revision, so there is no
    data to lose. `btree_gist` is deliberately left installed; see the module
    docstring.
    """
    op.drop_index("ix_availability_exceptions_range", table_name="availability_exceptions")
    op.drop_table("availability_exceptions")
    op.drop_index("ix_availability_rules_mentor", table_name="availability_rules")
    op.drop_table("availability_rules")
    op.execute("DROP TYPE availability_exception_type")
