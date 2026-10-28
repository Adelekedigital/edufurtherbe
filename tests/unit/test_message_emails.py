"""The message emails' rules and words (decision 303), with no database.

Which email a batch makes, what each says, when the hour ends, and that every
name the docs offer a template resolves from what the email carries, in the
recipient's zone.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest

from app.domain.chats import PREVIEW_LENGTH
from app.domain.message_emails import (
    SENDER_ROWS,
    UNREAD_EMAIL_INTERVAL,
    EmailedMessage,
    held_until,
    instant_email,
    unread_email,
    unread_kind,
)
from app.domain.messages import MessageContext, UnresolvedVariableError, build_variables
from app.domain.notifications import Notification

NOW = dt.datetime(2026, 10, 28, 14, 0, tzinfo=dt.UTC)
ADA = uuid4()
BEN = uuid4()
LONG = "word " * 60


def said(
    body: str | None = "Hello",
    *,
    sender: UUID = ADA,
    first: str | None = "Ada",
    last: str | None = "Obi",
    file_name: str | None = None,
    minutes: int = 0,
) -> EmailedMessage:
    return EmailedMessage(
        sender_id=sender,
        first_name=first,
        last_name=last,
        body=body,
        file_name=file_name,
        sent_at=NOW + dt.timedelta(minutes=minutes),
    )


def context(extras: dict[str, str], **overrides: object) -> MessageContext:
    base: dict[str, object] = {
        "recipient_name": "Mo Bello",
        "recipient_first_name": "Mo",
        "recipient_timezone": "Africa/Lagos",
        "mentor_name": "",
        "mentee_name": "",
        "app_base_url": "https://app.example.test",
        "extras": extras,
    }
    return MessageContext(**(base | overrides))  # type: ignore[arg-type]


def test_two_messages_are_few_and_three_are_many() -> None:
    assert unread_kind(1) is Notification.MESSAGE_UNREAD_FEW
    assert unread_kind(2) is Notification.MESSAGE_UNREAD_FEW
    assert unread_kind(3) is Notification.MESSAGE_UNREAD_MANY


def test_the_hour_holds_from_the_last_email_and_then_lets_go() -> None:
    assert held_until(None, NOW) is None
    last = NOW - dt.timedelta(minutes=20)
    assert held_until(last, NOW) == last + UNREAD_EMAIL_INTERVAL
    assert held_until(NOW - UNREAD_EMAIL_INTERVAL, NOW) is None


def test_the_hour_ends_on_a_whole_second() -> None:
    """QStash's not-before is whole seconds: a job asked for a fraction early
    would arrive still held and loop."""
    last = NOW - dt.timedelta(minutes=20, microseconds=-1)
    assert held_until(last, NOW) == NOW + dt.timedelta(minutes=40, seconds=1)


def test_one_message_names_its_sender_and_cuts_the_text() -> None:
    values = unread_email([said(LONG)])

    assert values["email_subject"] == "Ada sent you a message"
    assert values["email_heading"] == "Ada sent you a message"
    assert values["button_label"] == "Reply in Messages"
    assert values["reply_to"] == str(ADA)
    assert values["replies_name"] == "Ada"
    assert len(values["message1_text"]) <= PREVIEW_LENGTH
    assert values["message1_text"].endswith("…")
    assert values["preview_text"] == values["message1_text"]
    assert values["message1_name"] == "Ada Obi"
    assert values["message1_initials"] == "AO"
    assert "message2_name" not in values


def test_two_from_one_person_count_them() -> None:
    values = unread_email([said("One"), said("Two", minutes=1)])

    assert values["email_subject"] == "2 new messages from Ada"
    assert values["email_heading"] == "Ada sent you 2 messages"
    assert values["message2_text"] == "Two"


def test_two_from_two_people_open_messages_q14() -> None:
    """Q14's default: the subject counts them, a card each, Open Messages."""
    values = unread_email([said("One"), said("Two", sender=BEN, first="Ben", minutes=1)])

    assert values["email_subject"] == "You have 2 unread messages on EduFurther"
    assert values["email_heading"] == "You have 2 unread messages"
    assert values["button_label"] == "Open Messages"
    assert values["replies_name"] == "them"
    assert "reply_to" not in values
    assert values["message2_name"] == "Ben Obi"


def test_a_file_shows_its_name() -> None:
    values = unread_email([said(None, file_name="cv.pdf")])

    assert values["message1_text"] == ""
    assert values["message1_file_name"] == "cv.pdf"
    assert values["preview_text"] == "cv.pdf"


