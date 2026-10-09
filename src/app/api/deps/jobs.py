"""Scheduler callbacks: session reminders and the runtime jobs QStash delivers."""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.api.deps.core import SessionDep, _configured, logger
from app.api.deps.sessions import _reminder_callback_url
from app.core.errors import (
    AuthenticationError,
    ValidationError,
)
from app.domain.notifications import REMINDER_OFFSETS, SESSION_REMINDER_KINDS
from app.domain.suggestions import SUGGESTION_REMINDER_KIND
from app.infra.clients.daily_presence import sighting_from, verify_daily_signature
from app.infra.clients.scheduler import (
    UntrustedCallbackError,
    verify_callback,
)

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.db.session_writer import (
    observe_presence,
    remind_before_session,
    remind_if_still_waiting,
    remind_suggestion,
)
from app.infra.jobs.manifest import RUNTIME_JOB_NAMES, schedule_id
from app.infra.jobs.runner import RuntimeJobs


async def reminder_callback(request: Request, session: SessionDep) -> bool:
    """Verify the caller is QStash, then fire the reminder if it is still owed.

    **The signature is the whole authorization**, so it is checked before the
    body is parsed as anything — a payload that has not been proved authentic is
    input, not instruction.

    Raises :class:`AuthenticationError` rather than a bespoke status, so this
    endpoint answers `401` through the same handler as everything else. A caller
    who cannot prove who they are has not been *refused permission*; there is
    nobody to refuse.
    """
    settings = _configured(request)
    keys = tuple(
        key.get_secret_value()
        for key in (settings.qstash_current_signing_key, settings.qstash_next_signing_key)
        if key is not None
    )
    url = _reminder_callback_url(request)
    token = request.headers.get("Upstash-Signature", "")
    if not keys or not url:
        # **Refused rather than waved through.** An unconfigured verifier on a
        # public endpoint that queues messages is worse than one that rejects
        # everything: the second is visibly broken, the first is quietly open.
        raise AuthenticationError("callback verification is not configured")

    body = await request.body()
    try:
        verify_callback(token=token, body=body, url=url, signing_keys=keys)
    except UntrustedCallbackError as exc:
        raise AuthenticationError(str(exc)) from exc

    payload = json.loads(body or b"{}")
    session_id = payload.get("session_id")
    kind = payload.get("kind")
    known = kind in REMINDER_OFFSETS or kind in SESSION_REMINDER_KINDS
    if not session_id or not (known or kind == SUGGESTION_REMINDER_KIND):
        raise ValidationError("not a reminder callback")

    # **Two kinds, one callback.** They differ in what they require: a response
    # reminder is only sent while the request is still unanswered, a session
    # reminder only while the session is still going ahead. Dispatching on the
    # kind keeps that in one place rather than in two endpoints that would drift.
    #
    # **The review reminder is deliberately not here.** It is a sweep, not a
    # scheduled callback — see `remind_unreviewed`, and the measurement that put
    # it there.
    if kind in SESSION_REMINDER_KINDS:
        queued = await remind_before_session(session, UUID(str(session_id)), str(kind))
    elif kind == SUGGESTION_REMINDER_KIND:
        # A suggested time's hold is about to lapse (#339): nudged only while
        # the offer is still unbooked and still held.
        queued = await remind_suggestion(session, UUID(str(session_id)), str(kind))
    else:
        queued = await remind_if_still_waiting(session, UUID(str(session_id)), str(kind))
    await session.commit()
    return queued


ReminderCallbackDep = Annotated[bool, Depends(reminder_callback)]


async def daily_presence_callback(request: Request, session: SessionDep) -> bool:
    """Verify the caller is Daily, then record who it saw in the room (#382).

    **The signature is the whole authorization**, checked against the raw body
    before anything is parsed, because what this writes is attendance: a forged
    join would mark a party present and move a refund.

    Unconfigured refuses everything, as the QStash callback does. Once verified,
    anything that is not a complete ``participant.joined`` (Daily's creation
    check, other event types) is acknowledged and records nothing: refusing it
    would count as a failed delivery, and three of those switch the webhook off.
    """
    secret = _configured(request).daily_webhook_secret
    # Empty refuses too, whatever the config layer allowed: an HMAC under an
    # empty key is one any caller can compute (security review, #382).
    if secret is None or not secret.get_secret_value().strip():
        raise AuthenticationError("daily webhook verification is not configured")
    body = await request.body()
    try:
        verify_daily_signature(
            secret=secret.get_secret_value(),
            timestamp=request.headers.get("X-Webhook-Timestamp", ""),
            body=body,
            signature=request.headers.get("X-Webhook-Signature", ""),
        )
    except UntrustedCallbackError as exc:
        raise AuthenticationError(str(exc)) from exc
    try:
        event = json.loads(body or b"{}")
    except ValueError:
        return False
    sighting = sighting_from(event) if isinstance(event, dict) else None
    if sighting is None:
        return False
    recorded = await observe_presence(session, sighting)
    await session.commit()
    return recorded


DailyPresenceDep = Annotated[bool, Depends(daily_presence_callback)]


class RuntimeJobRequest(BaseModel):
    """The complete body QStash sends to a recurring job endpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str


async def runtime_job_delivery(job_name: str, request: Request) -> dict[str, Any]:
    """Verify raw QStash bytes, then parse and call the shared job runner."""
    settings = _configured(request)
    keys = tuple(
        key.get_secret_value()
        for key in (settings.qstash_current_signing_key, settings.qstash_next_signing_key)
        if key is not None
    )
    if not keys or not settings.public_base_url:
        raise AuthenticationError("runtime callback verification is not configured")

    path = f"/api/v1/internal/jobs/{job_name}"
    destination = f"{settings.public_base_url.rstrip('/')}{path}"
    body = await request.body()
    try:
        verify_callback(
            token=request.headers.get("Upstash-Signature", ""),
            body=body,
            url=destination,
            signing_keys=keys,
        )
    except UntrustedCallbackError as exc:
        raise AuthenticationError(str(exc)) from exc

    if job_name not in RUNTIME_JOB_NAMES:
        raise ValidationError("unknown runtime job")
    try:
        payload = RuntimeJobRequest.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("not a runtime job callback") from exc
    if payload.job_id != schedule_id(settings.environment, job_name):
        raise ValidationError("job_id does not match this environment and endpoint")

    message_id = request.headers.get("Upstash-Message-Id")
    logger.info(
        "runtime job delivery",
        extra={
            "job_name": job_name,
            "job_id": payload.job_id,
            "upstash_message_id": message_id,
        },
    )
    runner = getattr(request.app.state, "runtime_jobs", None) or RuntimeJobs(settings)
    result = await runner.run(job_name, job_id=payload.job_id, message_id=message_id)
    return {
        "job": result.name,
        "job_id": result.job_id,
        "status": result.status,
        "counts": result.counts,
    }


RuntimeJobDep = Annotated[dict[str, Any], Depends(runtime_job_delivery)]
