"""Give the venue cascade a bottom, by removing the cascade.

D88 moves ``meeting_venue`` onto ``session_type_booking_configs`` and D21 says a
null there **means inherit**. Read together with D88's contract half — the
primary offering is what everything falls back to — that leaves a chain whose
terminus is ``mentor_profiles.default_meeting_venue``, which the contract step
drops.

**The terminus is reachable and empty**, which is why this revision exists rather
than being folded into the drop. ``trg_refuse_retiring_a_primary_offering``
makes retiring a primary offering an explicit two-step — release the pointer,
then retire — so a mentor with live offerings and a null
``primary_session_type_id`` is a state the guard *creates by design*. Resolving
that mentor's venue would walk config → primary → nothing, and
``SessionTypeRead.meeting_venue`` is a required field (D92).

Three ways out were available: make the response field optional (breaking a
shipped contract), forbid releasing the pointer while live offerings remain
(making "swap my primary" impossible without a single-statement swap), or give
every offering its own venue. This takes the third. **The cascade for venue
disappears** — there is nothing to inherit, so there is nothing to resolve to
null — and D21's null-means-inherit rule now applies to the degree short-form
column alone.

``requires_booking_confirmation`` arrived ``NOT NULL`` in the expand step for the
same reason stated differently: a boolean has no room for a third state. This
makes the pair consistent.

The backfill is defensive rather than expected. The expand step already wrote a
venue onto every config and the loader has written one since, so this should
touch nothing — but ``SET NOT NULL`` is the wrong place to discover otherwise.
"""

from __future__ import annotations

from alembic import op

revision = "b7f2e4c19a05"
down_revision = "d5a83b17c9e4"
branch_labels = None
depends_on = None

#: The column default for a config created without naming a venue. Matches
#: `mentor_profiles.default_meeting_venue`'s own server default, so the two
#: paths a config can arrive by agree.
FALLBACK = "google_meet"


def upgrade() -> None:
    # **Inner join, and no COALESCE, because the schema already guarantees both
    # halves.** The first draft had a LEFT JOIN and a fallback, on the premise
    # that `session_types.mentor_user_id` references `users` and so an offering
    # could belong to somebody with no mentor profile. It does not:
    # `fk_session_types_mentor_user_id_mentor_profiles` points at
    # `mentor_profiles`, so every offering has one, and that table's
    # `default_meeting_venue` is `NOT NULL`. The defence was for a row the
    # database refuses to create — dead code in the shape of caution, which is
    # the kind that survives review.
    op.execute(
        """
        UPDATE session_type_booking_configs c
           SET meeting_venue = mp.default_meeting_venue
          FROM session_types st
          JOIN mentor_profiles mp ON mp.user_id = st.mentor_user_id
         WHERE c.session_type_id = st.id
           AND c.meeting_venue IS NULL
        """
    )
    op.execute(
        f"ALTER TABLE session_type_booking_configs "
        f"ALTER COLUMN meeting_venue SET DEFAULT '{FALLBACK}'"
    )
    op.execute("ALTER TABLE session_type_booking_configs ALTER COLUMN meeting_venue SET NOT NULL")


def downgrade() -> None:
    """Reversible in shape, not in history.

    Which rows were null before the backfill is not recorded anywhere, and
    reconstructing it would mean re-nulling every config matching its mentor's
    default — destroying deliberate overrides that happen to agree. The values
    left behind are all correct resolved venues, so the column simply becomes
    permissive again.
    """
    op.execute("ALTER TABLE session_type_booking_configs ALTER COLUMN meeting_venue DROP NOT NULL")
    op.execute("ALTER TABLE session_type_booking_configs ALTER COLUMN meeting_venue DROP DEFAULT")
