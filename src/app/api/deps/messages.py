"""Messages: the caller's inbox, chats, sends and read marks (decision 301).

Every dependency is the caller's own: the store reads and writes through M0
with the caller's id from the token, never one from the request.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import StringConstraints

from app.api.deps.core import CurrentUserDep, SessionDep, _configured
from app.api.deps.files import UploadField, checked_file, private_storage, streamed_file
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
from app.core.errors import NotFoundError, ValidationError
from app.domain.chats import DEFAULT_PAGE, MAX_PAGE
from app.infra.db.chat_store import (
    chat_of,
    chat_rows,
    not_connected,
    people,
    thread_page,
    unread_total,
)
from app.infra.db.chat_writer import mark_read, send_message
from app.infra.db.message_file_store import readable_message_file, store_message_file

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
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
) -> tuple[MessageRead, bool]:
    """The message, written and committed, and whether it is new."""
    if payload.body is None and payload.file_id is None:
        # A blank body is `None` by now (`Normalised`): text, a file or both.
        raise ValidationError(
            "send text, a file or both", field_errors=(("/body", "text or a file is required"),)
        )
    me: UUID = user["id"]
    row, created = await send_message(
        session,
        me,
        user_id,
        client_id=payload.client_id,
        body=payload.body,
        file_id=payload.file_id,
        unused_hours=_configured(request).intake_file_unused_hours,
        now=_now(),
    )
    await session.commit()
    return MessageRead.of(row, me), created


async def read_marked(
    user_id: UUID, payload: ReadWrite, user: CurrentUserDep, session: SessionDep
) -> None:
    await mark_read(session, user["id"], user_id, payload.up_to, _now())
    await session.commit()


async def uploaded_message_file(
    request: Request, user: CurrentUserDep, session: SessionDep, file: UploadField
) -> dict[str, Any]:
    """The intake checks (`checked_file`), then the private bucket (decision 302)."""
    storage = private_storage(request)
    settings = _configured(request)
    checked = await checked_file(file, settings)
    return await store_message_file(
        session,
        storage,
        uploader_id=user["id"],
        filename=checked.filename,
        payload=checked.payload,
        kind=checked.kind,
        uploads_per_hour=settings.intake_uploads_per_hour,
        unused_hours=settings.intake_file_unused_hours,
        now=_now(),
    )


async def message_file_download(
    file_id: UUID, request: Request, user: CurrentUserDep, session: SessionDep
) -> StreamingResponse:
    """The file as an attachment, for its readers only (`readable_message_file`)."""
    row = await readable_message_file(session, file_id=file_id, caller_id=user["id"])
    if row is None:
        raise NotFoundError("no such file")
    return await streamed_file(
        request,
        storage_key=row["storage_key"],
        filename=row["file_name"],
        content_type=str(row["mime_type"]),
    )


OwnChatsDep = Annotated[ChatPage, Depends(own_chats)]
OwnUnreadDep = Annotated[int, Depends(own_unread)]
OwnPeopleDep = Annotated[list[PersonRead], Depends(own_people)]
OwnThreadDep = Annotated[ThreadRead, Depends(own_thread)]
SentMessageDep = Annotated[tuple[MessageRead, bool], Depends(sent_message)]
ReadMarkedDep = Annotated[None, Depends(read_marked)]
UploadedMessageFileDep = Annotated[dict[str, Any], Depends(uploaded_message_file)]
MessageFileDownloadDep = Annotated[StreamingResponse, Depends(message_file_download)]

__all__ = [
    "MessageFileDownloadDep",
    "OwnChatsDep",
    "OwnPeopleDep",
    "OwnThreadDep",
    "OwnUnreadDep",
    "ReadMarkedDep",
    "SentMessageDep",
    "UploadedMessageFileDep",
]
