"""The background mentee bio draft's read and its one write (decision 269).

**One predicate, `_unwritten`, for both** the re-read that decides whether a
draft is owed and the `WHERE` that guards the save, and `LIVE` for both too, so
an account deleted during the call gets nothing. A bio is unwritten while its
text is empty or blank and no draft has ever been recorded. Re-asserting it in
the write is what lets a person who typed a bio while the router was answering
win: their text makes the conditional write match nothing.

Every statement but `owed_bio_drafts` names the user the callback is about, and
only them; that one lists whom a one-off backfill queues.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.writing_assist import AssistResult
from app.infra.db.models.mentoring import MenteeGoal
from app.infra.db.models.user import User, UserOnboarding, UserProfile
from app.infra.db.predicates import LIVE, blank, has_mentor_profile

__all__ = ["bio_unwritten", "owed_bio_drafts", "save_bio_draft"]


def _unwritten() -> Any:
    return blank(UserProfile.about_me) & (UserProfile.about_me_drafted_at.is_(None))


def _bio_unwritten_for(user_id: Any) -> Any:
    """The user's bio is unwritten, or they have no profile row yet. ``user_id``
    is a value or a column, so the callback and the backfill read one rule."""
    mine = UserProfile.user_id == user_id
    return exists().where(mine, _unwritten()) | ~exists().where(mine)


async def bio_unwritten(session: AsyncSession, user_id: UUID) -> bool:
    """Whether a live account's bio is unwritten, or it has no profile row yet."""
    live = exists().where(User.id == user_id, LIVE)
    return bool((await session.execute(select(live & _bio_unwritten_for(user_id)))).scalar())


async def owed_bio_drafts(session: AsyncSession) -> list[UUID]:
    """Every live account that finished onboarding as a mentee (a goal and no
    mentor profile, `finishes_as_mentee`) and whose bio is still unwritten.

    For the one-off backfill of mentees who finished before drafts existed. It
    only chooses whom to queue: the callback re-checks each with the domain
    rule and `bio_unwritten`, and the save guards again, so the list going stale
    costs nothing.
    """
    finished = exists().where(
        UserOnboarding.user_id == User.id, UserOnboarding.completed_at.is_not(None)
    )
    goal = exists().where(MenteeGoal.user_id == User.id)
    rows = await session.scalars(
        select(User.id)
        .where(LIVE, finished, goal, ~has_mentor_profile(User.id), _bio_unwritten_for(User.id))
        .order_by(User.id)
    )
    return list(rows)


async def save_bio_draft(session: AsyncSession, user_id: UUID, draft: AssistResult) -> bool:
    """Save the draft with its provenance, only while the account is live and
    the bio unwritten. Returns whether it was saved. Does not commit.

    One statement. The row comes from `SELECT … FROM users WHERE id AND LIVE`
    and no mentor profile, so an account deleted, or turned mentor, during the
    call writes nothing: a mentor's About is theirs; a user with no
    profile gets one, as the profile writer does; an existing row is updated
    only where `_unwritten` still holds. The server stamps the time.
    """
    source = select(
        User.id,
        literal(draft.text),
        func.now(),
        literal(draft.prompt_version),
    ).where(User.id == user_id, LIVE, ~has_mentor_profile(User.id))
    columns = ["user_id", "about_me", "about_me_drafted_at", "about_me_prompt_version"]
    insert = pg_insert(UserProfile).from_select(columns, source)
    upsert = insert.on_conflict_do_update(
        index_elements=["user_id"],
        set_={name: insert.excluded[name] for name in columns[1:]},
        where=_unwritten(),
    ).returning(UserProfile.id)
    return (await session.execute(upsert)).first() is not None
