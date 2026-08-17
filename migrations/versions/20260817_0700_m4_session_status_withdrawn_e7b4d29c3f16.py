"""``SessionStatus.WITHDRAWN`` — the first value added after the conversion.

**Three ``CHECK`` swaps, and that is the whole migration.** Before
`d3a6f81b52c7` this would have been ``ALTER TYPE session_status ADD VALUE
'withdrawn'`` — one line, and permanent, because PostgreSQL has no
``DROP VALUE``. It is now reversible, which is exactly what settled decision #100
was for. This migration is the first evidence that the conversion bought
something rather than merely costing eight releases.

Three, not one, because a ``CHECK`` cannot span tables: ``sessions.status``,
``session_events.from_status`` and ``session_events.to_status`` each carry their
own copy of the vocabulary.

**What it means.** A mentee withdrawing a request the mentor has not answered
yet. It is deliberately *not* ``CANCELLED``: a confirmed session called off is
cancelled whoever calls it off, because the mentor has already committed time.
Collapsing the two would put a request nobody accepted into the same bucket as a
booking broken after agreement, and the two carry different policy for refunds
and for mentor-reliability statistics. The transition rule is
``PENDING_MENTOR_APPROVAL -> WITHDRAWN`` only, and it lives at the endpoint that
writes the status — nothing enforces transitions in the schema.

**``LIVE_STATUSES`` is untouched**, and that is the load-bearing part. A
withdrawn request holds no booking slot, so it sits outside the predicate with
``EXPIRED``, ``DECLINED`` and ``CANCELLED``: the mentor's time frees immediately
and ``sessions_no_mentor_double_booking`` ignores the row. Adding ``withdrawn``
to that predicate would keep a withdrawn request blocking the slot it just
released.

**The downgrade fails loudly if any row already holds the value**, and that is
correct rather than a defect. Reversing a vocabulary is only safe while nothing
uses it; a downgrade that silently rewrote live rows to some other status would
be destroying a fact. The failure is the signal to decide what those rows should
become.
"""

from __future__ import annotations

from alembic import op

revision = "e7b4d29c3f16"
down_revision = "d3a6f81b52c7"
branch_labels = None
depends_on = None

#: The vocabulary as it was before this migration, for ``downgrade`` only. The
#: new one is not repeated here — it lives in ``CONVERSIONS`` below, which is the
#: single copy ``upgrade`` uses and the pin test reads.
WITHOUT_WITHDRAWN = (
    "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
    "'declined', 'expired', 'no_show'"
)

#: **The labels are written out, not referenced.** `migration_tuples` parses this
#: structure with `ast` and collects only `ast.Constant` elements, so a
#: module-level name is invisible to it and the row silently collapses to four
#: items — the same class of trap as using a four-element tuple. Caught by
#: `test_the_converted_enum_labels_have_one_meaning`, which is what it is for.
CONVERSIONS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "sessions",
        "status",
        "session_status",
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show', 'withdrawn'",
        "pending_mentor_approval",
    ),
    (
        "session_events",
        "from_status",
        "session_status",
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show', 'withdrawn'",
        None,
    ),
    (
        "session_events",
        "to_status",
        "session_status",
        "'pending_mentor_approval', 'confirmed', 'completed', 'cancelled', "
        "'declined', 'expired', 'no_show', 'withdrawn'",
        None,
    ),
)


def _swap(table: str, column: str, labels: str) -> None:
    """Drop one ``CHECK`` and re-add it with the given vocabulary.

    Bare constraint names: ``op.drop_constraint`` and ``op.create_check_constraint``
    both run the name through the naming convention, so passing the rendered
    ``ck_sessions_status_is_known`` would produce a doubled prefix — the defect
    ``test_check_constraints_land_under_the_name_the_model_reports`` exists for.
    """
    op.drop_constraint(f"{column}_is_known", table, type_="check")
    op.create_check_constraint(f"{column}_is_known", table, f"{column} IN ({labels})")


def upgrade() -> None:
    for table, column, _type_name, labels, _default in CONVERSIONS:
        _swap(table, column, labels)


def downgrade() -> None:
    """Narrows the vocabulary again, and refuses if anything is using it.

    ``ADD CONSTRAINT`` validates existing rows, so a session already
    ``withdrawn`` makes this fail with *check constraint is violated by some
    row*. No ``ANALYZE`` and no rewrite: swapping a ``CHECK`` does not change the
    stored data, which is the difference between this and the eight migrations
    that converted the columns.
    """
    for table, column, _type_name, _labels, _default in CONVERSIONS:
        _swap(table, column, WITHOUT_WITHDRAWN)
