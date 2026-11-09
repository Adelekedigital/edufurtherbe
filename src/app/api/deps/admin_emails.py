"""The admin Emails pages (PRD 28, decision 321). Every live admin grant may view
and edit (owner, 2026-11-09)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import Annotated

from fastapi import Body, Depends, Path
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.core import QueueViewerDep, SessionDep, SettingsDep
from app.api.schemas.admin_emails import (
    EmailDetailRead,
    EmailLastEdited,
    EmailLineRead,
    EmailLinesRead,
    EmailListItem,
    EmailSaveBody,
    EmailValueRead,
    EmailVariantRead,
    SampleRecipient,
    ValueRead,
)
from app.api.schemas.common import Page
from app.core.config import Settings
from app.core.errors import (
    ConfigurationError,
    EmailLineConflictError,
    EmailLineInvalidError,
    EmailNotFoundError,
    EmailNotSetUpError,
    UpstreamError,
)
from app.domain.email_admin import LineSave, StoredLine, default_text, line_view, plan_save
from app.domain.email_lines import LINES, group_of, marks_shared
from app.domain.email_registry import (
    EMAILS,
    VALUES,
    VARIANT_LABELS,
    EmailEntry,
    entry,
    sample_context,
    values_for,
    variants_of,
)
from app.domain.enums import EmailLine
from app.domain.messages import build_variables
from app.domain.notifications import Channel, Notification
from app.infra.clients.notifications import template_for, template_variables
from app.infra.db.email_lines import apply_line_writes, last_edits, stored_lines

__all__ = [
    "AdminEmailDep",
    "AdminEmailListDep",
    "AdminEmailSaveDep",
    "AdminEmailValuesDep",
]

#: Where an invalid line's text is, in the save's body.
LINE_POINTER = "/lines/{line}/text"


def _live(settings: Settings) -> frozenset[Notification]:
    """The emails whose mapped template is the shared one: it shows the lines
    (`marks_shared`). Anything else is Not set up, and its lines read only.

    Fails closed: with no provider, or Loops unreachable, nothing is Live, so
    no admin edit is accepted that the next send would not use.
    """
    templates = template_variables(settings)
    if templates is None:
        return frozenset()
    live = set()
    try:
        for email in EMAILS:
            if email.key not in LINES:
                continue
            try:
                template = template_for(settings, email.key, Channel.EMAIL)
            except ConfigurationError:
                continue
            if any(marks_shared(name) for name in templates.declared(template)):
                live.add(email.key)
    except UpstreamError:
        return frozenset()
    return frozenset(live)


def _item(
    email: EmailEntry,
    live: frozenset[Notification],
    edits: Mapping[str, tuple[str, object]],
) -> dict[str, object]:
    edited = edits.get(str(email.key))
    return {
        "key": str(email.key),
        "name": email.name,
        "trigger": email.trigger,
        "group": email.group,
        "group_name": email.group_name,
        "template_name": email.template_name,
        "recipients": [str(r) for r in email.recipients],
        "status": "live" if email.key in live else "not_set_up",
        "last_edited": (
            EmailLastEdited(admin_name=edited[0], at=edited[1]) if edited else None  # type: ignore[arg-type]
        ),
    }


def _value(value: object) -> ValueRead:
    return ValueRead.model_validate(value, from_attributes=True)


def _variant(
    email: EmailEntry, variant: str, stored: Mapping[tuple[str, EmailLine], StoredLine]
) -> EmailVariantRead:
    names = list(values_for(email.key, variant))
    context = sample_context(email.key, variant)
    sample = build_variables(names, context) if names else {}
    lines = {
        line: EmailLineRead.model_validate(
            line_view(default_text(email, variant, line), stored.get((variant, line))),
            from_attributes=True,
        )
        for line in EmailLine
    }
    first = context.recipient_first_name or "reader"
    return EmailVariantRead(
        id=variant,
        label=email.name if variant == "default" else VARIANT_LABELS[variant],
        sample={name: sample[name] for name in names},
        sample_recipient=SampleRecipient(
            name=context.recipient_name, email=f"{first.lower()}@example.com"
        ),
        lines=EmailLinesRead(**{str(line): view for line, view in lines.items()}),
    )


async def _detail(session: AsyncSession, settings: Settings, email: EmailEntry) -> EmailDetailRead:
    # One session, so its reads go one at a time; Loops is asked meanwhile.
    live_task = asyncio.create_task(asyncio.to_thread(_live, settings))
    edits = await last_edits(session)
    stored = await stored_lines(session, str(email.key))
    live = await live_task
    variants = variants_of(email)
    names = {name for variant in variants for name in values_for(email.key, variant)}
    return EmailDetailRead(
        **_item(email, live, edits),  # type: ignore[arg-type]
        siblings=max(len(group_of(email.key)) - 1, 0),
        values=[_value(value) for value in VALUES if value.name in names],
        variants=[_variant(email, variant, stored) for variant in variants],
    )


def _email(key: str) -> EmailEntry:
    found = entry(key)
    if found is None:
        raise EmailNotFoundError(f"no email {key!r}")
    return found


async def admin_email_list(
    _: QueueViewerDep, session: SessionDep, settings: SettingsDep
) -> Page[EmailListItem]:
    live_task = asyncio.create_task(asyncio.to_thread(_live, settings))
    edits = await last_edits(session)
    live = await live_task
    return Page(data=[EmailListItem(**_item(e, live, edits)) for e in EMAILS], next_cursor=None)  # type: ignore[arg-type]


async def admin_email(
    _: QueueViewerDep,
    session: SessionDep,
    settings: SettingsDep,
    key: Annotated[str, Path(max_length=64, description="The email's key.")],
) -> EmailDetailRead:
    return await _detail(session, settings, _email(key))


async def admin_email_values(_: QueueViewerDep) -> Page[EmailValueRead]:
    used: dict[str, list[str]] = {value.name: [] for value in VALUES}
    for email in EMAILS:
        names: Iterable[str] = {n for v in variants_of(email) for n in values_for(email.key, v)}
        for name in names:
            used[name].append(email.name)
    return Page(
        data=[
            EmailValueRead(**_value(value).model_dump(), used_in=used[value.name])
            for value in VALUES
        ],
        next_cursor=None,
    )


async def save_admin_email(
    admin_id: QueueViewerDep,
    session: SessionDep,
    settings: SettingsDep,
    key: Annotated[str, Path(max_length=64, description="The email's key.")],
    variant: Annotated[str, Path(max_length=64, description="The variant's id.")],
    body: Annotated[EmailSaveBody, Body()],
) -> EmailDetailRead:
    """Check every rule, then write the overrides and their history together."""
    email = _email(key)
    if variant not in variants_of(email):
        raise EmailNotFoundError(f"{key!r} has no variant {variant!r}")
    if email.key not in await asyncio.to_thread(_live, settings):
        raise EmailNotSetUpError(f"{key!r} has no shared template yet, so it is read only")
    sent = {
        line: LineSave(getattr(body.lines, line).text, getattr(body.lines, line).version)
        for line in EmailLine
    }
    rows = await stored_lines(session, key, variant=variant, lock=True)
    stored = {line: row for (_, line), row in rows.items()}
    plan = plan_save(email, variant, stored, sent, frozenset(body.keep_edit))
    if plan.conflicts:
        winner = stored.get(plan.conflicts[0])
        await session.rollback()
        raise _conflict(winner)
    if plan.errors:
        await session.rollback()
        raise EmailLineInvalidError(
            tuple(
                {
                    "pointer": LINE_POINTER.format(line=error.line),
                    "message": str(error.code),
                    "line": str(error.line),
                    "code": str(error.code),
                    **({"value": error.value} if error.value else {}),
                    **({"suggestion": error.suggestion} if error.suggestion else {}),
                }
                for error in plan.errors
            )
        )
    try:
        await apply_line_writes(
            session, email_key=key, variant=variant, writes=plan.writes, admin_id=admin_id
        )
        await session.commit()
    except IntegrityError:
        # A second admin's first save on the same line won the insert.
        await session.rollback()
        fresh = await stored_lines(session, key, variant=variant)
        raise _conflict(next(iter(fresh.values()), None)) from None
    return await _detail(session, settings, email)


def _conflict(winner: StoredLine | None) -> EmailLineConflictError:
    return EmailLineConflictError(
        updated_by_name=winner.updated_by_name if winner else "Another admin",
        updated_at=winner.updated_at.isoformat() if winner else "",
    )


AdminEmailListDep = Annotated[Page[EmailListItem], Depends(admin_email_list)]
AdminEmailDep = Annotated[EmailDetailRead, Depends(admin_email)]
AdminEmailValuesDep = Annotated[Page[EmailValueRead], Depends(admin_email_values)]
AdminEmailSaveDep = Annotated[EmailDetailRead, Depends(save_admin_email)]
