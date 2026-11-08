"""The new-message push (decision 312): its QStash job, and sending it.

**Each send queues one job `MESSAGE_PUSH_DELAY` later**, after the send
commits, and only while push is configured here. A publish that fails is
logged and costs that message its push, never the send. **The job re-reads
everything** (`message_push_facts`): a message read meanwhile, or followed by
another from the same sender, pushes nothing, so a burst pushes once.

**Every verified delivery answers `200`**, whatever the push services said: a
retry would only push again to the browsers that did get it. A browser whose
service answers `404`/`410` is forgotten in the same transaction.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.api.deps.core import SessionDep, _configured, logger
from app.api.deps.jobs import verified_qstash_body
from app.api.deps.sessions import _scheduler, callback_url
from app.core.errors import ValidationError
from app.domain.push import message_push
from app.infra.clients.scheduler import SchedulerError
from app.infra.clients.web_push import PushOutcome, PushTarget, WebPushPort, fan_out, web_push_for
from app.infra.db.push_messages import message_push_facts
from app.infra.db.push_store import forget_gone, subscriptions_for

__all__ = [
    "PUSH_MESSAGE_CALLBACK_PATH",
    "PushMessageCallbackDep",
    "schedule_message_push",
    "web_push",
]

#: Where QStash calls back. One constant: it is signed into the token, so the
#: path published and the path verified must be the same string.
PUSH_MESSAGE_CALLBACK_PATH = "/api/v1/callbacks/push-messages"


def web_push(request: Request) -> WebPushPort:
    """The sender off `app.state` first, as every adapter seam is read."""
    return getattr(request.app.state, "web_push", None) or web_push_for(_configured(request))


async def schedule_message_push(request: Request, message_id: UUID, at: dt.datetime) -> None:
    """Queue the push job for a new message. Called after the send committed;
    nothing when push is not configured, and a failure is only logged."""
    if not _configured(request).push_configured:
        return
    try:
        url = callback_url(request, PUSH_MESSAGE_CALLBACK_PATH)
        if url is None:
            raise SchedulerError("no public origin to call back to")
        await asyncio.to_thread(
            _scheduler(request).schedule, url=url, body={"message_id": str(message_id)}, at=at
        )
    except (SchedulerError, NotImplementedError) as exc:
        logger.warning(
            "message push not scheduled",
            extra={"message_id": str(message_id), "error": type(exc).__name__},
        )


class PushMessageCallback(BaseModel):
    """The complete body QStash delivers."""

    model_config = ConfigDict(extra="forbid", strict=True)

    message_id: UUID


async def push_message_callback(request: Request, session: SessionDep) -> int:
    """Verify the caller is QStash, then push the message if it still owes one.
    How many browsers took it."""
    body = await verified_qstash_body(request, callback_url(request, PUSH_MESSAGE_CALLBACK_PATH))
    try:
        message_id = PushMessageCallback.model_validate_json(body).message_id
    except PydanticValidationError as exc:
        raise ValidationError("not a message push callback") from exc
    facts = await message_push_facts(session, message_id)
    browsers = [] if facts is None else await subscriptions_for(session, facts.recipient_id)
    outcomes: list[PushOutcome] = []
    if facts is not None and browsers:
        push = message_push(facts.sender_id, facts.first_name, facts.last_name, unread=facts.unread)
        targets = [PushTarget(row.endpoint, row.p256dh, row.auth) for row in browsers]
        ids = [row.id for row in browsers]
        # **The read ends before any push goes out**: a slow push service must
        # not hold a pooled connection idle in a transaction for the fan-out.
        # Everything needed after is copied out above, as the rows expire here.
        await session.rollback()
        outcomes = await asyncio.to_thread(fan_out, web_push(request), targets, push)
        await forget_gone(session, ids, outcomes)
        await session.commit()
    delivered = outcomes.count(PushOutcome.DELIVERED)
    logger.info(
        "message push",
        extra={
            "message_id": str(message_id),
            "owed": facts is not None,
            "browsers": len(browsers),
            "delivered": delivered,
            "gone": outcomes.count(PushOutcome.GONE),
            "failed": outcomes.count(PushOutcome.FAILED),
        },
    )
    return delivered


PushMessageCallbackDep = Annotated[int, Depends(push_message_callback)]
