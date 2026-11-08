"""What each session event pushes, and to whom (decision 312)."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.domain.notifications import AUDIENCE, Notification
from app.domain.push import (
    HIGH,
    MOVED_PUSHES,
    NORMAL,
    PUSH_TTL,
    SESSION_PUSHES,
    SESSION_SOON_TTL,
    PushType,
    message_push,
    session_push,
)

SID = UUID("00000000-0000-7000-8000-0000000000aa")


def push(notification: Notification, role: str = "mentor", *, moved: bool = False):  # type: ignore[no-untyped-def]
    return session_push(
        notification, session_id=SID, recipient_role=role, other_name="Ada", moved=moved, unread=3
    )


@pytest.mark.parametrize(
    ("notification", "role", "kind", "body", "url"),
    [
        (
            Notification.SESSION_REQUESTED,
            "mentor",
            PushType.BOOKING_REQUEST,
            "Ada wants to book a session with you",
            f"/bookings?tab=pending&booking={SID}",
        ),
        (
            Notification.SESSION_BOOKED,
            "mentor",
            PushType.BOOKING_REQUEST,
            "Ada booked a session with you",
            f"/sessions/{SID}",
        ),
        (
            Notification.REQUEST_ACCEPTED,
            "mentee",
            PushType.BOOKING_RESPONSE,
            "Ada accepted your booking",
            f"/sessions/{SID}",
        ),
        (
            Notification.REQUEST_DECLINED,
            "mentee",
            PushType.BOOKING_RESPONSE,
            "Ada declined your booking",
            f"/sessions/{SID}",
        ),
        (
            Notification.SESSION_TIME_SUGGESTED,
            "mentee",
            PushType.BOOKING_RESPONSE,
            "Ada suggested a new time",
            f"/sessions/{SID}",
        ),
        (
            Notification.SESSION_CANCELLED,
            "mentee",
            PushType.SESSION_CHANGED,
            "Ada cancelled your session",
            f"/sessions/{SID}",
        ),
        (
            Notification.REQUEST_WITHDRAWN,
            "mentor",
            PushType.SESSION_CHANGED,
            "Ada withdrew their booking request",
            f"/sessions/{SID}",
        ),
    ],
)
def test_each_event_pushes_its_type_words_and_link(
    notification: Notification, role: str, kind: PushType, body: str, url: str
) -> None:
    sent = push(notification, role)

    assert sent is not None
    assert (sent.type, sent.body, sent.url, sent.tag, sent.badge) == (
        kind,
        body,
        url,
        f"session:{SID}",
        3,
    )
    assert (sent.ttl, sent.urgency) == (PUSH_TTL, NORMAL)


def test_starting_soon_is_urgent_and_short_lived() -> None:
    sent = push(Notification.SESSION_LAST_REMINDER, "mentee")

    assert sent is not None
    assert (sent.type, sent.ttl, sent.urgency) == (PushType.SESSION_SOON, SESSION_SOON_TTL, HIGH)
    assert sent.body == "Your session with Ada starts in 5 minutes"


def test_the_mentee_who_booked_is_not_pushed_their_own_booking() -> None:
    assert push(Notification.SESSION_BOOKED, "mentee") is None
    assert push(Notification.SESSION_BOOKED, "mentee", moved=True) is None


@pytest.mark.parametrize(
    ("notification", "body", "url"),
    [
        (
            Notification.SESSION_REQUESTED,
            "Ada asked to move your session",
            f"/bookings?tab=pending&booking={SID}",
        ),
        (Notification.SESSION_BOOKED, "Ada moved your session", f"/sessions/{SID}"),
    ],
)
def test_a_moved_booking_pushes_as_a_change(
    notification: Notification, body: str, url: str
) -> None:
    sent = push(notification, moved=True)

    assert sent is not None
    assert (sent.type, sent.body, sent.url) == (PushType.SESSION_CHANGED, body, url)


def test_moved_only_changes_the_bookings() -> None:
    assert push(Notification.SESSION_CANCELLED, "mentee", moved=True) == push(
        Notification.SESSION_CANCELLED, "mentee"
    )


def test_a_party_who_left_reads_with_a_capital() -> None:
    sent = session_push(
        Notification.SESSION_CANCELLED,
        session_id=SID,
        recipient_role="mentee",
        other_name="your mentor",
        moved=False,
        unread=0,
    )

    assert sent is not None and sent.body == "Your mentor cancelled your session"


def test_an_event_without_a_push_pushes_nothing() -> None:
    assert push(Notification.REQUEST_EXPIRED) is None
    assert push(Notification.SESSION_REMINDER) is None


def test_every_push_rides_an_email_to_a_session_party() -> None:
    """A push is queued beside its email (`outbox.enqueue`), so each event must
    have one, addressed to a session's parties."""
    assert set(SESSION_PUSHES) <= set(AUDIENCE)
    assert set(MOVED_PUSHES) <= set(SESSION_PUSHES)


def test_ttl_and_urgency_stay_out_of_the_payload() -> None:
    sent = push(Notification.SESSION_LAST_REMINDER)

    assert sent is not None
    assert b"ttl" not in sent.payload() and b"urgency" not in sent.payload()


def test_a_message_push_opens_the_chat_the_emails_link() -> None:
    sent = message_push(SID, "Ada", None, unread=1)

    assert sent.url == f"/messages?with={SID}"
