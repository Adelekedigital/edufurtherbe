"""Templates several emails share, and the lines the backend writes for them (decision 317)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from itertools import combinations
from pathlib import Path

import pytest

from app.domain import email_lines
from app.domain.email_lines import (
    LINE_NAMES,
    LINES,
    OWNED_BY,
    PLACEHOLDER,
    SHARED_TEMPLATE_NAMES,
    TEMPLATE_GROUPS,
    EmailLines,
    group_of,
)
from app.domain.email_registry import SAMPLES
from app.domain.enums import EmailLine
from app.domain.messages import (
    ALIASES,
    RESOLVERS,
    MessageContext,
    UnresolvedVariableError,
    build_variables,
)
from app.domain.notifications import Notification

N = Notification
START = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.UTC)
SHOWN = SHARED_TEMPLATE_NAMES
DOC = Path(__file__).resolve().parents[2] / "docs" / "loops-template-variables.md"


#: A row with nothing on it; the tests that need values pass their own.
EXTRAS: dict[str, str] = {}


def a_context(notification: Notification | None, **overrides: object) -> MessageContext:
    base: dict[str, object] = {
        "recipient_name": "Mo Bello",
        "recipient_first_name": "Mo",
        "recipient_timezone": "Africa/Lagos",
        "mentor_name": "Ada Obi",
        "mentee_name": "Mo Bello",
        "starts_at": START,
        "respond_by": START + dt.timedelta(days=3),
        "session_id": "s-1",
        "app_base_url": "https://app.example.test",
        "extras": EXTRAS,
        "notification": notification,
    }
    return MessageContext(**(base | overrides))  # type: ignore[arg-type]


def test_an_email_shares_at_most_one_template() -> None:
    for first, second in combinations(TEMPLATE_GROUPS.values(), 2):
        assert not first & second


def test_every_grouped_email_has_lines_and_only_those_do() -> None:
    grouped = frozenset().union(*TEMPLATE_GROUPS.values())
    assert set(LINES) == grouped


def test_every_owner_is_in_a_group() -> None:
    for owners in OWNED_BY.values():
        assert all(group_of(owner) for owner in owners)


def test_the_docs_list_the_same_groups() -> None:
    """The operator pastes ids from the doc's table: it cannot drift (rule 8)."""
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("## Shared templates") : text.index("**What a shared template")]
    documented = {
        found.group(1): frozenset(N(m) for m in re.findall(r"`([a-z_]+)`", found.group(2)))
        for found in re.finditer(r"^\| ([a-z_]+) \| (.+) \|$", section, re.MULTILINE)
        if found.group(1) != "template"
    }
    assert documented == TEMPLATE_GROUPS


def test_every_placeholder_names_a_variable_and_never_a_line() -> None:
    for cases in LINES.values():
        for lines in cases.values():
            for text in (lines.subject, lines.heading, lines.button):
                for name in PLACEHOLDER.findall(text):
                    assert ALIASES.get(name, name) in RESOLVERS, name
                    assert name not in LINE_NAMES
            assert lines.link in RESOLVERS
            assert "—" not in lines.subject + lines.heading + lines.button


def test_every_grouped_email_has_a_sample_per_variant() -> None:
    """`SAMPLES` is the one table of what each producer carries (rule 8): the
    registry's tests fill every line from it."""
    assert set(SAMPLES) == set(LINES)
    for email, cases in LINES.items():
        assert set(SAMPLES[email]) == {email_lines.variant_of(case) for case in cases}


def test_a_session_booked_reads_from_each_side() -> None:
    mentor = build_variables(["emailSubject"], a_context(N.SESSION_BOOKED, recipient_role="mentor"))
    mentee = build_variables(["emailSubject"], a_context(N.SESSION_BOOKED, recipient_role="mentee"))
    assert mentor["emailSubject"] == "New session booked with Mo Bello"
    assert mentee["emailSubject"] == "Your session with Ada Obi is booked"


def test_the_button_opens_the_emails_own_link() -> None:
    built = build_variables(["buttonUrl"], a_context(N.SESSION_REQUESTED))
    assert built["buttonUrl"] == "https://app.example.test/bookings?tab=pending&booking=s-1"


def test_which_email_and_which_side_travel_as_values() -> None:
    built = build_variables(
        ["emailType", "recipientRole"], a_context(N.CREDITS_RENEWED, recipient_role="")
    )
    assert built == {"emailType": "credits_renewed", "recipientRole": ""}
    with pytest.raises(UnresolvedVariableError, match="emailType"):
        build_variables(["emailType"], a_context(None))


def test_a_variable_another_email_owns_is_empty_within_the_group() -> None:
    granted = a_context(N.CREDITS_GRANTED, extras={"credit_count": "2"})
    assert build_variables(["emailType", "creditCount", "intervalTime"], granted) == {
        "emailType": "credits_granted",
        "creditCount": "2",
        "intervalTime": "",
        "recipientRole": "",
    }
    window = a_context(N.MESSAGE_IN_SESSION_WINDOW, extras={})
    assert build_variables(["emailType", "unreadCount", "senderFirstNames"], window) == {
        "emailType": "message_in_session_window",
        "unreadCount": "",
        "senderFirstNames": "",
        "recipientRole": "",
    }


