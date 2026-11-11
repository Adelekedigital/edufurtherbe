"""The send path's lines: an admin's override, fallbacks, and the safety net (PRD 28)."""

from __future__ import annotations

import dataclasses
import logging

import httpx
import pytest

from app.core.config import Settings
from app.domain import email_registry
from app.domain.email_registry import sample_context
from app.domain.messages import LineNote, MessageContext, UnresolvedVariableError, build_variables
from app.domain.notifications import Channel, Notification
from app.infra.clients.notifications import LoopsNotifier, _log_line_notes
from app.infra.clients.templates import StaticTemplates

N = Notification


def subject(context: MessageContext, notes: list[LineNote] | None = None) -> str:
    return build_variables(["emailSubject"], context, notes)["emailSubject"]


def test_an_override_wins_over_the_default() -> None:
    context = dataclasses.replace(
        sample_context(N.CREDITS_GRANTED, "default"),
        line_overrides={("default", "subject"): "Credits for {recipientFirstName}"},
    )
    notes: list[LineNote] = []
    assert subject(context, notes) == "Credits for Mo"
    assert notes == []


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
    assert build_variables(["buttonLabel"], mentor)["buttonLabel"] == "View session"


def test_an_empty_value_in_a_line_says_its_fallback_and_is_noted() -> None:
    context = dataclasses.replace(
        sample_context(N.CREDITS_RENEWED, "default"),
        extras={},
        line_overrides={("default", "heading"): "You have {creditCount} credits"},
    )
    notes: list[LineNote] = []
    built = build_variables(["emailHeading"], context, notes)
    assert built["emailHeading"] == "You have your credits"
    assert notes == [LineNote("fallback", "credits_renewed", "default", "heading", "creditCount")]


def test_a_default_line_falls_back_too() -> None:
    context = dataclasses.replace(sample_context(N.SESSION_REQUESTED, "default"), mentee_name="")
    notes: list[LineNote] = []
    assert subject(context, notes) == "New session request from your mentee"
    assert [note.kind for note in notes] == ["fallback"]


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
    notes: list[LineNote] = []
    assert subject(context, notes) == "Your session credits are here"
    assert notes == [LineNote("override_failed", "credits_granted", "default", "subject")]


def test_a_broken_override_is_noted_even_when_a_later_value_refuses() -> None:
    context = dataclasses.replace(
        sample_context(N.CREDITS_RENEWED, "default"),
        extras={},
        line_overrides={("default", "subject"): "Hello {aValueSinceRemoved}"},
    )
    notes: list[LineNote] = []
    with pytest.raises(UnresolvedVariableError, match="creditCount"):
        build_variables(["creditCount", "emailType", "emailSubject"], context, notes)
    assert [note.kind for note in notes] == ["override_failed"]


def test_copies_of_a_context_never_share_notes() -> None:
    base = dataclasses.replace(sample_context(N.SESSION_REQUESTED, "default"), mentee_name="")
    copy = dataclasses.replace(base)
    first: list[LineNote] = []
    second: list[LineNote] = []
    subject(base, first)
    subject(copy, second)
    assert len(first) == len(second) == 1
    assert subject(base) == "New session request from your mentee"


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
        "&utm_source=email&utm_medium=notification&utm_campaign=session_requested"
    )


def test_notes_are_logged_by_kind(caplog: pytest.LogCaptureFixture) -> None:
    notes = [
        LineNote("fallback", "credits_granted", "default", "subject", "recipientFirstName"),
        LineNote("override_failed", "credits_granted", "default", "heading"),
    ]
    with caplog.at_level(logging.WARNING, logger="app.infra.clients.notifications"):
        _log_line_notes(notes)

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "email_line_fallback"),
        (logging.ERROR, "email_line_override_failed"),
    ]
    assert caplog.records[0].value == "recipientFirstName"  # type: ignore[attr-defined]


def test_a_send_that_refuses_still_logs_its_broken_override(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The notifier logs what the lines worked around even when another value
    stops the send, so a broken override is reported, not lost."""
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    notifier = (
        LoopsNotifier("key", client=client)
        .with_settings(Settings(email_templates={"credits_renewed": "loops:tmpl_credits"}))
        .with_templates(
            StaticTemplates(
                {"tmpl_credits": frozenset({"emailType", "emailSubject", "creditCount"})}
            )
        )
    )
    context = dataclasses.replace(
        sample_context(N.CREDITS_RENEWED, "default"),
        extras={},
        line_overrides={("default", "subject"): "Hello {aValueSinceRemoved}"},
    )
    with (
        caplog.at_level(logging.WARNING, logger="app.infra.clients.notifications"),
        pytest.raises(UnresolvedVariableError, match="creditCount"),
    ):
        notifier.send(
            notification=N.CREDITS_RENEWED,
            channel=Channel.EMAIL,
            to="mo@example.test",
            context=context,
            idempotency_key="row-1",
        )
    assert [r.getMessage() for r in caplog.records] == ["email_line_override_failed"]
