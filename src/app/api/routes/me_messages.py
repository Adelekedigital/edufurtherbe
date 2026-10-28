"""Messages: your inbox, a chat, sending and reading (ADR 0010; decision 301).

**Keyed by the other person's user id**, so `/messages?with={id}` maps
straight on. You may read and write only a chat with somebody you are
connected with (M0): one of you is the mentor and the other the mentee of a
session that was booked (confirmed, completed, missed or cancelled). It is
checked in every statement, so a chat with anyone else is a `404` to read and
a `403` to send into.

**The static paths are declared before `/{user_id}`**, which would otherwise
read `unread`, `people` and `settings` as an id.
"""

from uuid import UUID

from fastapi import APIRouter, Response, status

from app.api.deps import (
    PRIVATE_NO_STORE,
    OwnChatsDep,
    OwnMessageSettingsDep,
    OwnPeopleDep,
    OwnThreadDep,
    OwnUnreadDep,
    ReadMarkedDep,
    SentMessageDep,
    UpdatedMessageSettingsDep,
)
from app.api.schemas.common import Page
from app.api.schemas.message_settings import MessageSettingsRead
from app.api.schemas.messages import ChatPage, MessageRead, PersonRead, ThreadRead, UnreadRead
from app.domain.chats import BODY_MAX_LENGTH, MAX_PAGE, PREVIEW_LENGTH

router = APIRouter(prefix="/api/v1/me/messages", tags=["messages"])

UNAUTHORISED: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    }
}

NOT_CONNECTED_READ: dict[int | str, dict[str, str]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "Plain `404`: you are not connected with this person, they do not "
            "exist, or it is you. All three look the same."
        )
    }
}


@router.get(
    "",
    response_model=ChatPage,
    summary="Your inbox",
    description=(
        "One row per person you are connected with, with or without messages: "
        "the newest message first, then people with no messages, newest booking "
        "first. Each has the person, the newest message (`last`, its text cut to "
        f"{PREVIEW_LENGTH} characters at a word), your unread count and your next "
        "confirmed session with them.\n\n"
        "**Not paged**: every chat is in `data` and `next_cursor` is `null`. "
        "`total_unread` is the sum of `unread`. Reading this marks nothing read."
    ),
    responses=UNAUTHORISED,
)
async def list_chats(chats: OwnChatsDep, response: Response) -> ChatPage:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return chats


@router.get(
    "/unread",
    response_model=UnreadRead,
    summary="Your unread count, for the badge",
    description=(
        "Unread messages across every chat you can read: the same count as the "
        "inbox's `total_unread`. Cheap enough to poll every 30 seconds."
    ),
    responses=UNAUTHORISED,
)
async def unread(total: OwnUnreadDep, response: Response) -> UnreadRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return UnreadRead(total=total)


@router.get(
    "/people",
    response_model=Page[PersonRead],
    summary="People you can message",
    description=(
        "Everyone you are connected with, for New message, by first then last "
        "name. `q` keeps those whose name contains it, ignoring case. Not paged: "
        "`next_cursor` is `null`."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "`q` is over 100 characters."},
    },
)
async def list_people(found: OwnPeopleDep, response: Response) -> Page[PersonRead]:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return Page[PersonRead](data=found)


@router.get(
    "/settings",
    response_model=MessageSettingsRead,
    summary="Your message emails",
    description=(
        "Whether you get the unread message emails: `true` unless you switched "
        "them off. The session-window email is always on and has no switch."
    ),
    responses=UNAUTHORISED,
)
async def message_settings(
    settings: OwnMessageSettingsDep, response: Response
) -> MessageSettingsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return settings


@router.patch(
    "/settings",
    response_model=MessageSettingsRead,
    summary="Switch your unread message emails",
    description=(
        "Saves `unread_emails` and returns your settings. Saving the same value "
        "again changes nothing. An unread email already queued is not sent once "
        "it is off."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`unread_emails` is missing or not a boolean, or the body has another field."
            )
        },
    },
)
async def update_message_settings(settings: UpdatedMessageSettingsDep) -> MessageSettingsRead:
    return settings


@router.get(
    "/{user_id}",
    response_model=ThreadRead,
    summary="Your chat with one person",
    description=(
        "The person, whether you can send (`lock`), your next session with them, "
        "and a page of messages, oldest first.\n\n"
        "With no cursor, the newest `limit`. `before` (a message id) reads the "
        "page just older than it; `after` the page just newer, which is what a "
        "poll of an open chat sends with the last id it has. `limit` is 0 to "
        f"{MAX_PAGE} (default 50); `limit=0` returns no items and fills in "
        "everything else.\n\n"
        "A chat with no messages yet is a `200` with `items: []`. **Reading "
        "marks nothing read**: `POST .../read` does."
    ),
    responses={
        **UNAUTHORISED,
        **NOT_CONNECTED_READ,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`before` and `after` were both sent, `limit` is outside 0 to "
                f"{MAX_PAGE}, or a cursor is not a message in this chat."
            )
        },
    },
)
async def get_thread(thread: OwnThreadDep, response: Response) -> ThreadRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return thread


@router.post(
    "/{user_id}",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message",
    description=(
        "Text (`body`), a file (`file_id`) or both. `body` is trimmed and may hold "
        f"up to {BODY_MAX_LENGTH} characters; line breaks inside it are kept. "
        "`file_id` is a file you uploaded with `POST /me/message-files` and have "
        "not sent: it is attached with the message, in one step. The first "
        "message creates the chat.\n\n"
        "**Idempotent on `client_id`**: sending the same `client_id` again "
        "returns the stored message (with its file) with `200` and stores "
        "nothing, even if the first attempt's answer was lost. `Location` names "
        "the chat: a message has no URL of its own."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_200_OK: {
            "model": MessageRead,
            "description": "A repeat of a `client_id` you already sent: the stored message.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "`/problems/not-connected`: you can message only people you have "
                "booked with. The same for somebody who does not exist, and for you."
            )
        },
        status.HTTP_409_CONFLICT: {
            "description": (
                "`/problems/recipient-closed`: they closed their account. "
                "`/problems/recipient-paused`: they are a paused mentor. The chat "
                "is read only; `closed` wins when both hold."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                f"Neither text nor a file, or text over {BODY_MAX_LENGTH} characters "
                "(pointer `/body`); `/problems/file-unavailable`: `file_id` is not "
                "your unsent upload (somebody else's, already sent, or expired; "
                "pointer `/file_id`); `client_id` was already used for a message in "
                "another chat (pointer `/client_id`); or the body has a field this "
                "endpoint does not take."
            )
        },
    },
)
async def send(user_id: UUID, sent: SentMessageDep, response: Response) -> MessageRead:
    message, created = sent
    # A message has no URL of its own; the chat is where it is read.
    response.headers["Location"] = f"/api/v1/me/messages/{user_id}"
    if not created:
        response.status_code = status.HTTP_200_OK
    return message


@router.post(
    "/{user_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark your chat read",
    description=(
        "Moves how far you have read to `up_to`, a message in this chat. "
        "**Forward only**: an older `up_to` changes nothing and is still `204`. "
        "Their messages up to it stop counting as unread, and they see your "
        "`their_last_read_at` move."
    ),
    responses={
        **UNAUTHORISED,
        **NOT_CONNECTED_READ,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`up_to` is not a message in this chat (pointer `/up_to`)."
        },
    },
)
async def mark_read(_: ReadMarkedDep) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)
