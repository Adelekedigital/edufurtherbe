"""Links in emails and pushes say where the visit came from (decision 336)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from app.domain.messages import MessageContext, build_variables, session_page_url, tracked
from app.domain.notifications import Notification
from app.domain.push import message_push, session_push, session_push_spec

SID = UUID("00000000-0000-7000-8000-0000000000aa")
TAGS = "utm_source=email&utm_medium=notification&utm_campaign=request_accepted"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("/explore", f"/explore?{TAGS}"),
        ("/bookings?tab=pending&booking=1", f"/bookings?tab=pending&booking=1&{TAGS}"),
        ("/sessions/1#review", f"/sessions/1?{TAGS}#review"),
        ("https://app.example/sessions/1", f"https://app.example/sessions/1?{TAGS}"),
    ],
)
def test_a_link_gains_source_medium_and_campaign(link: str, expected: str) -> None:
    assert tracked(link, source="email", campaign="request_accepted") == expected


def test_a_campaign_is_encoded() -> None:
    assert tracked("/x", source="push", campaign="a b&c").endswith("utm_campaign=a+b%26c")


def context(notification: Notification | None) -> MessageContext:
    return MessageContext(
        recipient_name="Ada Mentor",
        recipient_timezone="Africa/Lagos",
        mentor_name="Ada Mentor",
        mentee_name="Bo Mentee",
        starts_at=dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.UTC),
        session_id="01a0-session",
        app_base_url="https://app.edufurther.org",
        notification=notification,
    )


def test_an_emails_links_carry_its_kind() -> None:
    built = build_variables(["sessionUrl", "exploreUrl"], context(Notification.REQUEST_ACCEPTED))

    assert built["sessionUrl"] == f"https://app.edufurther.org/sessions/01a0-session?{TAGS}"
    assert built["exploreUrl"] == f"https://app.edufurther.org/explore?{TAGS}"


def test_a_link_for_no_named_email_stays_plain() -> None:
    built = build_variables(["sessionUrl"], context(None))

    assert built["sessionUrl"] == "https://app.edufurther.org/sessions/01a0-session"


def test_a_calendar_invites_session_link_stays_plain() -> None:
    """An invite is not a notification: its link is the bare session page."""
    assert session_page_url("https://app.edufurther.org", "s1") == (
        "https://app.edufurther.org/sessions/s1"
    )


def test_a_session_push_carries_its_kind() -> None:
    spec = session_push_spec(Notification.REQUEST_ACCEPTED, recipient_role="mentee", moved=False)
    assert spec is not None
    sent = session_push(
        spec,
        notification=Notification.REQUEST_ACCEPTED,
        session_id=SID,
        other_name="Ada",
        unread=0,
        ttl=spec.ttl,
    )

    assert sent.url.endswith(
        "?utm_source=push&utm_medium=notification&utm_campaign=request_accepted"
    )


def test_a_message_push_is_the_message_campaign() -> None:
    sent = message_push(SID, "Ada", None, unread=1)

    assert sent.url == (
        f"/messages?with={SID}&utm_source=push&utm_medium=notification&utm_campaign=message"
    )
