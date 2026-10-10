"""A booked session's intake answers, for the three people who may read them.

**Three readers, and the same three as the file behind a file answer**
(decision 210): the session's mentee, its mentor, and any live admin. The
check is in the query — the session's party predicate, `is_a_party`, the one
`GET /sessions/{id}` uses — so a caller outside the three reads exactly what a
caller holding a made-up id reads: nothing, which the route answers as 404.

**The question reads as it stands now, not as it stood at booking.** Nothing
keeps a copy of the wording when a mentee answers, so a mentor who rewords a
question after a booking sees the new wording beside the old answer. A *retired*
question is still shown, because retiring is a soft delete that keeps the row
for exactly this read, and it is flagged `retired`. The same holds for an
option's text. Whether to keep a copy at booking is #350's question.

**The rows and their folding are shared** with the preview every
``SessionRead`` carries (``session_answer_rows``), so the two cannot drift.

**Only answered questions are listed.** An optional question the mentee
skipped has no row, and what the form held at booking is not recorded, so the
current form cannot stand in for it: a question added since would read as
skipped when it was never asked.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.sessions import Session
from app.infra.db.session_answer_rows import answer_rows, fold_answers, form_rows, with_form
from app.infra.db.session_store import is_a_party

__all__ = ["session_answers"]


def _readable_by(caller_id: UUID, caller_is_admin: bool) -> Any:
    """The session's two parties, or an admin. One predicate for both reads."""
    return or_(is_a_party(caller_id), literal(caller_is_admin))


async def session_answers(
    session: AsyncSession, session_id: UUID, *, caller_id: UUID, caller_is_admin: bool
) -> list[dict[str, Any]] | None:
    """The booking's form and answers, in the order asked; ``None`` if unreadable.

    ``None`` is "no such session **or** not yours", which the route turns into
    one 404. A readable session with no form, or a migrated one, is an empty
    list: it exists and it was asked nothing.
    """
    readable = await session.scalar(
        select(Session.id).where(Session.id == session_id, _readable_by(caller_id, caller_is_admin))
    )
    if readable is None:
        return None

    # Scoped again here rather than trusting the check above: the rows are
    # personal data, and the predicate costs one join.
    scope = (Session.id == session_id, _readable_by(caller_id, caller_is_admin))
    rows = await session.execute(answer_rows(*scope))
    answers = fold_answers(rows.mappings()).get(session_id, [])
    # Every question of the form kept at booking, skipped ones included
    # (owner, 2026-10-10), read under the same scope as the answers.
    form = (await session.execute(form_rows(*scope))).mappings().all()
    return with_form(answers, form)
