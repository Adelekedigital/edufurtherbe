"""Internal notes admins leave on a mentor (Admin PRD, 2026-10-26; decision 298).

**Admins only**: the caller's grant is checked in `api.deps.require_admin`
before anything here runs, and no other module reads this table. Append-only:
a note is written once and never edited.

A note is about a mentor who exists (`mentor_exists()`: neither the profile nor
the account soft-deleted), checked in the writing statement itself. The author
is joined through `live()`, so an admin who later deleted their account keeps
their notes and loses their name, as a deleted reviewer does (decision 212).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Text, Uuid, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.infra.db.models.admin import MentorAdminNote
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import live
from app.infra.db.public_visibility import mentor_exists

__all__ = ["add_note", "list_notes"]

_AUTHOR = aliased(User, name="author")
_AUTHOR_PROFILE = aliased(UserProfile, name="author_profile")


def _with_author() -> Any:
    return (
        select(
            MentorAdminNote.id,
            MentorAdminNote.body,
            MentorAdminNote.created_at,
            MentorAdminNote.author_id,
            _AUTHOR.first_name.label("author_first_name"),
            _AUTHOR.last_name.label("author_last_name"),
            _AUTHOR_PROFILE.avatar_url.label("author_avatar_url"),
            _AUTHOR_PROFILE.cover_color.label("author_cover_color"),
        )
        .outerjoin(_AUTHOR, (_AUTHOR.id == MentorAdminNote.author_id) & live(_AUTHOR))
        .outerjoin(_AUTHOR_PROFILE, _AUTHOR_PROFILE.user_id == _AUTHOR.id)
    )


def _the_mentor(mentor: UUID, *columns: Any) -> Any:
    """``mentor``'s live profile, the one a note may be about, selecting ``columns``."""
    return (
        select(MentorProfile.user_id, *columns)
        .join(User, User.id == MentorProfile.user_id)
        .where(MentorProfile.user_id == mentor, *mentor_exists())
    )


async def list_notes(session: AsyncSession, mentor: UUID) -> list[dict[str, Any]] | None:
    """One mentor's notes, oldest first; ``None`` when there is no such mentor.

    Served by `ix_mentor_admin_notes_mentor`.
    """
    if await session.scalar(_the_mentor(mentor)) is None:
        return None
    statement = (
        _with_author()
        .where(MentorAdminNote.mentor_user_id == mentor)
        .order_by(MentorAdminNote.created_at, MentorAdminNote.id)
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]


async def add_note(
    session: AsyncSession, *, mentor: UUID, author: UUID, body: str
) -> dict[str, Any] | None:
    """Write one note; ``None`` when there is no such mentor. Does not commit.

    One `INSERT … SELECT` from the mentor's live profile, so a mentor deleted
    between the caller's check and this write gets no note rather than one
    about nobody.
    """
    inserted = (
        await session.execute(
            insert(MentorAdminNote)
            .from_select(
                ["mentor_user_id", "author_id", "body"],
                _the_mentor(mentor, literal(author, Uuid), literal(body, Text)),
            )
            .returning(MentorAdminNote.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return None
    row = (
        (await session.execute(_with_author().where(MentorAdminNote.id == inserted)))
        .mappings()
        .one()
    )
    return dict(row)