def test_a_template_showing_the_lines_is_shared_and_always_gets_its_switches() -> None:
    """Loops lists only what a template shows, never `emailType`, which only
    picks its sections: the lines mark it shared, and the switches always go."""
    built = build_variables(
        ["emailSubject", "intervalTime"], a_context(N.CREDITS_GRANTED, extras={})
    )
    assert built == {
        "emailSubject": "Your session credits are here",
        "intervalTime": "",
        "emailType": "credits_granted",
        "recipientRole": "",
    }


def test_a_mentors_copy_carries_its_side_and_the_link_alone_marks_nothing() -> None:
    built = build_variables(
        ["buttonLabel"], a_context(N.SESSION_BOOKED, recipient_role="mentor", extras={})
    )
    assert (built["emailType"], built["recipientRole"]) == ("session_booked", "mentor")
    alone = build_variables(["buttonUrl"], a_context(N.SESSION_BOOKED, recipient_role="mentor"))
    assert set(alone) == {"buttonUrl"}


def test_a_template_with_neither_lines_nor_type_gets_no_switches() -> None:
    built = build_variables(["recipientName"], a_context(N.CREDITS_GRANTED, extras={}))
    assert built == {"recipientName": "Mo Bello"}


def test_a_template_one_email_still_uses_alone_fails_as_before() -> None:
    """Only a shared template (`marks_shared`) gets the empty value:
    a separate template asking for another email's variable is a template
    pointed at the wrong email."""
    with pytest.raises(UnresolvedVariableError, match="intervalTime"):
        build_variables(["intervalTime"], a_context(N.CREDITS_GRANTED, extras={}))


@pytest.mark.parametrize("email", [N.CREDITS_GRANTED, N.CREDITS_RENEWED])
def test_an_owner_still_needs_its_variable(email: Notification) -> None:
    with pytest.raises(UnresolvedVariableError, match="creditCount"):
        build_variables(["emailType", "creditCount"], a_context(email, extras={}))


def test_outside_the_group_a_variable_still_fails_by_name() -> None:
    with pytest.raises(UnresolvedVariableError, match="unreadCount"):
        build_variables(["emailType", "unreadCount"], a_context(N.SESSION_BOOKED, extras={}))
    with pytest.raises(UnresolvedVariableError, match="creditCount"):
        build_variables(["emailType", "creditCount"], a_context(N.REVIEW_REQUESTED, extras={}))


def test_an_email_with_no_lines_is_refused_by_name() -> None:
    with pytest.raises(UnresolvedVariableError, match="emailSubject"):
        build_variables(["emailSubject"], a_context(N.CALENDAR_DISCONNECTED))


def test_a_line_naming_a_line_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    looped = dataclasses.replace(LINES[N.CREDITS_GRANTED][""], subject="{emailSubject}")
    assert isinstance(looped, EmailLines)
    monkeypatch.setitem(email_lines.LINES, N.CREDITS_GRANTED, {"": looped})
    with pytest.raises(UnresolvedVariableError, match="cannot name"):
        build_variables(["emailSubject"], a_context(N.CREDITS_GRANTED))


def test_a_case_or_a_side_wins_over_the_emails_own_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = LINES[N.CREDITS_GRANTED][""]
    side = dataclasses.replace(own, subject="for mentees")
    case = dataclasses.replace(own, subject="for the case")
    monkeypatch.setitem(email_lines.LINES, N.CREDITS_GRANTED, {"": own, "mentee": side, "x": case})
    assert email_lines.lines_for(N.CREDITS_GRANTED, "x", "mentee") is case
    assert email_lines.lines_for(N.CREDITS_GRANTED, "", "mentee") is side
    assert email_lines.lines_for(N.CREDITS_GRANTED, "", "") is own
    assert email_lines.lines_for(N.CALENDAR_DISCONNECTED, "", "") is None


def test_every_override_key_and_default_fits_the_tables_bounds() -> None:
    """An admin copying a default into an override must never meet the
    table's CHECK (decision 318): each key is 1 to 64 as stored."""
    assert max(len(member.value) for member in N) <= email_lines.LINE_KEY_MAX_LENGTH
    for cases in LINES.values():
        for case, lines in cases.items():
            assert 1 <= len(email_lines.variant_of(case)) <= email_lines.LINE_KEY_MAX_LENGTH
            for line in EmailLine:
                assert 1 <= len(getattr(lines, line)) <= email_lines.LINE_HARD_MAX[line]


def test_the_rewordable_lines_are_every_field_but_the_link_and_each_has_a_max() -> None:
    words = {field.name for field in dataclasses.fields(EmailLines)} - {"link"}
    assert {line.value for line in EmailLine} == words
    assert set(email_lines.LINE_HARD_MAX) == set(EmailLine)


def test_an_emails_own_case_is_default_outside_the_code_and_back() -> None:
    assert email_lines.variant_of("") == email_lines.DEFAULT_VARIANT == "default"
    assert email_lines.case_of("default") == ""
    for cases in LINES.values():
        for case in cases:
            assert email_lines.case_of(email_lines.variant_of(case)) == case
