"""Admins' email wording: what the send path reads and what the admin pages save
(PRD 28, decisions 318 and 321)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.email_admin import LineWrite, StoredLine
from app.domain.enums import EmailLine
from app.domain.names import joined_name
from app.infra.db.models import User
from app.infra.db.models.email_lines import EmailLineEdit, EmailLineOverride

__all__ = [
    "LineOverrides",
    "apply_line_writes",
    "last_edits",
    "last_line_edit",
    "line_saves",
    "load_line_overrides",
    "stored_lines",
]

#: Every override, ``email_key -> {(variant, line): text}``.
LineOverrides = dict[str, dict[tuple[str, str], str]]


async def load_line_overrides(session: AsyncSession) -> LineOverrides:
    """Every admin override, read once per sweep rather than once per message:
    the table holds at most three rows per variant of a few dozen emails."""
    rows = await session.execute(
        select(
            EmailLineOverride.email_key,
            EmailLineOverride.variant,
            EmailLineOverride.line,
            EmailLineOverride.override_text,
        )
    )
    found: LineOverrides = {}
    for email_key, variant, line, text in rows:
        found.setdefault(email_key, {})[(variant, str(line))] = text
    return found


#: Whom a change is credited to when the admin's account has no name.
UNNAMED_ADMIN = "An admin"


def _name(first: str | None, last: str | None) -> str:
    return joined_name(first, last) or UNNAMED_ADMIN


async def stored_lines(
    session: AsyncSession, email_key: str, *, variant: str | None = None, lock: bool = False
) -> dict[tuple[str, EmailLine], StoredLine]:
    """This email's overrides (one variant's, when given), with who saved each.

    ``lock`` takes the rows for update, so a save reads, checks and writes them
    in one transaction while a second admin's save waits its turn.
    """
    query = (
        select(EmailLineOverride, User.first_name, User.last_name)
        .join(User, User.id == EmailLineOverride.updated_by)
        .where(EmailLineOverride.email_key == email_key)
    )
    if variant is not None:
        query = query.where(EmailLineOverride.variant == variant)
    if lock:
        query = query.with_for_update(of=EmailLineOverride)
    rows = await session.execute(query)
    return {
        (row.variant, EmailLine(row.line)): StoredLine(
            text=row.override_text,
            default_at_edit=row.default_at_edit,
            version=row.version,
            updated_by_name=_name(first, last),
            updated_at=row.updated_at,
        )
        for row, first, last in rows
    }


async def last_edits(
    session: AsyncSession, email_key: str | None = None
) -> dict[str, tuple[str, dt.datetime]]:
    """Each email's newest history row (one email's, when given): who saved it
    and when, a reset included (the list's "Last edited"). One statement, by
    `ix_email_line_edits_email_key_created_at`."""
    query = (
        select(
            EmailLineEdit.email_key,
            EmailLineEdit.created_at,
            User.first_name,
            User.last_name,
        )
        .join(User, User.id == EmailLineEdit.admin_id)
        .distinct(EmailLineEdit.email_key)
        .order_by(EmailLineEdit.email_key, EmailLineEdit.created_at.desc(), EmailLineEdit.id.desc())
    )
    if email_key is not None:
        query = query.where(EmailLineEdit.email_key == email_key)
    rows = await session.execute(query)
    return {key: (_name(first, last), at) for key, at, first, last in rows}


async def line_saves(
    session: AsyncSession, email_key: str, variant: str | None = None
) -> dict[tuple[str, EmailLine], int]:
    """How many changes each line's history holds: its version when no edit is
    stored, so a version never repeats after a reset (decision 321)."""
    query = (
        select(EmailLineEdit.variant, EmailLineEdit.line, func.count())
        .where(EmailLineEdit.email_key == email_key)
        .group_by(EmailLineEdit.variant, EmailLineEdit.line)
    )
    if variant is not None:
        query = query.where(EmailLineEdit.variant == variant)
    rows = await session.execute(query)
    return {(v, EmailLine(line)): count for v, line, count in rows}


async def last_line_edit(
    session: AsyncSession, email_key: str, variant: str, lines: tuple[EmailLine, ...]
) -> tuple[str, dt.datetime] | None:
    """The newest change to any of these lines: who made it and when, so a
    conflict names who saved first even when they reset the line."""
    row = (
        await session.execute(
            select(EmailLineEdit.created_at, User.first_name, User.last_name)
            .join(User, User.id == EmailLineEdit.admin_id)
            .where(
                EmailLineEdit.email_key == email_key,
                EmailLineEdit.variant == variant,
                EmailLineEdit.line.in_(lines),
            )
            .order_by(EmailLineEdit.created_at.desc(), EmailLineEdit.id.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else (_name(row.first_name, row.last_name), row.created_at)


async def apply_line_writes(
    session: AsyncSession,
    *,
    email_key: str,
    variant: str,
    writes: Iterable[LineWrite],
    admin_id: UUID,
) -> None:
    """Write a save's plan and its history, inside the caller's transaction.

    An edit updates the row read under lock, or inserts one at the plan's
    version when there is none; a reset deletes it. A second admin's concurrent
    insert on the same line meets the unique key and raises, which the caller
    turns into a conflict.
    """
    for write in writes:
        key = (
            EmailLineOverride.email_key == email_key,
            EmailLineOverride.variant == variant,
            EmailLineOverride.line == write.line,
        )
        if write.text is None:
            await session.execute(delete(EmailLineOverride).where(*key))
        else:
            updated = await session.execute(
                update(EmailLineOverride)
                .where(*key)
                .values(
                    override_text=write.text,
                    default_at_edit=write.default,
                    version=write.version,
                    updated_by=admin_id,
                )
            )
            if not updated.rowcount:  # type: ignore[attr-defined]
                await session.execute(
                    insert(EmailLineOverride).values(
                        email_key=email_key,
                        variant=variant,
                        line=write.line,
                        override_text=write.text,
                        default_at_edit=write.default,
                        version=write.version,
                        updated_by=admin_id,
                    )
                )
        await session.execute(
            insert(EmailLineEdit).values(
                email_key=email_key,
                variant=variant,
                line=write.line,
                before_text=write.before,
                after_text=write.after,
                default_text=write.default,
                action=write.action,
                admin_id=admin_id,
            )
        )
