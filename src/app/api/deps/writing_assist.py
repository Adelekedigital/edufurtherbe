"""The writing assistant: draft or improve a bio, never saved (decision 267).

Order matters, because each refusal must cost nothing it need not:

1. The request's own shape (`text` for an improve, none for a draft) — `422`.
2. Whether the assistant is on and configured — `503`, nothing read or counted.
3. Whether the caller has the profile the field belongs to — `404`.
4. The body's size against the router's per-task cap — `422` at `/text`.
5. The daily limit, and the row that records the call — `429`, or committed.
6. The call, off the event loop; no answer of any kind is `503`.

A row is written only once every check has passed and the call is about to be
made, so a refused request never counts against the limit.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Request

from app.api.deps.core import CurrentUserDep, SessionDep, _configured
from app.api.schemas.writing_assist import WritingAssistRead, WritingAssistWrite
from app.core.errors import NotFoundError, ValidationError, WritingAssistUnavailableError
from app.domain.enums import WritingAssistField, WritingAssistMode
from app.domain.writing_assist import (
    MAX_LENGTH,
    TASKS,
    WritingAssistant,
    call_for,
    daily_limit,
    is_known_prompt_version,
    source_for,
    source_size,
    usable_text,
)
from app.infra.clients.ai_router import writing_assistant_for
from app.infra.db.writing_assist_store import draft_facts, has_required_profile, record_call

#: The one message every "no answer" carries; the cause is logged, not sent.
UNAVAILABLE = "the writing assistant could not write that just now; try again later"
_NO_PROFILE = {
    WritingAssistField.MENTOR_ABOUT: "you have no mentor profile",
    WritingAssistField.MENTEE_BIO: "add your goal or profile first",
}


def writing_assistant(request: Request) -> WritingAssistant:
    """The assistant wired on `app.state`, else the one settings choose."""
    wired: WritingAssistant | None = getattr(request.app.state, "writing_assistant", None)
    return wired if wired is not None else writing_assistant_for(_configured(request))


def _text_problem(message: str) -> ValidationError:
    return ValidationError(message, field_errors=(("/text", message),))


def _checked_text(body: WritingAssistWrite) -> str | None:
    """The text to improve, within the field's bound; `None` for a draft."""
    if body.mode is WritingAssistMode.DRAFT:
        if body.text is not None:
            raise _text_problem("a draft is written from your profile; send no text")
        return None
    limit = MAX_LENGTH[body.field]
    if body.text is None or not body.text.strip():
        raise _text_problem("send the text to improve")
    if len(body.text) > limit:
        raise _text_problem(f"the text to improve is longer than {limit} characters")
    return body.text


async def assisted_text(
    body: WritingAssistWrite, request: Request, user: CurrentUserDep, session: SessionDep
) -> WritingAssistRead:
    text = _checked_text(body)
    assistant = writing_assistant(request)
    if not assistant.available:
        raise WritingAssistUnavailableError(UNAVAILABLE)
    user_id = user["id"]
    facts = None
    if body.mode is WritingAssistMode.DRAFT:
        facts = await draft_facts(session, user_id, body.field)
        if facts is None:
            raise NotFoundError(_NO_PROFILE[body.field])
    elif not await has_required_profile(session, user_id, body.field):
        raise NotFoundError(_NO_PROFILE[body.field])

    limit = daily_limit(_configured(request))
    # The largest `attempt` a call can carry is unbounded, so the size is
    # checked with room for any: a ten-digit number.
    sized = source_for(body.field, body.mode, attempt=10**9, facts=facts, text=text)
    if source_size(sized) > TASKS[body.mode].max_source_bytes:
        if body.mode is WritingAssistMode.IMPROVE:
            raise _text_problem("that text is too long for the writing assistant")
        # A draft sends no text, so nothing at `/text` could fix it.
        raise ValidationError("your profile holds too much to draft from")

    row_id, attempt = await record_call(session, user_id, body.field, limit=limit)
    await session.commit()

    source = source_for(body.field, body.mode, attempt=attempt, facts=facts, text=text)
    call = call_for(user_id, body.field, body.mode, source, str(row_id))
    result = await asyncio.to_thread(assistant.run, call)
    suggestion = usable_text(result.text, body.field) if result is not None else None
    # A version the provenance could not store is no answer either.
    if result is None or suggestion is None or not is_known_prompt_version(result.prompt_version):
        raise WritingAssistUnavailableError(UNAVAILABLE)
    return WritingAssistRead(text=suggestion, prompt_version=result.prompt_version)


AssistedTextDep = Annotated[WritingAssistRead, Depends(assisted_text)]
