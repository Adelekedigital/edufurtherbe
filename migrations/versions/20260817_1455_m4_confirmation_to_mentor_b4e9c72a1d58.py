"""``requires_booking_confirmation`` returns to ``mentor_profiles``.

**D88 is reversed for this field, and the premise is what failed rather than the
placement.** D88 said *an offering without its own X uses the primary offering's
X* — one fallback mechanism covering three fields. Each field then went somewhere
else on contact: ``meeting_venue`` became per-offering and ``NOT NULL`` because
its cascade had a reachable, empty bottom (#102); ``custom_meeting_url`` was
deleted rather than moved, because nothing read it; and this one comes back to
the mentor.

**The D88 argument against a nullable boolean does not apply here, and that is
the whole point.** It said *a boolean has no room for a third state, so null
would mean inherit and be indistinguishable from false* — true when inheriting
from a **primary offering**, because a mentor may legitimately hold live
offerings and no primary, so the chain bottoms out at nothing. A mentor row
always exists. The terminus cannot be missing, which is exactly what killed the
venue cascade and exactly what this one has.

**The order of the four statements is the whole correctness of this migration.**
The mentor column is seeded from the configs *before* the configs are cleared. A
reader who reverses those two loses every mentor's setting silently, with the
row count unchanged and every constraint satisfied.

**The seed is ``bool_or`` over the mentor's non-deleted offerings**, not the
primary offering's value and not ``false``.

- Not ``false``: every mentor who required confirmation would silently stop
  requiring it, which is the one regression this migration can cause. A test
  upgrades to `e7b4d29c3f16`, writes offerings at ``true``, and upgrades.
- Not the primary offering: ``primary_session_type_id`` is dropped by the very
  next migration, and a mentor mid-way through the sanctioned
  release-then-retire two-step has no primary at all, so that reading seeds
  ``false`` for exactly the mentors who are reconfiguring.
- ``is_active`` is deliberately **not** in the predicate, though
  ``_fan_out_booking_confirmation`` only ever wrote to live offerings. A mentor
  with every offering paused still chose a booking policy, and filtering on
  ``is_active`` would discard it. ``bool_or`` over nothing is null, so
  ``COALESCE(..., false)`` carries the mentor with no offerings at all — which
  #57 already settles as ``false``.

**The config column keeps the value nobody reads for the length of one
deployment window, and then does not.** Clearing it is the point rather than
tidiness: two columns holding one fact is the duplication non-negotiable #8 calls
a defect, and it is what D88 produced in the other direction. Nothing reads the
config column after this release — the per-offering override it now expresses
has no consumer until an endpoint can set one, which is #21.

**The window is named rather than wished away.** Migrations are dispatched by
hand and never run on startup (ADR 0017), so there is an interval where this
schema meets the previously deployed code. That code reads the config column and
will find null, which ``MentorProfileRead.requires_booking_confirmation`` already
types ``bool | None`` — so it degrades to reporting *no booking policy* rather
than raising. A profile ``PATCH`` landing inside that window still runs the old
fan-out and writes a per-offering override; the value it writes is the mentor's
own choice, so the resolved answer stays right and the stale override is what the
next release's writer replaces.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e9c72a1d58"
down_revision: str | Sequence[str] | None = "e7b4d29c3f16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Seed the mentor from the offerings. ``bool_or`` returns null over an empty
#: set, which is a mentor with no offerings and not a mentor who chose ``false``
#: — the ``COALESCE`` is what makes the column ``NOT NULL``-able immediately.
SEED_THE_MENTOR = """
UPDATE mentor_profiles mp
   SET requires_booking_confirmation = COALESCE((
           SELECT bool_or(c.requires_booking_confirmation)
             FROM session_type_booking_configs c
             JOIN session_types st ON st.id = c.session_type_id
            WHERE st.mentor_user_id = mp.user_id
              AND st.deleted_at IS NULL
       ), false)
"""

#: The inverse, for ``downgrade``. Only rows that are inheriting are written:
#: a row holding an explicit override is already the value the old schema wants,
#: and overwriting it would discard a deliberate per-offering choice.
RESTORE_THE_CONFIGS = """
UPDATE session_type_booking_configs c
   SET requires_booking_confirmation = mp.requires_booking_confirmation
  FROM session_types st
  JOIN mentor_profiles mp ON mp.user_id = st.mentor_user_id
 WHERE c.session_type_id = st.id
   AND c.requires_booking_confirmation IS NULL
"""


def upgrade() -> None:
    """Expand, seed, widen, clear — in that order."""
    op.add_column(
        "mentor_profiles",
        sa.Column(
            "requires_booking_confirmation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    # Before anything touches the configs. Reversing these two is silent.
    op.execute(SEED_THE_MENTOR)

    # Null now means *inherit from the mentor*. The server default goes with the
    # NOT NULL: a default of `false` under a nullable column would make every
    # inserted row an override that happens to agree, which is the state this
    # reversal exists to remove.
    op.alter_column(
        "session_type_booking_configs",
        "requires_booking_confirmation",
        existing_type=sa.Boolean(),
        nullable=True,
        server_default=None,
    )
    op.execute("UPDATE session_type_booking_configs SET requires_booking_confirmation = NULL")


def downgrade() -> None:
    """Push the mentor's value back down onto every inheriting offering.

    **Not symmetrical with ``upgrade``, deliberately.** ``upgrade`` reduces many
    offerings to one mentor value; the reverse fans one value back out, and an
    offering that carried a genuine override is left holding it. The mentor
    column is the only thing lost, and by this point it is a copy.

    The sweep before ``SET NOT NULL`` is not defensive noise: the restore joins
    through ``session_types`` to ``mentor_profiles``, so a config whose offering
    outlived its mentor profile would stay null and the ``ALTER`` would fail on
    it. Foreign keys make that unreachable today; the ``ALTER`` failing at 2am
    because it became reachable is not the way to find out.
    """
    op.execute(RESTORE_THE_CONFIGS)
    op.execute(
        "UPDATE session_type_booking_configs "
        "SET requires_booking_confirmation = false "
        "WHERE requires_booking_confirmation IS NULL"
    )
    op.alter_column(
        "session_type_booking_configs",
        "requires_booking_confirmation",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )
    op.drop_column("mentor_profiles", "requires_booking_confirmation")
