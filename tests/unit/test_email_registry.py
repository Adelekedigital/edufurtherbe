"""The admin app's email list and the values its lines may use (PRD 28, decision 319)."""

from __future__ import annotations

import pytest

from app.domain.email_lines import (
    LINE_NAMES,
    LINES,
    PLACEHOLDER,
    SHARED_TEMPLATE_NAMES,
    TEMPLATE_GROUPS,
    case_of,
    variant_of,
)
from app.domain.email_registry import (
    EMAILS,
    GROUP_NAMES,
    SAMPLES,
    VALUES,
    VARIANT_LABELS,
    LineError,
    LineErrorCode,
    Recipient,
    check_line,
    entry,
    sample_context,
    values_for,
    variants_of,
)
from app.domain.enums import EmailLine
from app.domain.messages import ALIASES, RESOLVERS, build_variables
from app.domain.notifications import Notification

N = Notification
SHOWN = SHARED_TEMPLATE_NAMES
EVERY_LINE = [
    (email, variant_of(case), line)
    for email, cases in LINES.items()
    for case in cases
    for line in EmailLine
]


def test_every_email_is_registered_once() -> None:
    keys = [email.key for email in EMAILS]
    assert sorted(keys) == sorted(Notification)
    assert len(keys) == len(set(keys))


def test_a_shared_templates_emails_sit_together_in_the_list() -> None:
    order = [email.group for email in EMAILS]
    seen: list[str] = []
    for group in order:
        if not seen or seen[-1] != group:
            assert group not in seen, f"{group} is split in the list"
            seen.append(group)


def test_a_shared_groups_card_and_template_are_one_name() -> None:
    assert set(GROUP_NAMES) == set(TEMPLATE_GROUPS)
    for email in EMAILS:
        if email.group in GROUP_NAMES:
            assert email.template_name == email.group_name == GROUP_NAMES[email.group]
            assert email.own_template is None
        else:
            assert email.group_name == email.name


def test_an_email_of_its_own_template_is_a_group_of_one() -> None:
    cancelled = entry("session_cancelled")
    assert cancelled is not None
    assert cancelled.group == "session_cancelled"
    booked = entry("session_booked")
    assert booked is not None
    assert booked.group == "session_confirmed"
    assert entry("sign_in_code") is None
    assert entry("not_an_email") is None


def test_recipients_read_as_the_goes_to_filter() -> None:
    booked = entry("session_booked")
    received = entry("mentor_application_received")
    granted = entry("credits_granted")
    assert booked is not None and received is not None and granted is not None
    assert booked.recipients == (Recipient.MENTOR, Recipient.MENTEE)
    assert received.recipients == (Recipient.ADMINS,)
    assert granted.recipients == (Recipient.MENTEE,)


def test_every_variant_has_a_label_and_a_sample() -> None:
    for email in EMAILS:
        for variant in variants_of(email):
            assert VARIANT_LABELS[variant]
            if email.key in LINES:
                assert variant in SAMPLES[email.key]
    booked = entry("session_booked")
    assert booked is not None
    assert variants_of(booked) == ("mentor", "mentee")


def test_every_value_is_explained_and_is_a_word_a_line_may_say() -> None:
    names = [value.name for value in VALUES]
    assert len(names) == len(set(names))
    for value in VALUES:
        assert value.meaning and value.example and value.fallback
        assert ALIASES.get(value.name, value.name) in RESOLVERS
        assert value.name not in LINE_NAMES
        assert "—" not in value.meaning


@pytest.mark.parametrize(("email", "variant", "line"), EVERY_LINE)
def test_every_default_line_passes_its_own_check(
    email: Notification, variant: str, line: EmailLine
) -> None:
    """A default naming a value its email does not carry would be refused the
    moment an admin saved it unchanged."""
    text = getattr(LINES[email][case_of(variant)], line)
    assert check_line(email, variant, line, text) == []


@pytest.mark.parametrize(
    ("email", "variant", "side"),
    [
        (email, variant, side)
        for email in LINES
        for variant in SAMPLES[email]
        for side in ("mentor", "mentee")
    ],
)
def test_every_line_fills_from_its_sample_on_either_side(
    email: Notification, variant: str, side: str
) -> None:
    """Both parties' copies fill: `attendee` is the other person on each."""
    built = build_variables(SHOWN, sample_context(email, variant, side))
    assert all(built[name] for name in SHOWN), built
    assert not any("{" in value for value in built.values())


def test_every_unread_case_is_reached_by_its_sample() -> None:
    for case in LINES[N.MESSAGE_UNREAD_FEW]:
        assert SAMPLES[N.MESSAGE_UNREAD_FEW][case]["line_case"] == case


