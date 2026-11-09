"""What the admin Emails pages show and what a save changes (PRD 28, decision 321).

Pure: the caller loads the stored overrides and the edit history, and this turns
them into views and into a save plan. The registry (`domain.email_registry`)
and the defaults (`domain.email_lines.LINES`) are the facts; a stored override
is only ever an edit over a default, so saving the default deletes it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.email_lines import LINES, case_of
from app.domain.email_registry import EmailEntry, LineError, check_line
from app.domain.enums import EmailEditAction, EmailLine

__all__ = [
    "LineSave",
    "LineView",
    "LineWrite",
    "SavePlan",
    "StoredLine",
    "default_text",
    "line_view",
    "plan_save",
]


@dataclass(frozen=True, slots=True)
class StoredLine:
    """One saved override, as the table holds it."""

    text: str
    default_at_edit: str
    version: int
    updated_by_name: str
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class LineView:
    """One line as the editor shows it (PRD 28, *API*)."""

    default_text: str
    text: str
    edited: bool
    version: int
    default_changed: bool
    new_default: str | None


def default_text(email: EmailEntry, variant: str, line: EmailLine) -> str:
    """The code's wording for this line, or empty for an email with none (its
    template is its own, so its lines are not the backend's)."""
    cases = LINES.get(email.key)
    if cases is None:
        return ""
    return str(getattr(cases[case_of(variant)], line))


def line_view(default: str, stored: StoredLine | None) -> LineView:
    """The line as sent now, and whether the default moved since it was edited."""
    if stored is None:
        return LineView(default, default, False, 0, False, None)
    changed = stored.default_at_edit != default
    return LineView(
        default_text=default,
        text=stored.text,
        edited=stored.text != default,
        version=stored.version,
        default_changed=changed,
        new_default=default if changed else None,
    )


@dataclass(frozen=True, slots=True)
class LineSave:
    """What the editor sent for one line: the text, and the version it read."""

    text: str
    version: int


@dataclass(frozen=True, slots=True)
class LineWrite:
    """One change to make: ``text`` ``None`` deletes the override (a reset)."""

    line: EmailLine
    action: EmailEditAction
    text: str | None
    version: int
    before: str
    after: str
    default: str


@dataclass(frozen=True, slots=True)
class SavePlan:
    """A save's outcome: stale lines (a 409), invalid lines (a 422), or writes."""

    conflicts: tuple[EmailLine, ...]
    errors: tuple[LineError, ...]
    writes: tuple[LineWrite, ...]


def plan_save(
    email: EmailEntry,
    variant: str,
    stored: Mapping[EmailLine, StoredLine],
    lines: Mapping[EmailLine, LineSave],
    keep_edit: frozenset[EmailLine] = frozenset(),
) -> SavePlan:
    """What saving ``lines`` for this variant does (PRD 28, *Rules*).

    **Versions first:** a line another admin saved since it was read is a
    conflict and nothing is written. Then every line is checked; any error
    refuses the whole save. Saving the default deletes the override (a reset);
    saving the same text changes nothing, unless "Keep my edit" was pressed on
    a line whose default has moved, which records that the admin saw it.
    """
    conflicts = tuple(
        line
        for line, sent in lines.items()
        if sent.version != (stored[line].version if line in stored else 0)
    )
    if conflicts:
        return SavePlan(conflicts, (), ())
    errors = tuple(
        error
        for line, sent in lines.items()
        for error in check_line(email.key, variant, line, sent.text)
    )
    if errors:
        return SavePlan((), errors, ())
    writes = []
    for line, sent in lines.items():
        default = default_text(email, variant, line)
        current = stored.get(line)
        before = current.text if current else default
        version = (current.version if current else 0) + 1
        if sent.text == default:
            if current is not None:
                writes.append(
                    LineWrite(line, EmailEditAction.RESET, None, version, before, default, default)
                )
        elif current is not None and sent.text == current.text:
            if line in keep_edit and current.default_at_edit != default:
                writes.append(
                    LineWrite(
                        line, EmailEditAction.KEEP_EDIT, sent.text, version, before, before, default
                    )
                )
        else:
            writes.append(
                LineWrite(
                    line, EmailEditAction.EDIT, sent.text, version, before, sent.text, default
                )
            )
    return SavePlan((), (), tuple(writes))
