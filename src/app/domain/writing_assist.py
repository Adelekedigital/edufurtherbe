"""The writing assistant: drafting or improving a bio (Onboarding v5 B7, decision 267).

Pure. Which router task serves which (field, mode), what each task may be sent,
how a call is keyed, and what counts as a usable answer are all here; the call
itself is behind `WritingAssistant`, a provider-neutral port (decision 14) whose
adapter lives in `infra/clients/ai_router.py`.

**A caller never sends a prompt.** Prompts, prompt versions, output policies and
validators live in the AI router; this side sends a task name and `source_data`,
and gets back `{"text"}` plus the router's `prompt_version`.

**Nothing identifying is sent.** A draft carries labels from the caller's own
saved profile (countries, degree, field, help areas), never a name, an email, an
id or free text they wrote elsewhere. An improve carries only the text the
caller sent. Both always carry `role` and `attempt`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid5

from app.core.config import Settings
from app.domain.enums import WritingAssistField, WritingAssistMode
from app.domain.profile_text import (
    ABOUT_ME_MAX_LENGTH,
    PROMPT_VERSION_MAX_LENGTH,
    PROMPT_VERSION_PATTERN,
)
from app.domain.text import JOINERS, visible_only

__all__ = [
    "BACKGROUND_ATTEMPT",
    "DAILY_WINDOW",
    "FEATURE_ID",
    "OUTPUT_TEXT_KEY",
    "PRODUCT_ID",
    "TASKS",
    "AssistCall",
    "AssistResult",
    "Task",
    "WritingAssistant",
    "answer_from",
    "background_relation_id",
    "bio_draft_enabled",
    "call_for",
    "daily_limit",
    "fits",
    "is_known_prompt_version",
    "provenance_problem",
    "source_for",
    "source_size",
    "usable_text",
    "window_retry_after",
]

#: Who is calling, as the router's `SERVICE_CALLERS` authorizes it. One each.
PRODUCT_ID = "edufurther_platform"
FEATURE_ID = "profile_writing_assist"
#: Where the router's `output` carries the text (router, 2026-10-17).
OUTPUT_TEXT_KEY = "text"

#: The limit's window, measured on the database clock.
DAILY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Task:
    """One router task and what it may be sent."""

    name: str
    #: The router's `max_source_bytes` for the task, measured as `source_size`.
    #: Checked here first so an over-large request never spends a call.
    max_source_bytes: int


BIO_DRAFT = Task("bio_draft", max_source_bytes=8_192)
BIO_IMPROVE = Task("bio_improve", max_source_bytes=20_480)

#: Each field's router role and longest text. Both are `user_profiles.about_me`,
#: so both cap at the save bound (decision 263), read rather than retyped.
ROLES: dict[WritingAssistField, str] = {
    WritingAssistField.MENTEE_BIO: "mentee",
    WritingAssistField.MENTOR_ABOUT: "mentor",
}
MAX_LENGTH: dict[WritingAssistField, int] = {
    WritingAssistField.MENTEE_BIO: ABOUT_ME_MAX_LENGTH,
    WritingAssistField.MENTOR_ABOUT: ABOUT_ME_MAX_LENGTH,
}
TASKS: dict[WritingAssistMode, Task] = {
    WritingAssistMode.DRAFT: BIO_DRAFT,
    WritingAssistMode.IMPROVE: BIO_IMPROVE,
}

_VERSION = re.compile(PROMPT_VERSION_PATTERN)
#: A bio's provenance may name only the tasks that write one.
PROVENANCE_TASKS = frozenset(task.name for task in TASKS.values())

#: A bio keeps its paragraphs and its joiners (orthography in several scripts,
#: and emoji sequences); every other invisible character goes.
_KEEP = frozenset({"\n"}) | JOINERS

#: A draft's free-text facts (a programme, a start term) are cut to this, so a
#: profile can never make a draft's body too large to send.
FACT_MAX_LENGTH = 200


#: The `attempt` the background mentee draft sends (B6, decision 269). A press
#: counts from 1, so the two never share a body or a key, and every delivery of
#: the one background draft sends the same key, which the router replays.
BACKGROUND_ATTEMPT = 0
#: Names the background draft's `taskrelation_id`, which a row id names for a
#: press: a stable reference per user that is not their id.
_BACKGROUND_RELATIONS = UUID("5b6d1f0e-8c2a-5e4b-9f3d-6a7c0b1e2d3f")


def bio_draft_enabled(settings: Settings) -> bool:
    """Whether a finished mentee's bio is drafted in the background; the one reader."""
    return settings.mentee_bio_draft_enabled


def background_relation_id(user_id: UUID) -> str:
    """The background draft's `taskrelation_id`: the same for every delivery."""
    return str(uuid5(_BACKGROUND_RELATIONS, str(user_id)))


def daily_limit(settings: Settings) -> int:
    """Calls one user may make per field in `DAILY_WINDOW`; the one reader."""
    return settings.writing_assist_daily_limit


def is_known_prompt_version(version: str) -> bool:
    """`<task>-v<n>`, at most 64 characters, for a task that writes a bio."""
    if len(version) > PROMPT_VERSION_MAX_LENGTH or not _VERSION.fullmatch(version):
        return False
    return version.rsplit("-v", 1)[0] in PROVENANCE_TASKS


