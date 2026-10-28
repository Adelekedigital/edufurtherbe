"""Message emails (decision 303): the unread email's QStash job, the
session-window email at send, and the caller's switch.

**Each send queues one job for 15 minutes later** (`schedule_unread_email`),
published after the send commits. A publish that fails is logged and costs
that message its email, never the send. **The job re-reads everything**
(`queue_unread_email`), so a job for a message read meanwhile does nothing,
and a job that finds an email went within the hour publishes itself again for
when the hour ends. Nothing is ever cancelled (ADR 0028's callbacks).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.api.deps.core import CurrentUserDep, SessionDep, logger
from app.api.deps.jobs import verified_qstash_body
from app.api.deps.sessions import _scheduler, callback_url
from app.api.schemas.message_settings import MessageSettingsRead, MessageSettingsWrite
from app.core.errors import ValidationError
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.message_emails import queue_unread_email
from app.infra.db.message_prefs import set_unread_emails, unread_emails_on

__all__ = [
    "MESSAGE_EMAIL_CALLBACK_PATH",
    "MessageEmailCallbackDep",
    "OwnMessageSettingsDep",
    "UpdatedMessageSettingsDep",
    "schedule_unread_email",
]

#: Where QStash calls back. One constant: it is signed into the token, so the
#: path published and the path verified must be the same string.
MESSAGE_EMAIL_CALLBACK_PATH = "/api/v1/callbacks/message-emails"


async def _publish(request: Request, recipient: UUID, at: dt.datetime) -> None:
    """Publish the job for ``recipient`` at ``at``. Raises when it cannot."""
    url = callback_url(request, MESSAGE_EMAIL_CALLBACK_PATH)
    if url is None:
        raise SchedulerError("no public origin to call back to")
    await asyncio.to_thread(
        _scheduler(request).schedule, url=url, body={"recipient_id": str(recipient)}, at=at
    )


async def schedule_unread_email(request: Request, recipient: UUID, at: dt.datetime) -> None:
    """Queue the unread email job after a send. Called after the send committed;
    a failure is logged, and the message simply gets no unread email."""
    try:
        await _publish(request, recipient, at)
    except (SchedulerError, NotImplementedError) as exc:
        logger.warning(
            "unread message email not scheduled",
            extra={"recipient_id": str(recipient), "error": type(exc).__name__},
        )


class MessageEmailCallback(BaseModel):
    """The complete body QStash delivers."""

    model_config = ConfigDict(extra="forbid", strict=True)

    recipient_id: UUID


async def message_email_callback(request: Request, session: SessionDep) -> bool:
    """Verify the caller is QStash, then queue the unread email if one is owed.

    **Every handled outcome is a `200`**, `queued: false` included. A held
    email publishes its next run after this transaction commits; if that
    publish fails, the error answers non-2xx so QStash retries this delivery,
    which finds the same messages still waiting.
    """
    body = await verified_qstash_body(request, callback_url(request, MESSAGE_EMAIL_CALLBACK_PATH))
    try:
        recipient = MessageEmailCallback.model_validate_json(body).recipient_id
    except PydanticValidationError as exc:
        raise ValidationError("not a message email callback") from exc
    outcome = await queue_unread_email(session, recipient, dt.datetime.now(dt.UTC))
    await session.commit()
    if outcome.retry_at is not None:
        await _publish(request, recipient, outcome.retry_at)
    logger.info(
        "unread message email",
        extra={
            "recipient_id": str(recipient),
            "outcome": outcome.reason,
            "upstash_message_id": request.headers.get("Upstash-Message-Id"),
        },
    )
    return outcome.queued is not None


async def own_message_settings(user: CurrentUserDep, session: SessionDep) -> MessageSettingsRead:
    return MessageSettingsRead(unread_emails=await unread_emails_on(session, user["id"]))


async def updated_message_settings(
    payload: MessageSettingsWrite, user: CurrentUserDep, session: SessionDep
) -> MessageSettingsRead:
    on = await set_unread_emails(session, user["id"], payload.unread_emails)
    await session.commit()
    return MessageSettingsRead(unread_emails=on)


MessageEmailCallbackDep = Annotated[bool, Depends(message_email_callback)]
OwnMessageSettingsDep = Annotated[MessageSettingsRead, Depends(own_message_settings)]
UpdatedMessageSettingsDep = Annotated[MessageSettingsRead, Depends(updated_message_settings)]
