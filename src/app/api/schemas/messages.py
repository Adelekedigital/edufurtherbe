"""Messages: the inbox, a chat, sending and reading (ADR 0010; decision 301).

Everything here is the caller's own: their chats with the people they are
connected with (M0). The person is `PersonRead`, which shares its fields with
a session's party through `PersonBaseRead`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.common import Normalised, Page, PersonBaseRead
from app.domain.chats import (
    BODY_MAX_LENGTH,
    PREVIEW_LENGTH,
    SESSION_WINDOW,
    ChatLock,
    ChatRole,
    LastKind,
    chat_lock,
    preview,
)
from app.domain.enums import IntakeFileType, MessageType

WINDOW_MINUTES = int(SESSION_WINDOW.total_seconds() // 60)


class PersonRead(PersonBaseRead):
    """Somebody you can message: a row of your inbox, the New message picker,
    and a chat's header. A session party's person fields, plus what they are
    to you."""

    role: ChatRole = Field(
        description=(
            "What they are to you: `mentor` or `mentee`. Two people have one chat, "
            "with the roles they had when it began (before that, in their newest "
            "booking with you)."
        )
    )
    handle: str | None = Field(
        description=(
            "A mentor's profile handle (`/mentors/{handle}`), only while their "
            "profile is public. `null` for a mentee, and for a mentor whose "
            "profile does not resolve (paused, not approved, or a closed "
            "account): do not link."
        )
    )
    paused: bool = Field(
        description=(
            "They are a mentor who is paused, by themselves or by EduFurther: you "
            "can read the chat and cannot send. Always `false` for a mentee."
        )
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> PersonRead:
        return cls(
            **PersonBaseRead.fields_of(row),
            role=ChatRole(str(row["role"])),
            handle=row["handle"],
            paused=bool(row["paused"]),
        )


class NextBookingRead(BaseModel):
    """Your next confirmed session with them that has not started."""

    id: UUID
    starts_at: dt.datetime
    session_name: str | None = Field(
        description="The session type's name; `null` for a migrated session with none."
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> NextBookingRead | None:
        if row["next_booking_id"] is None:
            return None
        return cls(
            id=row["next_booking_id"],
            starts_at=row["next_booking_starts_at"],
            session_name=row["next_booking_session_name"],
        )


class LastRead(BaseModel):
    """The newest message in a chat, as its inbox row previews it."""

    kind: LastKind = Field(description="`file` for a message carrying a file, else `text`.")
    text: str | None = Field(
        description=(
            f"The text, on one line, cut to {PREVIEW_LENGTH} characters at a word "
            "and ending `…` when cut. `null` for a file alone, and when `deleted`."
        )
    )
    file_name: str | None = Field(
        description="The file's name for a `file`; `null` otherwise, and when `deleted`."
    )
    mine: bool = Field(description="You sent it (show `You: `).")
    at: dt.datetime = Field(description="When it was sent.")
    deleted: bool = Field(
        description="It was deleted: show the deleted line, not its text (imported only in v1)."
    )

    @classmethod
    def of(cls, row: dict[str, Any], me: UUID) -> LastRead | None:
        if row["last_id"] is None:
            return None
        deleted = row["last_deleted_at"] is not None
        body = row["last_body"]
        return cls(
            kind=LastKind.FILE
            if row["last_message_type"] == MessageType.ATTACHMENT
            else LastKind.TEXT,
            text=preview(str(body)) if body is not None and not deleted else None,
            # Files arrive with the upload endpoints.
            file_name=None,
            mine=row["last_sender_id"] == me,
            at=row["last_sent_at"],
            deleted=deleted,
        )


class ChatRead(BaseModel):
    """One row of your inbox: one person you are connected with."""

    person: PersonRead
    last: LastRead | None = Field(description="The newest message; `null` when there is none.")
    unread: int = Field(description="Their messages you have not read, not deleted.")
    next_booking: NextBookingRead | None

    @classmethod
    def of(cls, row: dict[str, Any], me: UUID) -> ChatRead:
        return cls(
            person=PersonRead.of(row),
            last=LastRead.of(row, me),
            unread=int(row["unread"]),
            next_booking=NextBookingRead.of(row),
        )


class ChatPage(Page[ChatRead]):
    """Your inbox. Not paged: every chat is here and `next_cursor` is `null`."""

    total_unread: int = Field(description="The sum of `unread` over `data`: the nav badge.")


class UnreadRead(BaseModel):
    """Your unread messages across every chat."""

    total: int = Field(description="Equal to `total_unread` on `GET /me/messages`.")


class FileRead(BaseModel):
    """A file in a message. Arrives with the upload endpoints."""

    id: UUID
    filename: str
    size: int = Field(description="Bytes.")
    content_type: IntakeFileType


class MessageRead(BaseModel):
    """One message in a chat."""

    id: UUID
    client_id: UUID | None = Field(
        description="The id the sender's app chose; `null` on imported messages."
    )
    sender_id: UUID
    mine: bool = Field(description="You sent it.")
    body: str | None = Field(
        description="The text, line breaks kept; `null` for a file alone and when `deleted`."
    )
    file: FileRead | None = Field(description="The file it carries; `null` when none or `deleted`.")
    sent_at: dt.datetime
    edited_at: dt.datetime | None = Field(
        description="When it was last edited (imported messages only in v1)."
    )
    deleted: bool = Field(description="It was deleted (imported messages only in v1).")

    @classmethod
    def of(cls, row: dict[str, Any], me: UUID) -> MessageRead:
        deleted = row["deleted_at"] is not None
        return cls(
            id=row["id"],
            client_id=row["client_id"],
            sender_id=row["sender_id"],
            mine=row["sender_id"] == me,
            body=None if deleted else row["body"],
            file=None,
            sent_at=row["sent_at"],
            edited_at=row["edited_at"],
            deleted=deleted,
        )


class ThreadRead(BaseModel):
    """Your chat with one person, and one page of its messages."""

    person: PersonRead
    lock: ChatLock | None = Field(
        description=(
            "Why you cannot send: `closed` (they closed their account) wins over "
            "`paused` (they are a paused mentor). `null` when you can. Reading "
            "always works."
        )
    )
    next_booking: NextBookingRead | None
    session_window: bool = Field(
        description=(
            f"A session between you starts within {WINDOW_MINUTES} minutes either "
            "side of now: confirmed, or already settled as held or missed; never "
            "a cancelled one."
        )
    )
    items: list[MessageRead] = Field(description="Oldest first.")
    their_last_read_at: dt.datetime | None = Field(
        description="How far they have read: your messages sent at or before it are read."
    )
    my_last_read_at: dt.datetime | None
    has_more: bool = Field(
        description=(
            "With no cursor or with `before`: older messages exist before "
            "`items[0]`. With `after`: newer ones are waiting, so call again with "
            "`after` set to the last id. With `limit=0` and no cursor: the chat "
            "has any message at all."
        )
    )

    @classmethod
    def of(
        cls, row: dict[str, Any], items: list[dict[str, Any]], has_more: bool, me: UUID
    ) -> ThreadRead:
        return cls(
            person=PersonRead.of(row),
            lock=chat_lock(closed=bool(row["deleted"]), paused=bool(row["paused"])),
            next_booking=NextBookingRead.of(row),
            session_window=bool(row["session_window"]),
            items=[MessageRead.of(item, me) for item in items],
            their_last_read_at=row["their_last_read_at"],
            my_last_read_at=row["my_last_read_at"],
            has_more=has_more,
        )


class MessageWrite(Normalised):
    """A message to send. **Text only for now**: files arrive with the upload
    endpoints, as an optional `file_id` beside `body`."""

    model_config = ConfigDict(extra="forbid")

    client_id: UUID = Field(
        description=(
            "A fresh id your app makes for this message. Sending the same one "
            "again returns the stored message (`200`) and never stores a second."
        )
    )
    body: str = Field(
        max_length=BODY_MAX_LENGTH,
        description=(
            f"The text, 1 to {BODY_MAX_LENGTH} characters after trimming; line "
            "breaks inside it are kept."
        ),
    )


class ReadWrite(BaseModel):
    """How far you have read."""

    model_config = ConfigDict(extra="forbid")

    up_to: UUID = Field(description="The id of the newest message in this chat you have seen.")