def test_many_counts_senders_and_never_carries_text() -> None:
    batch = [said("a"), said("b", sender=BEN, first="Ben"), said("c", sender=BEN, first="Ben")]
    values = unread_email(batch)

    assert values["unread_count"] == "3"
    assert values["sender_first_names"] == "Ben and Ada"
    assert values["sender1_count"] == "2 messages"
    assert values["sender2_count"] == "1 message"
    assert values["other_senders"] == ""
    assert not any("text" in key or "preview" in key for key in values)
    assert not any(value in {"a", "b", "c"} for value in values.values())


def test_many_rows_stop_at_the_limit_and_count_the_rest() -> None:
    batch = [said("x", sender=uuid4(), first=f"P{n}") for n in range(SENDER_ROWS + 2)]
    values = unread_email(batch)

    assert f"sender{SENDER_ROWS}_name" in values
    assert f"sender{SENDER_ROWS + 1}_name" not in values
    assert values["other_senders"] == "and 2 more"


def test_a_nameless_sender_is_someone() -> None:
    values = unread_email([said(first=None, last=None)])
    assert values["email_subject"] == "Someone sent you a message"
    assert values["message1_initials"] == ""


def test_the_instant_email_carries_the_whole_text() -> None:
    values = instant_email(said(LONG))

    assert values["message1_text"] == LONG
    assert values["sender_first_name"] == "Ada"
    assert values["reply_to"] == str(ADA)


def test_no_messages_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="needs a message"):
        unread_email([])


FEW = [
    "emailSubject",
    "emailHeading",
    "buttonLabel",
    "buttonUrl",
    "repliesName",
    "previewText",
    "message1Name",
    "message1Initials",
    "message1Time",
    "message1Text",
    "message1FileName",
    "message2Name",
    "message2Time",
    "message2Text",
    "settingsUrl",
    "messagesUrl",
    "recipientFirstName",
]
MANY = [
    "unreadCount",
    "senderFirstNames",
    *(
        f"sender{n}{part}"
        for n in range(1, SENDER_ROWS + 1)
        for part in ("Name", "Initials", "Count")
    ),
    "otherSenders",
    "messagesUrl",
    "settingsUrl",
]
INSTANT = [
    "senderFirstName",
    "previewText",
    "message1Name",
    "message1Time",
    "message1Text",
    "buttonUrl",
    "sessionName",
    "sessionDuration",
    "sessionTime",
    "sessionUrl",
]


def test_the_few_email_resolves_every_name_in_the_recipients_zone() -> None:
    values = unread_email([said("One")])
    built = build_variables(FEW, context(values))

    assert built["message1Time"] == "15:00"  # 14:00 UTC in Lagos.
    assert built["message2Name"] == ""
    assert built["message2Time"] == ""
    assert built["buttonUrl"] == f"https://app.example.test/messages?with={ADA}"
    assert built["settingsUrl"] == "https://app.example.test/messages?settings=1"
    assert built["messagesUrl"] == "https://app.example.test/messages"


def test_two_people_get_the_inbox_link() -> None:
    values = unread_email([said("One"), said("Two", sender=BEN, minutes=1)])
    assert build_variables(["buttonUrl"], context(values))["buttonUrl"] == (
        "https://app.example.test/messages"
    )


def test_the_many_email_resolves_every_name() -> None:
    values = unread_email([said("a"), said("b"), said("c", sender=BEN, first="Ben")])
    built = build_variables(MANY, context(values))
    assert built["sender1Name"] == "Ada Obi"
    assert built["sender3Name"] == ""


def test_the_instant_email_resolves_with_its_session() -> None:
    values = instant_email(said("See you soon"))
    built = build_variables(
        INSTANT,
        context(
            values,
            starts_at=NOW,
            session_id="s-1",
            session_type_name="CV review",
            duration_minutes=45,
        ),
    )
    assert built["sessionName"] == "CV review"
    assert built["sessionDuration"] == "45 min"
    assert built["sessionTime"] == "15:00"
    assert built["sessionUrl"] == "https://app.example.test/sessions/s-1"


def test_a_message_email_name_on_another_email_is_refused() -> None:
    """A template pointed at the wrong email fails by name, never blank."""
    with pytest.raises(UnresolvedVariableError, match="emailSubject"):
        build_variables(["emailSubject"], context({}))
    with pytest.raises(UnresolvedVariableError, match="sessionDuration"):
        build_variables(["sessionDuration"], context({}))
