"""Conferencing becomes a mentor's own table, and the offering references it.

**``meeting_venue`` was a label, and half its vocabulary named capabilities we do
not have.** ``google_meet`` and ``daily`` are minted by the platform and need
nothing from a mentor; ``zoom`` has no integration and nothing can create one;
``custom`` needs a URL and ``custom_meeting_url`` was deleted by D88's contract
step. So two of four values could not produce a joinable session, and one column
could not tell *which provider* from *whether this mentor can host on it*.

**The key is composite, and that is why the reference sits on ``session_types``
rather than on the booking config.** A single-column foreign key is satisfied by
*any* option row, including another mentor's; the composite version makes that
unrepresentable rather than merely refused. ``session_types`` already carries
``mentor_user_id``, so it costs nothing there and would cost a denormalised
column or a trigger on the config. ``UNIQUE (user_id, id)`` on the options table
exists for no other reason than to be the target of that key.

**Resolution has three steps, not two**, and the third is not defensive padding:

    offering.conferencing_option_id  ->  that option
          | null
    the mentor's option WHERE is_default
          | missing
    'google_meet'                        <- platform fallback; never null

Seeding every mentor a default is not sufficient on its own. *"It cannot happen
because creation always sets it"* was true of ``primary_session_type_id`` right
up until the retirement trigger made release-then-retire a legal state, and the
venue cascade then had a reachable, empty bottom. ``SessionTypeRead.meeting_venue``
is a required field, so a resolution that can return null is a 500 waiting for
the first mentor who slips through. Seed **and** fall back.

**The backfill order is the correctness of this migration.** Options are seeded
from ``meeting_venue`` and every offering is pointed at its own row *before* the
column is dropped. Dropping first loses the only source, silently, with every row
count reconciling — and every migrated offering would then resolve to the
platform fallback, so mentors on ``daily`` would become Google Meet.

Each offering is pointed at its **own** option rather than left null to inherit.
Null-means-default is for offerings created from now on; relying on it here would
collapse a mentor's per-offering venues into one the moment a mentor has two.

**Offerings on ``custom`` are quarantined, and this is the one thing the backfill
cannot do faithfully.** The symmetric ``CHECK`` requires a URL, and there is no
source for one: ``mentor_profiles.custom_meeting_url`` was removed by
``a3c81f7e5b24`` *as a removal rather than a move, because nothing had ever
written it*, and the legacy transform still maps "external video tool" to
``custom``. So a ``custom`` option row is unsatisfiable. Rather than invent a URL,
relax the constraint the table exists to enforce, or silently rewrite the venue,
those mentors are seeded a ``google_meet`` default — which keeps them bookable —
and **named in the migration output** for a human to follow up. That is the
``CalendarSettings`` quarantine precedent (#81): a value that cannot be known is
reported rather than guessed.

**Diverges from canonical.** ``04_sessions.sql`` specifies
``session_type_booking_configs.meeting_venue`` as nullable-inherit. See the ADR
landing in this pull request.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e2b74c1f36"
down_revision: str | Sequence[str] | None = "c8f1a3e2b904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "fk_session_types_conferencing_option"

#: One option per distinct provider a mentor's live offerings actually use.
#: `custom` is excluded — see the module docstring. `deleted_at IS NULL` is
#: deliberately the only offering predicate: a paused offering still records a
#: venue the mentor chose, and filtering on `is_active` would discard it.
SEED_OPTIONS = """
INSERT INTO mentor_conferencing_options (user_id, provider, is_default)
SELECT DISTINCT st.mentor_user_id, c.meeting_venue, false
  FROM session_types st
  JOIN session_type_booking_configs c ON c.session_type_id = st.id
 WHERE st.deleted_at IS NULL
   AND c.meeting_venue <> 'custom'
"""

#: Every mentor whose offerings are on `custom` and who therefore has no option
#: yet. Seeded `google_meet` so they stay bookable; reported separately.
SEED_QUARANTINED = """
INSERT INTO mentor_conferencing_options (user_id, provider, is_default)
SELECT DISTINCT st.mentor_user_id, 'google_meet', false
  FROM session_types st
  JOIN session_type_booking_configs c ON c.session_type_id = st.id
 WHERE st.deleted_at IS NULL
   AND c.meeting_venue = 'custom'
   AND NOT EXISTS (
       SELECT 1 FROM mentor_conferencing_options o
        WHERE o.user_id = st.mentor_user_id
   )
"""

QUARANTINED_MENTORS = """
SELECT DISTINCT st.mentor_user_id
  FROM session_types st
  JOIN session_type_booking_configs c ON c.session_type_id = st.id
 WHERE st.deleted_at IS NULL
   AND c.meeting_venue = 'custom'
"""

#: Exactly one default per mentor, chosen by `id`. Deterministic across runs and
#: arbitrary among rows inserted in the same tick — acceptable because any
#: provider the mentor already uses is a valid default, and the partial unique
#: index is what makes "exactly one" true by construction rather than by this
#: statement being careful.
MARK_DEFAULTS = """
UPDATE mentor_conferencing_options o
   SET is_default = true
 WHERE o.id = (
       SELECT o2.id FROM mentor_conferencing_options o2
        WHERE o2.user_id = o.user_id
        ORDER BY o2.id
        LIMIT 1
   )
