"""Allow ``other`` as a session event's reason code.

Owner, 2026-10-10. A cancellation or decline reason becomes a choice a person
must make, from the coded list with **Other** last, so the reasons can be
analysed. A coded ``other`` rather than a null code keeps "none of these"
countable: a null reads the same as a migrated row or a client that sent
nothing, and the count of ``other`` is what says when a new reason has earned
its own code.

**A constraint swap, not a type change** (settled decision #100), the shape
``e7b4d29c3f16`` used to add ``withdrawn``: the column is ``text`` guarded by
``ck_session_events_reason_code_is_known``, which must name every member of
``SessionReasonCode`` (``test_schema_parity``). Widening it rewrites nothing and
only re-checks existing rows, which all hold old values.

Merged alone, before the code that lets a person pick ``other``
(project-conventions, *Migration ordering*). The enum gains the member in this
same PR, because the parity test needs model and database to agree, but no role
may submit it until the next PR, so this lands inert in either deploy order.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d8b3e1f7a24"
down_revision: str | Sequence[str] | None = "7c4e2a9f1d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The vocabulary before this migration, for ``downgrade``. The new one lives in
#: ``CONVERSIONS`` below, the single copy ``upgrade`` uses and the pin test reads.
WITHOUT_OTHER = (
    "'mentor_unavailable', 'mentee_no_longer_needed', 'scheduling_conflict', "
    "'technical_issue', 'mentor_no_show', 'mentee_no_show', "
    "'expired_no_response', 'rescheduled', 'admin_action'"
)

#: **The labels are written out, not referenced**: `migration_tuples` parses this
#: with `ast` and reads only constants (see `e7b4d29c3f16`).
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "session_events",
        "reason_code",
        "session_reason_code",
        "'mentor_unavailable', 'mentee_no_longer_needed', 'scheduling_conflict', "
        "'technical_issue', 'mentor_no_show', 'mentee_no_show', "
        "'expired_no_response', 'rescheduled', 'admin_action', 'other'",
        None,
    ),
)


def _swap(table: str, column: str, labels: str) -> None:
    """Drop one ``CHECK`` and re-add it with the given vocabulary. Bare names:
    the naming convention adds the ``ck_{table}_`` prefix."""
    op.drop_constraint(f"{column}_is_known", table, type_="check")
    op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    for table, column, _type_name, labels, _default in CONVERSIONS:
        _swap(table, column, labels)


def downgrade() -> None:
    """Narrow the vocabulary back, and **refuse** while anything uses ``other``.

    ``ADD CONSTRAINT`` validates existing rows, so an event already coded
    ``other`` makes this fail with *check constraint is violated by some row*,
    the behaviour ``e7b4d29c3f16`` chose for ``withdrawn``. Rewriting those codes
    to NULL would destroy the very data this column was widened to collect
    (Codex, #414); deciding what an ``other`` should become is an operator's
    data migration to write first, not something a downgrade does silently.
    """
    op.execute("SET lock_timeout = '3s'")
    _swap("session_events", "reason_code", WITHOUT_OTHER)
