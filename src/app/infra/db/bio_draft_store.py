"""The background mentee bio draft's read and its one write (decision 269).

**One predicate, `_unwritten`, for both** the re-read that decides whether a
draft is owed and the `WHERE` that guards the save, and `LIVE` for both too, so
an account deleted during the call gets nothing. A bio is unwritten while its
text is empty or blank and no draft has ever been recorded. Re-asserting it in
the write is what lets a person who typed a bio while the router was answering
win: their text makes the conditional write match nothing.

Every statement names the user the callback is about, and only them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.writing_assist import AssistResult
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import LIVE

__all__ = ["bio_unwritten", "save_bio_draft"]


def _unwritten() -> Any:
    return (func.coalesce(func.btrim(UserProfile.about_me), "") == "") & (
        UserProfile.about_me_drafted_at.is_(None)
    )


async def bio_unwritten(session: AsyncSession, user_id: UUID) -> bool:
    """Whether a live account's bio is unwritten, or it has no profile row yet."""
    mine = UserProfile.user_id == user_id
    unwritten = exists().where(mine, _unwritten()) | ~exists().where(mine)
    live = exists().where(User.id == user_id, LIVE)
    return bool((await session.execute(select(live & unwritten))).scalar())


async def save_bio_draft(session: AsyncSession, user_id: UUID, draft: AssistResult) -> bool:
    """Save the draft with its provenance, only while the account is live and
    the bio unwritten. Returns whether it was saved. Does not commit.

    One statement. The row comes from `SELECT … FROM users WHERE id AND LIVE`,
    so an account deleted during the call writes nothing; a user with no
    profile gets one, as the profile writer does; an existing row is updated
    only where `_unwritten` still holds. The server stamps the time.
    """
    source = select(
        User.id,
        literal(draft.text),
        func.now(),
        literal(draft.prompt_version),
    ).where(User.id == user_id, LIVE)
    columns = ["user_id", "about_me", "about_me_drafted_at", "about_me_prompt_version"]
    insert = pg_insert(UserProfile).from_select(columns, source)
    upsert = insert.on_conflict_do_update(
        index_elements=["user_id"],
        set_={name: insert.excluded[name] for name in columns[1:]},
        where=_unwritten(),
    ).returning(UserProfile.id)
    return (await session.execute(upsert)).first() is not None
