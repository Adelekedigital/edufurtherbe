"""Every stored application stage becomes one of the design's six (decision 288).

Step 3a of the stage change, **data only**. Session Types PRD, founder
decisions 2026-10-21: no custom stage, and the founder's remap:

- ``early_exploration`` -> ``exploring``
- ``drafting_stage`` and ``revisions`` -> ``preparing_documents``
- ``post_submission`` -> ``awaiting_decision``
- ``interviewing`` -> ``interview_prep``
- ``other`` -> dropped, with its label

The reads have published exactly this since #536 (``current_stages``), so no
client sees a change. This rewrites the rows to match: a stage two retired
ones both became is kept once, at the first one's place, and positions are
renumbered from zero. ``session_types.application_stage`` becomes the set's
first again (or the remap of its own value, for a type with no set rows), and
every ``custom_stage_label`` is cleared.

**The CHECKs stay wide.** Narrowing them is step 3b, with the code that stops
knowing the retired values. Leaving them here means neither the code running
now nor the code after it can meet a row it does not expect, in either deploy
order. ``MAP`` is a literal copy of ``app.domain.sessions.RETIRED_STAGES``,
pinned by a test, because no migration imports from ``app``.

Only rows that change are touched, so ``updated_at`` moves for an offering
whose stages were remapped and for nothing else. Migrated session types never
held a stage, so no Bubble timestamp is overwritten.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d2e6f4a3c7"
down_revision: str | Sequence[str] | None = "a1f4c8e2d6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Each retired stage and the current one it became; `other` has none.
MAP = {
    "early_exploration": "exploring",
    "drafting_stage": "preparing_documents",
    "revisions": "preparing_documents",
    "post_submission": "awaiting_decision",
    "interviewing": "interview_prep",
    "other": None,
}


def _mapped(column: str) -> str:
    """`column` as its current stage, in SQL; a current stage maps to itself."""
    arms = " ".join(
        f"WHEN '{old}' THEN {'NULL' if new is None else repr(new)}" for old, new in MAP.items()
    )
    return f"(CASE {column} {arms} ELSE {column} END)"


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    # 1. `other` leaves every set.
    op.execute("DELETE FROM session_type_stages WHERE stage = 'other'")
    # 2. A row whose stage becomes the same one as an earlier row's goes, so the
    #    survivor keeps the first one's place and the pair index is never met.
    op.execute(
        "DELETE FROM session_type_stages AS later USING session_type_stages AS earlier "  # noqa: S608  # nosec B608 - literals from this file only
        "WHERE later.session_type_id = earlier.session_type_id "
        "AND earlier.position < later.position "
        f"AND {_mapped('earlier.stage')} = {_mapped('later.stage')}"
    )
    # 3. Each surviving retired stage becomes its current one.
    retired = ", ".join(repr(old) for old in MAP)
    op.execute(
        f"UPDATE session_type_stages SET stage = {_mapped('stage')} "  # noqa: S608  # nosec B608 - literals from this file only
        f"WHERE stage IN ({retired})"
    )
    # 4. Positions from zero again, in two steps so the position index is never
    #    met halfway: out of the way, then into place.
    op.execute(
        """
        WITH gapped AS (
            SELECT session_type_id FROM session_type_stages
            GROUP BY session_type_id
            HAVING max(position) <> count(*) - 1
        )
        UPDATE session_type_stages SET position = position + 1000000
        WHERE session_type_id IN (SELECT session_type_id FROM gapped)
        """
    )
    op.execute(
        """
        UPDATE session_type_stages AS s
        SET position = ranked.n
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY session_type_id ORDER BY position
            ) - 1 AS n
            FROM session_type_stages
        ) AS ranked
        WHERE ranked.id = s.id AND s.position >= 1000000
        """
    )
    # 5. The legacy column is the set's first, or its own value remapped when
    #    the type has no set rows; labels go in the same statement, because the
    #    CHECK tying `other` to its label is immediate.
    op.execute(
        "WITH target AS (SELECT t.id, CASE WHEN EXISTS ("  # noqa: S608  # nosec B608 - literals from this file only
        "SELECT 1 FROM session_type_stages s WHERE s.session_type_id = t.id) "
        "THEN (SELECT s.stage FROM session_type_stages s "
        "WHERE s.session_type_id = t.id ORDER BY s.position LIMIT 1) "
        f"ELSE {_mapped('t.application_stage')} END AS stage FROM session_types t) "
        "UPDATE session_types AS t "
        "SET application_stage = target.stage, custom_stage_label = NULL "
        "FROM target WHERE target.id = t.id "
        "AND (t.application_stage IS DISTINCT FROM target.stage "
        "OR t.custom_stage_label IS NOT NULL)"
    )


def downgrade() -> None:
    """Nothing to undo that can be undone.

    The previous revision accepts every value this one writes, so going back
    needs no change to the data; the retired stages, the dropped `other` rows
    and the cleared labels are not restored, because nothing recorded them.
    """