def test_a_mentor_only_email_is_previewed_as_the_mentor_reads_it() -> None:
    context = sample_context(N.SESSION_REQUESTED, "default")
    assert (context.recipient_name, context.recipient_role) == ("Ada Obi", "mentor")
    assert build_variables(["attendee"], context) == {"attendee": "Mo Bello"}
    booked = sample_context(N.SESSION_BOOKED, "mentee")
    assert (booked.recipient_name, booked.recipient_role) == ("Mo Bello", "mentee")


def test_a_new_application_is_previewed_as_the_admin_reads_it() -> None:
    context = sample_context(N.MENTOR_APPLICATION_RECEIVED, "default")
    assert (context.recipient_name, context.recipient_role) == ("Sam Eze", "")
    assert context.mentor_name == "Ada Obi"


def test_a_requests_sample_countdown_reads_as_its_example() -> None:
    """Built from now, so the preview never drifts to "0 hours left"."""
    context = sample_context(N.SESSION_REQUESTED, "default")
    assert build_variables(["hours"], context) == {"hours": "18"}


def test_hours_mean_something_only_on_a_request() -> None:
    assert "hours" in values_for(N.SESSION_REQUESTED, "default")
    assert "hours" in values_for(N.MENTEE_REQUEST_REMINDER, "default")
    assert "hours" not in values_for(N.SESSION_REMINDER, "default")
    assert "hours" not in values_for(N.SESSION_BOOKED, "mentor")


def test_values_follow_what_the_email_is_about() -> None:
    assert values_for(N.CREDITS_GRANTED, "default") == ("recipientFirstName", "recipientName")
    assert "creditCount" in values_for(N.CREDITS_RENEWED, "default")
    assert "sessionTopic" not in values_for(N.CREDITS_RENEWED, "default")
    assert "mentorName" in values_for(N.MENTOR_APPROVED, "default")
    assert "menteeName" not in values_for(N.MENTOR_APPROVED, "default")
    assert "reasonTitle" in values_for(N.REQUEST_DECLINED, "default")
    assert "reasonTitle" not in values_for(N.SESSION_BOOKED, "mentor")
    assert "unreadCount" not in values_for(N.MESSAGE_IN_SESSION_WINDOW, "default")


def test_an_empty_line_is_refused_alone() -> None:
    assert check_line(N.CREDITS_GRANTED, "default", EmailLine.SUBJECT, "  ") == [
        LineError(EmailLine.SUBJECT, LineErrorCode.EMPTY)
    ]


def test_the_hard_max_counts_the_raw_text() -> None:
    at_max = "x" * 32
    assert check_line(N.CREDITS_GRANTED, "default", EmailLine.BUTTON, at_max) == []
    assert check_line(N.CREDITS_GRANTED, "default", EmailLine.BUTTON, at_max + "x") == [
        LineError(EmailLine.BUTTON, LineErrorCode.TOO_LONG)
    ]


@pytest.mark.parametrize(
    ("text", "value", "suggestion"),
    [
        ("Hi {mentorname}", "mentorname", "mentorName"),
        ("On {SESSIONDATE}", "SESSIONDATE", "sessionDate"),
        ("Hi {menteName}", "menteName", "menteeName"),
        ("Hi {nobodyKnows}", "nobodyKnows", None),
        ("{emailSubject} again", "emailSubject", None),
        ("Open {sessionUrl}", "sessionUrl", None),
        ("With { menteeName }", "{ menteeName }", "menteeName"),
        ("With {mentee name}", "{mentee name}", "menteeName"),
        ("A lone { brace", "{ brace", None),
        ("A lone } brace", "}", None),
    ],
)
def test_an_unknown_value_is_named_with_a_suggestion(
    text: str, value: str, suggestion: str | None
) -> None:
    assert check_line(N.SESSION_REQUESTED, "default", EmailLine.SUBJECT, text) == [
        LineError(EmailLine.SUBJECT, LineErrorCode.UNKNOWN_VALUE, value, suggestion)
    ]


def test_a_value_the_email_does_not_carry_is_refused() -> None:
    assert check_line(N.CREDITS_GRANTED, "default", EmailLine.HEADING, "{creditCount} credits") == [
        LineError(EmailLine.HEADING, LineErrorCode.VALUE_NOT_SENT, "creditCount")
    ]


def test_every_reason_is_reported_once_per_value() -> None:
    text = "{creditCount} {creditCount} {nope} " + "x" * 100
    errors = check_line(N.CREDITS_GRANTED, "default", EmailLine.SUBJECT, text)
    assert [error.code for error in errors] == [
        LineErrorCode.TOO_LONG,
        LineErrorCode.VALUE_NOT_SENT,
        LineErrorCode.UNKNOWN_VALUE,
    ]


def test_every_placeholder_in_a_default_is_a_catalogue_value() -> None:
    names = {value.name for value in VALUES}
    for cases in LINES.values():
        for lines in cases.values():
            for line in EmailLine:
                assert set(PLACEHOLDER.findall(getattr(lines, line))) <= names
