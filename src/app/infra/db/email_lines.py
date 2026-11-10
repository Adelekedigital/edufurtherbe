"""Admins' email wording, as the send path reads it (PRD 28, decision 318)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.email_lines import EmailLineOverride

__all__ = ["LineOverrides", "load_line_overrides"]

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
