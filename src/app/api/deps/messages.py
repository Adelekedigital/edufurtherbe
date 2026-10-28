"""Messages: the caller's inbox, chats, sends and read marks (decision 301).

Every dependency is the caller's own: the store reads and writes through M0
with the caller's id from the token, never one from the request.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query, Request
from pydantic import StringConstraints

from app.api.deps.core import CurrentUserDep, SessionDep
from app.api.deps.message_emails import schedule_unread_email
from app.api.schemas.common import StorableText
from app.api.schemas.messages import (
    ChatPage,
    ChatRead,
    MessageRead,
    MessageWrite,
    PersonRead,
    ReadWrite,
    ThreadRead,
)
from app.core.errors import ValidationError
from app.domain.chats import DEFAULT_PAGE, MAX_PAGE
from app.domain.message_emails import UNREAD_EMAIL_DELAY
from app.infra.db.chat_store import (
    chat_of,
    chat_rows,
    not_connected,
    people,
    thread_page,
    unread_total,
)
from app.infra.db.chat_writer import mark_read, send_message
from app.infra.db.message_emails import queue_instant_email

#: The longest name search the picker takes.
PEOPLE_QUERY_MAX_LENGTH = 100


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def own_chats(user: CurrentUserDep, session: SessionDep) -> ChatPage:
    me: UUID = user["id"]
    rows = [ChatRead.of(row, me) for row in await chat_rows(session, me, _now())]
    return ChatPage(data=rows, total_unread=sum(row.unread for row in rows))


async def own_unread(user: CurrentUserDep, session: SessionDep) -> int:
    return await unread_total(session, user["id"])


async def own_people(
    user: CurrentUserDep,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Part of a name, any case. Empty: everyone you are connected with."),
        StringConstraints(max_length=PEOPLE_QUERY_MAX_LENGTH),
        StorableText,
    ] = None,
) -> list[PersonRead]:
    rows = await people(session, user["id"], _now(), (q or "").strip() or None)
    return [PersonRead.of(row) for row in rows]


async def own_thread(
    user_id: UUID,
    user: CurrentUserDep,
    session: SessionDep,
    before: Annotated[
        UUID | None, Query(description="A message id: the page just older than it.")
    ] = None,
    after: Annotated[
        UUID | None, Query(description="A message id: the page just newer than it.")
    ] = None,
    limit: Annotated[
        int, Query(ge=0, le=MAX_PAGE, description="How many messages, 0 to 100.")
    ] = DEFAULT_PAGE,
) -> ThreadRead:
    if before is not None and after is not None:
        raise ValidationError("send before or after, not both")
    me: UUID = user["id"]
    chat = await chat_of(session, me, user_id, _now())
    if chat is None:
        raise not_connected()
    items, has_more = await thread_page(
        session, chat["conversation_id"], before=before, after=after, limit=limit
    )
    return ThreadRead.of(chat, items, has_more, me)


async def sent_message(
    user_id: UUID,
    payload: MessageWrite,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> tuple[MessageRead, bool]:
    """The message, written and committed, and whether it is new.

    A new message around a session between the two is emailed at once, in the
    send's transaction (decision 303). Any other new message queues the unread
    email's job for 15 minutes later, once the send has committed. A replay
    emails nothing again.
    """
    me: UUID = user["id"]
    now = _now()
    row, created = await send_message(
        session, me, user_id, client_id=payload.client_id, body=payload.body, now=now
    )
    instant = created and await queue_instant_email(session, row["id"], me, user_id, now)
    await session.commit()
    if created and not instant:
        await schedule_unread_email(request, user_id, row["sent_at"] + UNREAD_EMAIL_DELAY)
    return MessageRead.of(row, me), created


async def read_marked(
    user_id: UUID, payload: ReadWrite, user: CurrentUserDep, session: SessionDep
) -> None:
    await mark_read(session, user["id"], user_id, payload.up_to, _now())
    await session.commit()


OwnChatsDep = Annotated[ChatPage, Depends(own_chats)]
OwnUnreadDep = Annotated[int, Depends(own_unread)]
OwnPeopleDep = Annotated[list[PersonRead], Depends(own_people)]
OwnThreadDep = Annotated[ThreadRead, Depends(own_thread)]
SentMessageDep = Annotated[tuple[MessageRead, bool], Depends(sent_message)]
ReadMarkedDep = Annotated[None, Depends(read_marked)]

__all__ = [
    "OwnChatsDep",
    "OwnPeopleDep",
    "OwnThreadDep",
    "OwnUnreadDep",
    "ReadMarkedDep",
    "SentMessageDep",
]
