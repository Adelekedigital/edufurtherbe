"""Templates several emails share, and the lines the backend writes for them (decision 317)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from itertools import combinations
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain import email_lines
from app.domain.email_lines import (
    LINE_NAMES,
    LINES,
    OWNED_BY,
    PLACEHOLDER,
    TEMPLATE_GROUPS,
    EmailLines,
    group_of,
)
from app.domain.message_emails import EmailedMessage, instant_email, unread_email
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
SHOWN = ["emailType", "emailSubject", "emailHeading", "buttonLabel", "buttonUrl"]
DOC = Path(__file__).resolve().parents[2] / "docs" / "loops-template-variables.md"


SENDERS = {name: uuid4() for name in ("Ada", "Ben", "Chi")}


def _said(first: str, minutes: int = 0) -> EmailedMessage:
    sent_at = START + dt.timedelta(minutes=minutes)
    return EmailedMessage(SENDERS[first], first, "Obi", "Hello", None, sent_at)


#: What each email's producer actually puts on its outbox row (`variables=` at
#: the enqueue, or the message email's values), so a line naming a value its
#: email never carries fails here rather than at send time.
CARRIED: dict[Notification, list[dict[str, str]]] = {
    N.SESSION_BOOKED: [{}],
    N.REQUEST_ACCEPTED: [{}],
    N.SESSION_REQUESTED: [{}],
    N.MENTOR_RESPONSE_REMINDER: [{"kind": "t24"}],
    N.MENTEE_REQUEST_REMINDER: [{"reminder_message": "Still keen"}],
    N.REQUEST_DECLINED: [{"reason_code": "scheduling_conflict"}],
    N.REQUEST_WITHDRAWN: [{"reason_text": "Sorry"}],
    N.SESSION_REMINDER: [{"kind": "s24", "interval": "Tomorrow"}],
    N.SESSION_LAST_REMINDER: [{"kind": "s5", "interval": "in 5 Minutes"}],
    N.SESSION_TIME_SUGGESTED: [
        {
            "suggested_after": "declined",
            "suggested_starts_at": START.isoformat(),
            "held_until": START.isoformat(),
        }
    ],
    N.SESSION_SUGGESTION_REMINDER: [
        {"suggested_starts_at": START.isoformat(), "held_until": START.isoformat()}
    ],
    N.CREDITS_GRANTED: [{}],
    N.CREDITS_RENEWED: [{"credit_count": "2"}],
    N.CREDITS_EXPIRING: [{"kind": "c14", "interval": "two weeks", "credit_count": "1"}],
    N.MENTOR_APPROVED: [{}],
    N.MENTOR_DECLINED: [{"reason": "Not yet"}],
    N.MENTOR_PAUSED_BY_ADMIN: [{}],
    N.MESSAGE_UNREAD_FEW: [
        unread_email([_said("Ada")]),
        unread_email([_said("Ada"), _said("Ada", 1)]),
        unread_email([_said("Ada"), _said("Ben", 1)]),
    ],
    N.MESSAGE_UNREAD_MANY: [unread_email([_said("Ada"), _said("Ben"), _said("Chi")])],
    N.MESSAGE_IN_SESSION_WINDOW: [instant_email(_said("Ada"))],
}

#: Everything any grouped email may carry, so every line can be filled.
EXTRAS = {
    "interval": "Tomorrow",
    "suggested_starts_at": START.isoformat(),
    "held_until": START.isoformat(),
    "suggested_after": "declined",
    "reminder_message": "Still keen",
    "credit_count": "2",
    "reply_to": "u-1",
    "unread_count": "2",
    "replies_name": "Ada",
    "sender_first_names": "Ada and Ben",
    "sender_first_name": "Ada",
    "preview_text": "Hi",
}


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


def test_every_grouped_email_says_what_it_carries() -> None:
    assert set(CARRIED) == set(LINES)


@pytest.mark.parametrize(
    ("notification", "extras", "role"),
    [
        (member, carried, role)
        for member, rows in CARRIED.items()
        for carried in rows
        for role in ("mentor", "mentee")
    ],
)
def test_every_line_fills_from_what_its_email_carries(
    notification: Notification, extras: dict[str, str], role: str
) -> None:
    built = build_variables(SHOWN, a_context(notification, recipient_role=role, extras=extras))
    assert all(built.values()), built
    assert not any("{" in value for value in built.values())


def test_every_case_is_reached() -> None:
    reached = {
        case
        for member, rows in CARRIED.items()
        for row in rows
        for case in (row.get("line_case", ""),)
    }
    named = {case for cases in LINES.values() for case in cases} - {"", "mentor", "mentee"}
    assert named <= reached


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
    granted = a_context(N.CREDITS_GRANTED, extras={})
    assert build_variables(["emailType", "creditCount", "intervalTime"], granted) == {
        "emailType": "credits_granted",
        "creditCount": "",
        "intervalTime": "",
    }
    window = a_context(N.MESSAGE_IN_SESSION_WINDOW, extras={})
    assert build_variables(["emailType", "unreadCount", "senderFirstNames"], window) == {
        "emailType": "message_in_session_window",
        "unreadCount": "",
        "senderFirstNames": "",
    }


def test_a_template_one_email_still_uses_alone_fails_as_before() -> None:
    """Only a shared template (it declares `emailType`) gets the empty value:
    a separate template asking for another email's variable is a template
    pointed at the wrong email."""
    with pytest.raises(UnresolvedVariableError, match="creditCount"):
        build_variables(["creditCount"], a_context(N.CREDITS_GRANTED, extras={}))


def test_an_owner_still_needs_its_variable() -> None:
    with pytest.raises(UnresolvedVariableError, match="creditCount"):
        build_variables(["emailType", "creditCount"], a_context(N.CREDITS_RENEWED, extras={}))


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
    table's CHECK (decision 318)."""
    assert max(len(member.value) for member in N) <= email_lines.LINE_KEY_MAX_LENGTH
    for cases in LINES.values():
        for case, lines in cases.items():
            assert len(case) <= email_lines.LINE_KEY_MAX_LENGTH
            for field in email_lines.LINE_FIELDS:
                assert 1 <= len(getattr(lines, field)) <= email_lines.LINE_HARD_MAX[field]


def test_the_rewordable_fields_are_the_lines_words_and_each_has_a_max() -> None:
    assert email_lines.LINE_FIELDS == ("subject", "heading", "button")
    assert tuple(email_lines.LINE_HARD_MAX) == email_lines.LINE_FIELDS
