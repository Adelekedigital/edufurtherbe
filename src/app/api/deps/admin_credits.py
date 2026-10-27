"""The admin Credits page: find a mentee, their credits, the platform log, the rules.

Every read uses `CreditAdminDep`, the gate that writes grants: whoever may hand
out credits may see what a balance holds and how it moved.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.core import CreditAdminDep, LadderDep, SessionDep
from app.api.schemas.admin_credits import AdminMenteeRead, CreditEntryRead
from app.api.schemas.common import StorableText, decode_cursor, encode_cursor
from app.api.schemas.user import CreditsRead
from app.core.errors import NotFoundError
from app.domain.credit_log import FILTER_KINDS, CreditLogFilter
from app.domain.credits import CreditLadder
from app.infra.db.credit_log import credit_entries, find_mentees, get_mentee
from app.infra.db.credit_store import get_credit_summary

#: The longest mentee search accepted; a name or an email is far shorter.
MAX_QUERY_LENGTH = 200

#: Entries per page of the log and of a mentee's history.
ENTRIES_PER_PAGE = 50

Page = tuple[list[CreditEntryRead], str | None]


async def _with_credits(
    session: AsyncSession, row: dict[str, Any], ladder: CreditLadder
) -> AdminMenteeRead:
    """A mentee row with the card's credit block, from the card's own reader."""
    summary = await get_credit_summary(session, row["user_id"], ladder=ladder)
    return AdminMenteeRead(**row, credits=CreditsRead.model_validate(summary))


async def _entries(
    session: AsyncSession,
    *,
    cursor: str | None,
    chip: CreditLogFilter = "all",
    mentee_id: uuid.UUID | None = None,
) -> Page:
    rows, has_more = await credit_entries(
        session,
        limit=ENTRIES_PER_PAGE,
        after=decode_cursor(cursor),
        kinds=FILTER_KINDS[chip],
        mentee_id=mentee_id,
    )
    entries = [CreditEntryRead.from_row(row) for row in rows]
    if not (has_more and rows):
        return entries, None
    last = rows[-1]
    return entries, encode_cursor(last["created_at"].isoformat(), last["id"])


async def mentee_search(
    _: CreditAdminDep,
    session: SessionDep,
    ladder: LadderDep,
    q: Annotated[
        str | None,
        Query(description="Email, name or full user id. Empty: the recent mentees."),
        StringConstraints(max_length=MAX_QUERY_LENGTH),
        StorableText,
    ] = None,
) -> list[AdminMenteeRead]:
    rows = await find_mentees(session, (q or "").strip() or None)
    return [await _with_credits(session, row, ladder) for row in rows]


async def mentee_credits(
    user_id: uuid.UUID, _: CreditAdminDep, session: SessionDep, ladder: LadderDep
) -> tuple[AdminMenteeRead, Page]:
    row = await get_mentee(session, user_id)
    if row is None:
        raise NotFoundError("no such mentee")
    mentee = await _with_credits(session, row, ladder)
    return mentee, await _entries(session, cursor=None, mentee_id=user_id)


async def credit_log(
    _: CreditAdminDep,
    session: SessionDep,
    type: Annotated[  # noqa: A002 - the published query parameter
        CreditLogFilter, Query(description="Which entries: the log's chips.")
    ] = "all",
    cursor: Annotated[str | None, Query(description="From a previous `next_cursor`.")] = None,
    mentee_id: Annotated[
        uuid.UUID | None, Query(description="One mentee's entries: their history's next page.")
    ] = None,
) -> Page:
    return await _entries(session, cursor=cursor, chip=type, mentee_id=mentee_id)


def credit_rules(_: CreditAdminDep, ladder: LadderDep) -> CreditLadder:
    return ladder


MenteeSearchDep = Annotated[list[AdminMenteeRead], Depends(mentee_search)]
MenteeCreditsDep = Annotated[tuple[AdminMenteeRead, Page], Depends(mentee_credits)]
CreditLogDep = Annotated[Page, Depends(credit_log)]
CreditRulesDep = Annotated[CreditLadder, Depends(credit_rules)]