def source_size(source_data: dict[str, Any]) -> int:
    """The bytes the router counts: `json.dumps` with its default ASCII escapes,
    so a non-ASCII character costs its six- or twelve-byte escape."""
    return len(json.dumps(source_data).encode())


def fits(source_data: dict[str, Any], mode: WritingAssistMode) -> bool:
    """Whether the body is within the router's cap for the mode's task."""
    return source_size(source_data) <= TASKS[mode].max_source_bytes


@dataclass(frozen=True)
class AssistCall:
    """One request to the assistant, ready to send and safe to resend.

    `idempotency_key` is a function of the body, so an internal retry sends the
    same key with the same body, which is the only pairing the router accepts.
    """

    task: str
    source_data: dict[str, Any]
    idempotency_key: str
    #: Our own reference for the call (the `writing_assist_requests` row).
    relation_id: str


@dataclass(frozen=True)
class AssistResult:
    """What came back: the text, and the prompt version that wrote it."""

    text: str
    prompt_version: str


class WritingAssistant(Protocol):
    """Writes or rewrites a bio. Called through `asyncio.to_thread`."""

    #: False when the feature is off or unconfigured: nothing is called or counted.
    @property
    def available(self) -> bool: ...

    def run(self, call: AssistCall) -> AssistResult | None:
        """The answer, or `None` when there is none to give (any reason)."""
        ...


def source_for(
    field: WritingAssistField,
    mode: WritingAssistMode,
    *,
    attempt: int,
    facts: dict[str, Any] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """What the router is sent: `role` and `attempt` always, then the facts for a
    draft or the text for an improve, and nothing else."""
    source: dict[str, Any] = {"role": ROLES[field], "attempt": attempt}
    if mode is WritingAssistMode.IMPROVE:
        source["text"] = text
    else:
        source.update({key: _fact(value) for key, value in (facts or {}).items() if value})
    return source


def _fact(value: Any) -> Any:
    """A fact as sent: strings, and each string in a list, at most `FACT_MAX_LENGTH`."""
    if isinstance(value, str):
        return value[:FACT_MAX_LENGTH]
    if isinstance(value, list):
        return [_fact(item) for item in value]
    return value


def call_for(
    user_id: UUID,
    field: WritingAssistField,
    mode: WritingAssistMode,
    source_data: dict[str, Any],
    relation_id: str,
) -> AssistCall:
    """The call, keyed deterministically by who asked, for what, and the body.

    `attempt` is in the body, so a second press is a new key and a fresh
    answer, while a retry of one press is the same key and the same body.
    """
    canonical = json.dumps(
        {"user": str(user_id), "field": field.value, "mode": mode.value, "source": source_data},
        sort_keys=True,
        separators=(",", ":"),
    )
    return AssistCall(
        task=TASKS[mode].name,
        source_data=source_data,
        idempotency_key=hashlib.sha256(canonical.encode()).hexdigest(),
        relation_id=relation_id,
    )


def usable_text(raw: object, field: WritingAssistField) -> str | None:
    """The router's text, trimmed and without invisible characters, or `None`.

    Defence in depth: the router already validates its output. Empty after
    trimming, not a string, or over the field's bound is no answer, never a
    truncated one.
    """
    if not isinstance(raw, str):
        return None
    text = visible_only(raw.replace("\r\n", "\n"), keep=_KEEP).strip()
    if not text or len(text) > MAX_LENGTH[field]:
        return None
    return text


def answer_from(result: AssistResult | None, field: WritingAssistField) -> AssistResult | None:
    """What the router returned as it may be shown or saved, or `None`: usable
    text (`usable_text`) and a version a bio's provenance can record."""
    if result is None:
        return None
    text = usable_text(result.text, field)
    if text is None or not is_known_prompt_version(result.prompt_version):
        return None
    return AssistResult(text=text, prompt_version=result.prompt_version)


def window_retry_after(freeing_call_at: datetime, now: datetime) -> int:
    """Whole seconds until the call made at `freeing_call_at` leaves the window;
    never below one, since "retry in 0 seconds" invites a loop."""
    remaining = (freeing_call_at + DAILY_WINDOW - now).total_seconds()
    return max(1, math.ceil(remaining))


def provenance_problem(sent: dict[str, Any]) -> tuple[str, str] | None:
    """What is wrong with a profile write's provenance, as `(pointer, message)`.

    `sent` is the write's fields as sent. `about_me_drafted: true` needs the
    drafted text (`about_me`) and a bio prompt's version beside it; a version
    needs `about_me_drafted: true`; `false` needs nothing and clears both.
    """
    drafted = sent.get("about_me_drafted")
    version_sent = "about_me_prompt_version" in sent
    version = sent.get("about_me_prompt_version")
    if "about_me_drafted" in sent and drafted is None:
        return ("/about_me_drafted", "send true or false")
    if version_sent and drafted is not True:
        return ("/about_me_prompt_version", "send a prompt version only with about_me_drafted")
    if drafted is not True:
        return None
    if not isinstance(version, str) or not is_known_prompt_version(version):
        return ("/about_me_prompt_version", "send the prompt version the suggestion came with")
    about_me = sent.get("about_me")
    if not isinstance(about_me, str) or not about_me.strip():
        return ("/about_me", "send the drafted bio with about_me_drafted")
    return None
