"""What a save of an email's lines changes, and how a line reads (PRD 28, decision 321)."""

from __future__ import annotations

import datetime as dt

from app.domain.email_admin import (
    LineSave,
    LineView,
    LineWrite,
    StoredLine,
    default_text,
    line_view,
    plan_save,
)
from app.domain.email_registry import LineErrorCode, entry
from app.domain.enums import EmailEditAction, EmailLine

S, H, B = EmailLine.SUBJECT, EmailLine.HEADING, EmailLine.BUTTON
AT = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.UTC)
GRANTED = entry("credits_granted")
assert GRANTED is not None
DEFAULTS = {line: default_text(GRANTED, "default", line) for line in EmailLine}


def stored(text: str, version: int = 1, default: str | None = None) -> StoredLine:
    return StoredLine(text, default if default is not None else DEFAULTS[S], version, "Sam Eze", AT)


def unchanged(**changes: LineSave) -> dict[EmailLine, LineSave]:
    sent = {line: LineSave(DEFAULTS[line], 0) for line in EmailLine}
    sent.update({EmailLine(k): v for k, v in changes.items()})
    return sent


def test_a_line_with_no_edit_reads_as_its_default() -> None:
    assert line_view("Hello", None) == LineView("Hello", "Hello", False, 0, False, None)


def test_an_edited_line_reads_its_text_and_spots_a_moved_default() -> None:
    view = line_view("New default", StoredLine("Mine", "Old default", 3, "Sam", AT))
    assert view == LineView("New default", "Mine", True, 3, True, "New default")
    same = line_view("Old default", StoredLine("Mine", "Old default", 3, "Sam", AT))
    assert (same.default_changed, same.new_default) == (False, None)


def test_an_email_of_its_own_template_has_no_default_lines() -> None:
    cancelled = entry("session_cancelled")
    assert cancelled is not None
    assert default_text(cancelled, "default", S) == ""


def test_saving_new_wording_is_an_edit_at_the_next_version() -> None:
    plan = plan_save(GRANTED, "default", {}, unchanged(subject=LineSave("Credits for you", 0)))
    assert plan.writes == (
        LineWrite(
            S,
            EmailEditAction.EDIT,
            "Credits for you",
            1,
            DEFAULTS[S],
            "Credits for you",
            DEFAULTS[S],
        ),
    )


def test_saving_nothing_new_writes_nothing() -> None:
    assert plan_save(GRANTED, "default", {}, unchanged()).writes == ()
    kept = plan_save(
        GRANTED, "default", {S: stored("Mine")}, unchanged(subject=LineSave("Mine", 1))
    )
    assert kept.writes == ()


def test_saving_the_default_is_a_reset_that_deletes_the_edit() -> None:
    plan = plan_save(
        GRANTED, "default", {S: stored("Mine", 2)}, unchanged(subject=LineSave(DEFAULTS[S], 2))
    )
    assert plan.writes == (
        LineWrite(S, EmailEditAction.RESET, None, 3, "Mine", DEFAULTS[S], DEFAULTS[S]),
    )


def test_keep_my_edit_records_only_when_the_default_moved() -> None:
    moved = {S: stored("Mine", 2, default="An older default")}
    plan = plan_save(
        GRANTED, "default", moved, unchanged(subject=LineSave("Mine", 2)), frozenset({S})
    )
    assert plan.writes == (
        LineWrite(S, EmailEditAction.KEEP_EDIT, "Mine", 3, "Mine", "Mine", DEFAULTS[S]),
    )
    current = {S: stored("Mine", 2)}
    assert (
        plan_save(
            GRANTED, "default", current, unchanged(subject=LineSave("Mine", 2)), frozenset({S})
        ).writes
        == ()
    )


def test_a_stale_version_is_a_conflict_and_nothing_else_is_judged() -> None:
    plan = plan_save(
        GRANTED, "default", {S: stored("Theirs", 2)}, unchanged(subject=LineSave("", 1))
    )
    assert (plan.conflicts, plan.errors, plan.writes) == ((S,), (), ())
    first = plan_save(GRANTED, "default", {}, unchanged(heading=LineSave("Mine", 1)))
    assert first.conflicts == (H,)


def test_any_invalid_line_refuses_the_whole_save() -> None:
    plan = plan_save(
        GRANTED,
        "default",
        {},
        unchanged(subject=LineSave("Fine", 0), button=LineSave("{creditCount}", 0)),
    )
    assert plan.writes == ()
    assert [(e.line, e.code) for e in plan.errors] == [(B, LineErrorCode.VALUE_NOT_SENT)]