"""

#: Point each offering at its own option, so a mentor with two venues keeps both.
#: A `custom` offering matches nothing and stays null, which resolves through the
#: mentor's seeded default — the quarantine, expressed as data.
POINT_OFFERINGS = """
UPDATE session_types st
   SET conferencing_option_id = o.id
  FROM session_type_booking_configs c
  JOIN mentor_conferencing_options o ON o.user_id = (
       SELECT st2.mentor_user_id FROM session_types st2 WHERE st2.id = c.session_type_id
  )
 WHERE c.session_type_id = st.id
   AND o.provider = c.meeting_venue
"""

#: The inverse, for `downgrade`. An offering whose option was quarantined has a
#: null pointer and takes the column default rather than its original `custom`,
#: which is stated in `downgrade`'s docstring rather than silently accepted.
RESTORE_VENUE = """
UPDATE session_type_booking_configs c
   SET meeting_venue = o.provider
  FROM session_types st
  JOIN mentor_conferencing_options o ON o.id = st.conferencing_option_id
 WHERE c.session_type_id = st.id
"""


def upgrade() -> None:
    """Create, seed, point, then drop — and the order is not negotiable."""
    op.create_table(
        "mentor_conferencing_options",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("custom_url", sa.Text(), nullable=True),
        # Declared now, written by nothing yet. The table exists to hold these the
        # moment a provider needs authenticating; until then every row is a
        # preference and `status` is always `active`. Shipping them here rather
        # than later is the one place #21 is knowingly not applied, because the
        # alternative is a second migration on a table with a composite key
        # already pointed at it.
        sa.Column("external_account_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("connected_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            ["user_id"],
            ["mentor_profiles.user_id"],
            name=op.f("fk_mentor_conferencing_options_user_id_mentor_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_conferencing_options")),
        sa.UniqueConstraint(
            "user_id", "provider", name=op.f("uq_mentor_conferencing_options_user_id_provider")
        ),
        # Exists **only** so the composite foreign key below can reference it.
        # Redundant as a uniqueness claim — `id` is already the primary key — and
        # required by PostgreSQL, which needs a unique constraint on exactly the
        # referenced column pair.
        sa.UniqueConstraint(
            "user_id", "id", name=op.f("uq_mentor_conferencing_options_user_id_id")
        ),
        sa.CheckConstraint(
            "provider IN ('google_meet', 'daily', 'custom')",
            name=op.f("ck_mentor_conferencing_options_provider_is_known"),
        ),
        # **Symmetric, and that is the whole point.** The old
        # `ck_mentor_profiles_custom_url_requires_custom_venue` ran one direction
        # only — `custom_url IS NULL OR venue = 'custom'` — which permitted
        # `custom` with no URL and left a mentor bookable with nowhere to meet.
        sa.CheckConstraint(
            "(provider = 'custom') = (custom_url IS NOT NULL)",
            name=op.f("ck_mentor_conferencing_options_custom_url_matches_provider"),
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_mentor_conferencing_options_default "
        "ON mentor_conferencing_options (user_id) WHERE is_default"
    )
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON mentor_conferencing_options "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.add_column("session_types", sa.Column("conferencing_option_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        FK_NAME,
        "session_types",
        "mentor_conferencing_options",
        ["mentor_user_id", "conferencing_option_id"],
        ["user_id", "id"],
        ondelete="RESTRICT",
    )

    # Seed before dropping. Reversing these loses the only source of the value.
    op.execute(SEED_OPTIONS)
    op.execute(SEED_QUARANTINED)
    op.execute(MARK_DEFAULTS)
    op.execute(POINT_OFFERINGS)

    for row in op.get_bind().execute(sa.text(QUARANTINED_MENTORS)):
        print(
            f"QUARANTINED: mentor {row[0]} had an offering on 'custom' with no URL to carry. "
            f"Seeded google_meet so they stay bookable; their custom venue needs re-declaring."
        )

    # **Bare name, not the rendered one.** `op.drop_constraint` runs whatever it
    # is given through the naming convention, so passing
    # `ck_session_type_booking_configs_meeting_venue_is_known` produces
    # `ck_session_type_booking_configs_ck_session_type_booking_9308` — doubled,
    # then truncated and hashed past PostgreSQL's 63-byte limit. That is the
    # defect `test_check_constraints_land_under_the_name_the_model_reports`
    # exists for, and `_swap` in `e7b4d29c3f16` records it too.
    op.drop_constraint(
        "meeting_venue_is_known",
        "session_type_booking_configs",
        type_="check",
    )
    op.drop_column("session_type_booking_configs", "meeting_venue")


def downgrade() -> None:
    """Restore the column from the referenced option, then remove the table.

    **Not faithful for a quarantined offering, and it says so.** Its pointer is
    null because no ``custom`` option could be created, so it takes the column
    default of ``google_meet`` rather than the ``custom`` it held before the
    upgrade. There is nowhere for the original value to have been kept — that is
    the same absence the quarantine exists to report, showing up on the way back.
    """
    op.add_column(
        "session_type_booking_configs",
        sa.Column(
            "meeting_venue",
            sa.Text(),
            server_default=sa.text("'google_meet'"),
            nullable=False,
        ),
    )
    op.execute(RESTORE_VENUE)
    op.create_check_constraint(
        "meeting_venue_is_known",
        "session_type_booking_configs",
        "meeting_venue IN ('google_meet', 'daily', 'zoom', 'custom')",
    )

    op.drop_constraint(FK_NAME, "session_types", type_="foreignkey")
    op.drop_column("session_types", "conferencing_option_id")
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON mentor_conferencing_options")
    op.execute("DROP INDEX IF EXISTS ix_mentor_conferencing_options_default")
    op.drop_table("mentor_conferencing_options")
