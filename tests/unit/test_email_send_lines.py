"""The send path's lines: an admin's override, fallbacks, and the safety net (PRD 28)."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from app.domain import email_registry
from app.domain.email_registry import sample_context
from app.domain.messages import LineNote, UnresolvedVariableError, build_variables
from app.domain.notifications import Notification
from app.infra.clients.notifications import _log_line_notes

N = Notification


def subject(context: object) -> str:
    return build_variables(["emailSubject"], context)["emailSubject"]  # type: ignore[arg-type]


def test_an_override_wins_over_the_default() -> None:
    context = dataclasses.replace(
        sample_context(N.CREDITS_GRANTED, "default"),
        line_overrides={("default", "subject"): "Credits for {recipientFirstName}"},
    )
    assert subject(context) == "Credits for Mo"
    assert context.line_notes == []


def test_no_override_sends_the_default() -> None:
    assert subject(sample_context(N.CREDITS_GRANTED, "default")) == "Your session credits are here"


def test_an_override_reaches_only_its_own_variant_and_line() -> None:
    overrides = {("mentor", "subject"): "For the mentor", ("mentor", "heading"): "Mentor heading"}
    mentee = dataclasses.replace(
        sample_context(N.SESSION_BOOKED, "mentee"), line_overrides=overrides
    )
    mentor = dataclasses.replace(
        sample_context(N.SESSION_BOOKED, "mentor"), line_overrides=overrides
    )
    assert subject(mentee) == "Your session with Ada Obi is booked"
    assert subject(mentor) == "For the mentor"
    assert build_variables(["buttonLabel"], mentor) == {"buttonLabel": "View session"}


def test_an_empty_value_in_a_line_says_its_fallback_and_is_noted() -> None:
    context = dataclasses.replace(
        sample_context(N.CREDITS_RENEWED, "default"),
        extras={},
        line_overrides={("default", "heading"): "You have {creditCount} credits"},
    )
    assert build_variables(["emailHeading"], context) == {"emailHeading": "You have your credits"}
    assert context.line_notes == [
        LineNote("fallback", "credits_renewed", "default", "heading", "creditCount")
    ]


def test_a_default_line_falls_back_too() -> None:
    context = dataclasses.replace(sample_context(N.SESSION_REQUESTED, "default"), mentee_name="")
    assert subject(context) == "New session request from your mentee"
    assert [note.kind for note in context.line_notes] == ["fallback"]


def test_a_value_with_no_fallback_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(email_registry.FALLBACKS, "menteeName", None)
    context = dataclasses.replace(sample_context(N.SESSION_REQUESTED, "default"), mentee_name="")
    with pytest.raises(UnresolvedVariableError, match="menteeName"):
        subject(context)


def test_an_override_that_cannot_fill_sends_the_default_and_is_an_error() -> None:
    context = dataclasses.replace(
        sample_context(N.CREDITS_GRANTED, "default"),
        line_overrides={("default", "subject"): "Hello {aValueSinceRemoved}"},
    )
    assert subject(context) == "Your session credits are here"
    assert context.line_notes == [
        LineNote("override_failed", "credits_granted", "default", "subject")
    ]


def test_the_rest_of_a_template_still_refuses_an_empty_value() -> None:
    context = dataclasses.replace(sample_context(N.CREDITS_RENEWED, "default"), extras={})
    with pytest.raises(UnresolvedVariableError, match="creditCount"):
        build_variables(["emailType", "creditCount", "emailSubject"], context)


def test_the_button_link_is_never_the_admins() -> None:
    context = dataclasses.replace(
        sample_context(N.SESSION_REQUESTED, "default"),
        line_overrides={("default", "button"): "Answer now"},
    )
    built = build_variables(["buttonLabel", "buttonUrl"], context)
    assert built["buttonLabel"] == "Answer now"
    assert built["buttonUrl"].endswith(
        "/bookings?tab=pending&booking=00000000-0000-7000-8000-0000000000aa"
    )


def test_notes_are_logged_by_kind_then_cleared(caplog: pytest.LogCaptureFixture) -> None:
    context = sample_context(N.CREDITS_GRANTED, "default")
    context.line_notes.extend(
        [
            LineNote("fallback", "credits_granted", "default", "subject", "recipientFirstName"),
            LineNote("override_failed", "credits_granted", "default", "heading"),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.infra.clients.notifications"):
        _log_line_notes(context)

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "email_line_fallback"),
        (logging.ERROR, "email_line_override_failed"),
    ]
    assert caplog.records[0].value == "recipientFirstName"  # type: ignore[attr-defined]
    assert context.line_notes == []
