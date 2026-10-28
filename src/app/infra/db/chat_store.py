"""Chats: who the caller may message, their inbox, a thread, and the counts.

**M0 is `connections()`, and nothing else says who is connected** (ADR 0010):
the people the caller is the mentor or the mentee of in a session whose status
is in `domain.chats.CONNECTING_STATUSES`. Every read and write starts from it,
in the statement (non-negotiable #5), so a chat between two people who never
booked is unreachable, and a stranger's chat with somebody else is absent
rather than fetched and refused.

**The person is joined through `live()`, outer**, as a session's party is: a
person who closed their account keeps their place in the chat and loses their
name, avatar, cover and school, with `deleted` saying why.

Writes are in `chat_writer`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Select,
    Text,
    and_,
    case,
    exists,
    false,
    func,
    literal,
    or_,
    select,
    true,
    tuple_,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import NotFoundError, ValidationError
from app.domain.chats import (
    CONNECTING_STATUSES,
    SESSION_WINDOW,
    SESSION_WINDOW_STATUSES,
    ChatRole,
)
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.messaging import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageAttachment,
)
from app.infra.db.models.sessions import Session, SessionType
from app.infra.db.models.user import User, UserProfile
from app.infra.db.pending_requests import upcoming_session
from app.infra.db.predicates import LIVE, mentor_paused, name_like
from app.infra.db.public_visibility import mentor_is_live
from app.infra.db.qualifications import top_qualification

__all__ = [
    "FILE_COLUMNS",
    "MESSAGE_COLUMNS",
    "chat_of",
    "chat_rows",
    "connections",
    "message_select",
    "not_connected",
    "pair",
    "people",
    "thread_page",
    "unread_count",
    "unread_total",
]

_MINE = aliased(ConversationParticipant, name="mine")
_THEIRS = aliased(ConversationParticipant, name="theirs")

#: What a message is read as, everywhere.
MESSAGE_COLUMNS = (
    Message.id,
    Message.conversation_id,
    Message.client_id,
    Message.sender_id,
    Message.message_type,
    Message.body,
    Message.sent_at,
    Message.edited_at,
    Message.deleted_at,
)

#: The file a message carries, read beside it; all null for text alone.
FILE_COLUMNS = (
    MessageAttachment.id.label("file_id"),
    MessageAttachment.file_name.label("file_name"),
    MessageAttachment.size_bytes.label("file_size"),
    MessageAttachment.mime_type.label("file_content_type"),
)


def message_select() -> Select[Any]:
    """**Messages as every read sees them**: the message and its file. One file
    per message at most (`uq_message_attachments_message_id`), so the join
    never multiplies a row."""
    return (
        select(*MESSAGE_COLUMNS, *FILE_COLUMNS)
        .select_from(Message)
        .outerjoin(MessageAttachment, MessageAttachment.message_id == Message.id)
    )


def between(a: Any, b: Any) -> Any:
    """A session between these two people, whichever is the mentor."""
    return or_(
        and_(Session.mentor_id == a, Session.mentee_id == b),
        and_(Session.mentor_id == b, Session.mentee_id == a),
    )


def connections(me: UUID, other: UUID | None = None) -> Any:
    """**M0, the one statement of it**: one row per person ``me`` is connected
    with (or only ``other``), as a subquery.

    ``other_id`` is the person; ``booked_at`` their newest booking with ``me``
    (the inbox orders people with no messages by it: a booking is the act of
    claiming a slot, so its time is the session's `created_at`);
    ``session_role`` what they were in that booking.
    """
    as_mentor = Session.mentor_id == me
    person = case((as_mentor, Session.mentee_id), else_=Session.mentor_id)
    their_role = case(
        (as_mentor, literal(ChatRole.MENTEE.value)), else_=literal(ChatRole.MENTOR.value)
    )
    newest_role = func.array_agg(
        aggregate_order_by(their_role, Session.created_at.desc(), Session.id.desc()),
        type_=ARRAY(Text),
    )[1]
    scope = between(me, other) if other is not None else or_(as_mentor, Session.mentee_id == me)
    return (
        select(
            person.label("other_id"),
            func.max(Session.created_at).label("booked_at"),
            newest_role.label("session_role"),
        )
        .where(scope, Session.status.in_(CONNECTING_STATUSES))
        .group_by(person)
        .subquery("connection")
    )


def pair(me: Any, other: Any) -> Any:
    """The chat of ``me`` and ``other``, matched as `uq_conversations_pair` is
    declared, so the lookup rides that index. Symmetric in its arguments."""
    return and_(
        func.least(Conversation.mentor_user_id, Conversation.mentee_user_id)
        == func.least(other, me),
        func.greatest(Conversation.mentor_user_id, Conversation.mentee_user_id)
        == func.greatest(other, me),
    )


def unread_count(me: UUID, conversation_id: Any, last_read_at: Any) -> Any:
    """**The one statement of unread**: the other person's messages in this chat
    after ``last_read_at`` (all of them while it is null), not deleted. Served
    by `ix_messages_conversation_sent`."""
    return (
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_id != me,
            Message.deleted_at.is_(None),
            or_(last_read_at.is_(None), Message.sent_at > last_read_at),
        )
        .correlate_except(Message)
        .scalar_subquery()
    )


def _statement(me: UUID, now: dt.datetime, other: UUID | None = None) -> Select[Any]:
    """One row per connected person: the person, the chat if there is one, its
    newest message, the caller's unread count and the next booking.

    Every join yields at most one row per person (a lateral `limit 1`, a
    unique key, or the pair index), so nothing multiplies the list.
    """
    link = connections(me, other)
    # The chat's roles once it exists, else the newest booking's.
    role = case(
        (Conversation.mentor_user_id == link.c.other_id, literal(ChatRole.MENTOR.value)),
        (Conversation.mentee_user_id == link.c.other_id, literal(ChatRole.MENTEE.value)),
        else_=link.c.session_role,
    )
    school = top_qualification(User.id, name="chat_school")
    last = (
        message_select()
        .where(Message.conversation_id == Conversation.id)
        .order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(1)
        .lateral("last_message")
    )
    booking = (
        select(
            Session.id.label("id"),
            Session.starts_at.label("starts_at"),
            SessionType.name.label("session_name"),
        )
        # Outer: a migrated session may have no type yet.
        .outerjoin(SessionType, SessionType.id == Session.session_type_id)
        .where(between(me, link.c.other_id), *upcoming_session(now))
        .order_by(Session.starts_at, Session.id)
        .limit(1)
        .lateral("next_booking")
    )
    in_window = exists().where(
        between(me, link.c.other_id),
        Session.status.in_(SESSION_WINDOW_STATUSES),
        Session.starts_at >= now - SESSION_WINDOW,
        Session.starts_at <= now + SESSION_WINDOW,
    )
    is_mentor = role == ChatRole.MENTOR.value
    return (
        select(
            link.c.other_id.label("id"),
            link.c.booked_at,
            role.label("role"),
            # A paused mentor, by themselves or an admin; never a mentee who
            # is also a paused mentor elsewhere.
            func.coalesce(and_(is_mentor, *mentor_paused()), false()).label("paused"),
            case((and_(is_mentor, *mentor_is_live()), User.slug), else_=None).label("handle"),
            # The person's id is NOT NULL with a foreign key, so the only way
            # the outer join finds nobody is `live()` refusing them.
            User.id.is_(None).label("deleted"),
            User.first_name,
            User.last_name,
            UserProfile.avatar_url,
            UserProfile.avatar_focus_x,
            UserProfile.avatar_focus_y,
            UserProfile.cover_color,
            UserProfile.cover_art,
            school.c.degree,
            school.c.institution,
            Conversation.id.label("conversation_id"),
            _MINE.last_read_at.label("my_last_read_at"),
            _THEIRS.last_read_at.label("their_last_read_at"),
            *(column.label(f"last_{column.key}") for column in last.c),
            unread_count(me, Conversation.id, _MINE.last_read_at).label("unread"),
            booking.c.id.label("next_booking_id"),
            booking.c.starts_at.label("next_booking_starts_at"),
            booking.c.session_name.label("next_booking_session_name"),
            in_window.label("session_window"),
        )
        .select_from(link)
        .outerjoin(User, and_(User.id == link.c.other_id, LIVE))
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .outerjoin(MentorProfile, MentorProfile.user_id == User.id)
        .outerjoin(school, true())
        .outerjoin(Conversation, pair(me, link.c.other_id))
        .outerjoin(_MINE, and_(_MINE.conversation_id == Conversation.id, _MINE.user_id == me))
        .outerjoin(
            _THEIRS,
            and_(
                _THEIRS.conversation_id == Conversation.id,
                _THEIRS.user_id == link.c.other_id,
            ),
        )
        .outerjoin(last, true())
        .outerjoin(booking, true())
        .order_by(
            # Newest message first; then people with no messages, newest
            # booking first (Messages PRD); the id makes the order total.
            last.c.sent_at.desc().nullslast(),
            link.c.booked_at.desc(),
            link.c.other_id,
        )
    )


async def chat_rows(session: AsyncSession, me: UUID, now: dt.datetime) -> list[dict[str, Any]]:
    """The inbox: one row per connected person, in the PRD's order."""
    return [dict(row) for row in (await session.execute(_statement(me, now))).mappings()]


async def chat_of(
    session: AsyncSession, me: UUID, other: UUID, now: dt.datetime
) -> dict[str, Any] | None:
    """The chat with ``other``, as an inbox row, or ``None`` when the two are
    not connected (including when ``other`` is nobody, or ``me``)."""
    row = (await session.execute(_statement(me, now, other))).mappings().first()
    return dict(row) if row is not None else None


async def people(
    session: AsyncSession, me: UUID, now: dt.datetime, q: str | None
) -> list[dict[str, Any]]:
    """Everyone ``me`` is connected with, by name, for New message.

    ``q`` matches a live person's name, ignoring case: a closed account's
    stored name is never searched, since it is never shown.
    """
    statement = _statement(me, now).order_by(None)
    if q:
        statement = statement.where(name_like(User, q))
    statement = statement.order_by(
        User.first_name.asc().nullslast(), User.last_name.asc().nullslast(), "id"
    )
    return [dict(row) for row in (await session.execute(statement)).mappings()]


async def unread_total(session: AsyncSession, me: UUID) -> int:
    """The badge: unread across every chat ``me`` may read. The same
    connections and the same count as the inbox's rows, so it is their sum."""
    link = connections(me)
    statement = (
        select(func.coalesce(func.sum(unread_count(me, Conversation.id, _MINE.last_read_at)), 0))
        .select_from(link)
        .join(Conversation, pair(me, link.c.other_id))
        .outerjoin(_MINE, and_(_MINE.conversation_id == Conversation.id, _MINE.user_id == me))
    )
    return int((await session.execute(statement)).scalar_one())


async def _position(
    session: AsyncSession, conversation_id: UUID | None, message_id: UUID, pointer: str
) -> tuple[dt.datetime, UUID]:
    """A cursor message's place in this chat, or a 422 when it is not in it."""
    sent_at = (
        await session.execute(
            select(Message.sent_at).where(
                Message.id == message_id, Message.conversation_id == conversation_id
            )
        )
    ).scalar_one_or_none()
    if sent_at is None:
        raise ValidationError(
            "no such message in this chat", field_errors=((pointer, "not a message in this chat"),)
        )
    return sent_at, message_id


async def thread_page(
    session: AsyncSession,
    conversation_id: UUID | None,
    *,
    before: UUID | None,
    after: UUID | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of a chat, oldest first, and whether there is more.

    With no cursor, the newest ``limit``; with ``before``, the ``limit`` just
    older than it; with ``after``, the ``limit`` just newer. ``has_more`` says
    whether more exist past the page in the direction it was read: older for
    the first two, newer for ``after``. ``limit`` 0 reads no rows and still
    answers ``has_more``. ``conversation_id`` is the caller's own chat, found
    through M0 by the caller.
    """
    cursor = None
    if before is not None:
        cursor = await _position(session, conversation_id, before, "/before")
    elif after is not None:
        cursor = await _position(session, conversation_id, after, "/after")
    if conversation_id is None:
        return [], False

    key = tuple_(Message.sent_at, Message.id)
    statement = message_select().where(Message.conversation_id == conversation_id)
    if after is not None and cursor is not None:
        statement = statement.where(key > tuple_(literal(cursor[0]), literal(cursor[1])))
        statement = statement.order_by(Message.sent_at, Message.id)
    else:
        if cursor is not None:
            statement = statement.where(key < tuple_(literal(cursor[0]), literal(cursor[1])))
        statement = statement.order_by(Message.sent_at.desc(), Message.id.desc())
    rows = [dict(row) for row in (await session.execute(statement.limit(limit + 1))).mappings()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after is None:
        rows.reverse()
    return rows, has_more


def not_connected() -> NotFoundError:
    """A read of a chat the caller may not have: the same answer for a
    stranger, nobody and yourself."""
    return NotFoundError("no such chat")
