"""Chats between a mentor and a mentee: the rules a message obeys (ADR 0010).

Named `chats` because `domain.messages` is already the email copy resolver.
Pure: who counts as connected, what locks a chat, what a message may hold and
how its preview reads. The statements that read and write apply these in the
query (`infra/db/chat_store.py` and `chat_writer.py`).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from app.domain.enums import SessionStatus
from app.domain.refunds import BOOKED_STATUSES

__all__ = [
    "BODY_MAX_LENGTH",
    "CONNECTING_STATUSES",
    "DEFAULT_PAGE",
    "ELLIPSIS",
    "FILE_PREFIX",
    "MAX_PAGE",
    "PREVIEW_LENGTH",
    "SESSION_WINDOW",
    "SESSION_WINDOW_STATUSES",
    "ChatLock",
    "ChatRole",
    "LastKind",
    "chat_lock",
    "file_storage_key",
    "preview",
]

#: The most characters a message's text may hold, after trimming (Messages PRD;
#: SendMessage's limit, and Q10 applies it to the inbox too). Pinned to the
#: database's `ck_messages_body_length` by a test.
BODY_MAX_LENGTH = 2000

#: **M0: who may message whom.** Two people are connected while one is the
#: mentor and the other the mentee of at least one session in these statuses:
#: every session that was booked (`BOOKED_STATUSES`: confirmed, completed,
#: no_show, cancelled). A cancelled session was confirmed first. A request
#: still waiting, declined, expired or withdrawn is not a connection.
CONNECTING_STATUSES = BOOKED_STATUSES

#: A session "starts now" for a chat from this long before its start to this
#: long after it (founder, 2026-10-26: 15 minutes, not the design's 10). The
#: thread's `session_window` reads it, and the instant email will.
SESSION_WINDOW = dt.timedelta(minutes=15)

#: A session counts for the window while it is on: confirmed, or already
#: settled as held or missed (a short session can settle inside the window).
#: Never a cancelled one.
SESSION_WINDOW_STATUSES = tuple(
    status for status in CONNECTING_STATUSES if status != SessionStatus.CANCELLED
)

#: How much of a message a preview shows (the inbox row; the unread email).
PREVIEW_LENGTH = 140

#: What a cut preview ends with.
ELLIPSIS = "…"

#: A thread page's default and largest size.
DEFAULT_PAGE = 50
MAX_PAGE = 100


class ChatRole(StrEnum):
    """What the other person is to you in a chat."""

    MENTOR = "mentor"
    MENTEE = "mentee"


class ChatLock(StrEnum):
    """Why a chat is read only. Reading always works; sending does not."""

    #: The other person is a mentor who is paused, by themselves or an admin.
    PAUSED = "paused"
    #: The other person closed their account.
    CLOSED = "closed"


class LastKind(StrEnum):
    """What the newest message in a chat carries."""

    TEXT = "text"
    FILE = "file"


def chat_lock(*, closed: bool, paused: bool) -> ChatLock | None:
    """**The one statement of when a chat is read only**, read by the thread's
    `lock` and by the send's `409`s alike. A closed account wins over a pause:
    the person is gone, so their pause no longer means anything."""
    if closed:
        return ChatLock.CLOSED
    if paused:
        return ChatLock.PAUSED
    return None


def preview(text: str, length: int = PREVIEW_LENGTH) -> str:
    """`text` cut to at most `length` characters at a word, with an ellipsis.

    Line breaks become spaces, so a preview is one line. Text that fits is
    returned whole. Otherwise it is cut at the last space that leaves room for
    the ellipsis; a single word longer than that is cut mid-word, since there
    is no word to cut at.
    """
    flat = " ".join(text.split())
    if len(flat) <= length:
        return flat
    room = flat[: length - len(ELLIPSIS) + 1]
    cut = room.rfind(" ")
    head = room[:cut] if cut > 0 else room[:-1]
    return head.rstrip() + ELLIPSIS


#: Where message files sit in the private bucket, beside intake's
#: `{uploader}/{uuid}`: a uuid never reads `messages`, so the two never meet.
FILE_PREFIX = "messages"


def file_storage_key(uploader_id: UUID, object_id: UUID) -> str:
    """A message file's object key: ids, and only ids (decision 302)."""
    return f"{FILE_PREFIX}/{uploader_id}/{object_id}"
