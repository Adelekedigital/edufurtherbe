"""The two free-text columns get vocabularies, and one becomes a foreign key.

``session_types.category`` and ``application_stage`` shipped as free ``text``
with no constraint and no value in any row, and were withheld from the public
contract because publishing them *"would commit this contract to a shape nobody
has designed"*. The UI designed both, so the reason for withholding them lapses
and the columns stop being free text.

**``application_stage`` becomes a closed set**, ``text`` + ``CHECK`` per #100,
with ``ApplicationStage`` at the Pydantic boundary. Five values, one of which is
``other`` — an escape hatch kept deliberately, because a vocabulary drawn from
fourteen mock screens rather than from data is expected to be incomplete, and a
closed set with no hatch pushes the unanticipated into whichever member is least
wrong.

**``custom_stage_label`` carries ``other``'s label, tied by a symmetric
``CHECK``.** ``other`` with no label renders a blank chip; a named stage carrying
a stale label is dead data that survives an edit. Both directions, for the same
reason ``mentor_conferencing_options.custom_url`` is symmetric — the
one-directional form is exactly what let a mentor be bookable with nowhere to
meet.

**``category`` becomes ``service_offering_id``, and the rename is not cosmetic.**
The value is a reference to the existing six-row ``service_offerings`` taxonomy,
which is the matching axis a mentee's need and a mentor's offer already join on
(#53) — so this is no new vocabulary. It cannot keep the name ``category``,
because ``service_offerings`` **has its own ``category`` column** — a display
grouping — and a foreign key called ``category`` pointing at a table with a
different ``category`` one join away is a trap laid for the next reader.
``mentor_service_offerings`` and ``mentee_goal_needs`` both call it
``service_offering_id``; this is the third.

**The backfill maps by slug and reports what it cannot map.** The column is null
on every row today: the ETL never writes it and no endpoint could until the
offering writes shipped one release ago — and *that* release accepts it as free
text, which is the window this defends against. Anything unmatched is nulled and
named rather than dropped silently, so a value a mentor typed is a line in the
migration output instead of a disappearance.

**Diverges from canonical.** ``04_sessions.sql`` specifies both as plain ``text``
and has no ``custom_stage_label`` at all. See ADR 0022, landing in this pull
request.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a71c9b382d"
down_revision: str | Sequence[str] | None = "d9e2b74c1f36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "fk_session_types_service_offering_id_service_offerings"

STAGES = "'early_exploration', 'drafting_stage', 'post_submission', 'revisions', 'other'"

#: Map whatever is in `category` onto the taxonomy, by slug. Case-folded and
#: trimmed: the column was free text, so a mentor typing "Document Preparation"
#: meant the row `document-preparation` names, and refusing to match it would
#: throw the value away over presentation.
BACKFILL_BY_SLUG = """
UPDATE session_types st
   SET service_offering_id = so.id
  FROM service_offerings so
 WHERE st.category IS NOT NULL
   AND lower(btrim(st.category)) = so.slug
"""

UNMAPPED = """
SELECT st.id, st.category
  FROM session_types st
 WHERE st.category IS NOT NULL
   AND st.service_offering_id IS NULL
"""

#: The inverse, for `downgrade`: the slug is what the column held.
RESTORE_CATEGORY = """
UPDATE session_types st
   SET category = so.slug
  FROM service_offerings so
 WHERE so.id = st.service_offering_id
"""


def upgrade() -> None:
    """Add, backfill, report, drop — and the report is between the last two."""
    op.add_column("session_types", sa.Column("custom_stage_label", sa.Text(), nullable=True))
    op.add_column("session_types", sa.Column("service_offering_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        FK_NAME,
        "session_types",
        "service_offerings",
        ["service_offering_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(BACKFILL_BY_SLUG)
    for row in op.get_bind().execute(sa.text(UNMAPPED)):
        print(
            f"UNMAPPED CATEGORY: session type {row[0]} held {row[1]!r}, which matches no "
            f"service offering slug. Cleared; re-classify it against the taxonomy."
        )
    op.drop_column("session_types", "category")

    # **`NOT VALID` is deliberately not used.** The table is small and the column
    # is null on every row, so validation is free — and a constraint that exists
    # without being validated is the shape that looks enforced and is not.
    # **`op.f()`, so the rendered name is used verbatim in both directions.**
    # Without it `op.create_check_constraint` runs the name through the naming
    # convention on the way in and `op.drop_constraint` runs it through again on
    # the way out, which is the doubling defect `d9e2b74c1f36` hit. Spelling the
    # rendered name here is also what
    # `test_every_schema_identifier_appears_verbatim_in_source` looks for — an
    # identifier that exists only in the database is one nobody can grep for, and
    # is how a silently truncated name shipped once already.
    op.create_check_constraint(
        op.f("ck_session_types_application_stage_is_known"),
        "session_types",
        f"application_stage IS NULL OR application_stage IN ({STAGES})",
    )
    op.create_check_constraint(
        op.f("ck_session_types_custom_stage_label_matches_stage"),
        "session_types",
        "(application_stage = 'other') = (custom_stage_label IS NOT NULL)",
    )


def downgrade() -> None:
    """Restore ``category`` from the slug, then drop what this added.

    **Not faithful for a value the backfill could not map**, and there is nowhere
    it could have been kept — the same absence the upgrade reports, showing up on
    the way back. A row whose free text matched no slug returns as null.

    The `CHECK`s go first: `custom_stage_label_matches_stage` names a column this
    then drops, and PostgreSQL would refuse the drop while it is referenced.

    **`op.f()` again**, matching `upgrade`. A bare name would work here too, but
    the two directions naming the constraint differently is how one of them
    silently stops matching — and the rendered form is the one a reader greps for.
    """
    op.drop_constraint(
        op.f("ck_session_types_custom_stage_label_matches_stage"), "session_types", type_="check"
    )
    op.drop_constraint(
        op.f("ck_session_types_application_stage_is_known"), "session_types", type_="check"
    )

    op.add_column("session_types", sa.Column("category", sa.Text(), nullable=True))
    op.execute(RESTORE_CATEGORY)

    op.drop_constraint(FK_NAME, "session_types", type_="foreignkey")
    op.drop_column("session_types", "service_offering_id")
    op.drop_column("session_types", "custom_stage_label")
