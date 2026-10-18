"""The silent mentee bio draft (Onboarding v5 B6, decision 269): queued at a
mentee's first completion, written by a QStash callback.

**Queued after the completion commits**, never inside it: the message has no
delay, so publishing first could deliver it for a completion that then rolled
back. A publish that fails is logged and costs the person nothing but the draft;
completion never fails because of it.

**The callback re-reads before it spends anything** and writes only into an
unwritten bio, guarded again in the write itself, so a re-run, a redelivery or a
person who typed meanwhile never loses their words. It reuses the writing
assistant's port, source builder, key and answer rules; nothing is a second copy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.core import SessionDep, _configured, logger
from app.api.deps.jobs import verified_qstash_body
from app.api.deps.sessions import _scheduler, callback_url
from app.api.deps.writing_assist import writing_assistant
from app.core.errors import ValidationError
from app.domain.enums import WritingAssistField, WritingAssistMode
from app.domain.onboarding import finishes_as_mentee
from app.domain.writing_assist import (
    BACKGROUND_ATTEMPT,
    answer_from,
    background_relation_id,
    bio_draft_enabled,
    call_for,
    fits,
    source_for,
)
from app.infra.clients.scheduler import SchedulerError
from app.infra.db.bio_draft_store import bio_unwritten, save_bio_draft
from app.infra.db.onboarding_store import onboarding_evidence
from app.infra.db.writing_assist_store import draft_facts

__all__ = ["BIO_DRAFT_CALLBACK_PATH", "BioDraftCallbackDep", "queue_bio_draft"]

#: Where QStash calls back. One constant: it is signed into the token, so the
#: path published and the path verified must be the same string.
BIO_DRAFT_CALLBACK_PATH = "/api/v1/callbacks/bio-drafts"

FIELD = WritingAssistField.MENTEE_BIO
MODE = WritingAssistMode.DRAFT


async def queue_bio_draft(request: Request, user_id: UUID) -> None:
    """Publish the draft's callback with no delay, when it is on and can run.

    Off, the assistant unconfigured, or no public origin to call back to:
    nothing is queued. Called only after the completion committed.
    """
    url = callback_url(request, BIO_DRAFT_CALLBACK_PATH)
    on = bio_draft_enabled(_configured(request)) and writing_assistant(request).available
    if not on or url is None:
        return
    try:
        await asyncio.to_thread(
            _scheduler(request).schedule,
            url=url,
            body={"user_id": str(user_id)},
            at=dt.datetime.now(dt.UTC),
        )
    except (SchedulerError, NotImplementedError) as exc:
        logger.warning(
            "mentee bio draft not queued",
            extra={"user_id": str(user_id), "error": type(exc).__name__},
        )


class BioDraftCallback(BaseModel):
    """The complete body QStash delivers."""

    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UUID


async def _draft(request: Request, session: AsyncSession, user_id: UUID) -> str:
    """Draft and save the bio if one is still owed. Returns the outcome."""
    assistant = writing_assistant(request)
    if not (bio_draft_enabled(_configured(request)) and assistant.available):
        return "disabled"
    facts = await draft_facts(session, user_id, FIELD)
    owed = (
        facts is not None
        and finishes_as_mentee(await onboarding_evidence(session, user_id))
        and await bio_unwritten(session, user_id)
    )
    # Ends the read before the call, so no connection waits on the router.
    await session.commit()
    if not owed:
        return "not_owed"
    source = source_for(FIELD, MODE, attempt=BACKGROUND_ATTEMPT, facts=facts)
    if not fits(source, MODE):
        return "too_large"
    call = call_for(user_id, FIELD, MODE, source, background_relation_id(user_id))
    answer = answer_from(await asyncio.to_thread(assistant.run, call), FIELD)
    if answer is None:
        return "no_answer"
    saved = await save_bio_draft(session, user_id, answer)
    await session.commit()
    return "drafted" if saved else "kept_own"


async def bio_draft_callback(request: Request, session: SessionDep) -> bool:
    """Verify the caller is QStash, then draft the bio if it is still owed.

    **Every outcome but an error is a `200`**, because a non-2xx is a retry and
    none of them would change: a bio written or no longer owed stays so, and
    the router client has already retried what was transient. A database
    failure still raises, and QStash's own retry policy bounds the redelivery.
    """
    body = await verified_qstash_body(request, callback_url(request, BIO_DRAFT_CALLBACK_PATH))
    try:
        user_id = BioDraftCallback.model_validate_json(body).user_id
    except PydanticValidationError as exc:
        raise ValidationError("not a bio draft callback") from exc
    outcome = await _draft(request, session, user_id)
    logger.info(
        "mentee bio draft",
        extra={
            "user_id": str(user_id),
            "outcome": outcome,
            "upstash_message_id": request.headers.get("Upstash-Message-Id"),
        },
    )
    return outcome == "drafted"


BioDraftCallbackDep = Annotated[bool, Depends(bio_draft_callback)]
