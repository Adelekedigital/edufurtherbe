"""Writing the 53 migrated reviews, with Bubble's timestamps intact.

**One table, one idempotency key.** The ETL's recovery plan is a re-run, so the
write survives one: ``ON CONFLICT (legacy_bubble_id)`` updates in place rather
than inserting a second copy. The runbook rehearses twice, so that is the normal
path rather than an edge case.

**The trigger is held off, and the second load is why.** ``trg_set_updated_at``
is ``BEFORE UPDATE``, so a first load into an empty table preserves Bubble's
timestamps whether or not anything is disabled — a test that loads once and
checks the values passes against a loader with no ``DISABLE`` at all. It is the
*re-run* that breaks: the upsert takes its ``DO UPDATE`` branch, the trigger
fires, and every migrated ``updated_at`` becomes the import clock. `loader.py`
established this by deleting the ``DISABLE`` and watching which tests failed —
only the idempotence one did.

**Returns nothing.** What landed is read back by ``reconcile_reviews``; a count
handed over by the writer is the writer grading its own homework.

WHAT THE COLUMNS TAKE, AND WHY THEY ARE NOT NEGOTIATED HERE
===========================================================
``session_id`` is ``NULL`` and ``reviewed_for_role`` is ``mentor`` on every row.
Both are decisions the transform records and this module obeys; putting the
reasoning in two places is how the two come to disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from app.domain.transform.reviews import ReviewPlan
from app.infra.db.triggers import timestamps_from_source_across

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["STAMPED_TABLES", "ReviewLoader"]

#: The tables whose ``updated_at`` must come from the source rather than the
#: import clock. One here, and the tuple form is what
#: ``timestamps_from_source_across`` takes.
STAMPED_TABLES = ("reviews",)

UPSERT_REVIEW = """
INSERT INTO reviews (
    session_id, reviewed_by, reviewed_for, reviewed_for_role,
    communication_rating, knowledge_rating, practicality_rating, support_rating,
    valuable_rating, nps_recommend_score, public_review, private_review,
    legacy_bubble_id, created_at, updated_at
)
VALUES (
    NULL, :reviewed_by, :reviewed_for, 'mentor',
    :communication_rating, :knowledge_rating, :practicality_rating, :support_rating,
    :valuable_rating, :nps_recommend_score, :public_review, :private_review,
    :legacy_bubble_id, :created_at, :updated_at
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    reviewed_by = EXCLUDED.reviewed_by,
    reviewed_for = EXCLUDED.reviewed_for,
    communication_rating = EXCLUDED.communication_rating,
    knowledge_rating = EXCLUDED.knowledge_rating,
    practicality_rating = EXCLUDED.practicality_rating,
    support_rating = EXCLUDED.support_rating,
    valuable_rating = EXCLUDED.valuable_rating,
    nps_recommend_score = EXCLUDED.nps_recommend_score,
    public_review = EXCLUDED.public_review,
    private_review = EXCLUDED.private_review,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at
"""


class ReviewLoader:
    """One table, inside the caller's transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def load(self, *, users: dict[str, UUID], plan: ReviewPlan) -> None:
        """Write every planned review, resolving its two parties.

        **A party the users table does not know is a raise, not a skip.** The
        transform already refuses a review it cannot attribute, so reaching this
        means the transform and the database disagree about who exists —
        continuing would drop a review with nothing to show for it. The same call
        `SessionLoader` makes, for the same reason.

        ``session_id`` is not resolved because there is nothing to resolve it
        from: the legacy type carries no session link, so every row takes NULL.
        """

        def user_id(bubble_id: str, what: str) -> UUID:
            resolved = users.get(bubble_id)
            if resolved is None:
                message = f"{what} references unknown user {bubble_id}"
                raise LookupError(message)
            return resolved

        if not plan.reviews:
            return

        async with timestamps_from_source_across(self._connection, STAMPED_TABLES):
            await self._connection.execute(
                text(UPSERT_REVIEW),
                [
                    {
                        "reviewed_by": user_id(row.reviewed_by, "reviewedBy"),
                        "reviewed_for": user_id(row.reviewed_for, "reviewedFor"),
                        "communication_rating": row.communication_rating,
                        "knowledge_rating": row.knowledge_rating,
                        "practicality_rating": row.practicality_rating,
                        "support_rating": row.support_rating,
                        "valuable_rating": row.valuable_rating,
                        "nps_recommend_score": row.nps_recommend_score,
                        "public_review": row.public_review,
                        "private_review": row.private_review,
                        "legacy_bubble_id": row.legacy_bubble_id,
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                    }
                    for row in plan.reviews
                ],
            )
