"""What each session event pushes, and to whom (decision 312)."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.domain.notifications import AUDIENCE, SESSION_REMINDERS, Notification
from app.domain.push import (
    HIGH,
    MOVED_PUSHES,
    NORMAL,
    PUSH_TTL,
    SESSION_PUSHES,
    SESSION_SOON_TTL,
    PushMessage,
    PushType,
    message_push,
    session_push,
    session_push_spec,
)

SID = UUID("00000000-0000-7000-8000-0000000000aa")


def push(
    notification: Notification, role: str = "mentor", *, moved: bool = False
) -> PushMessage | None:
    spec = session_push_spec(notification, recipient_role=role, moved=moved)
    if spec is None:
        return None
    return session_push(spec, session_id=SID, other_name="Ada", unread=3, ttl=spec.ttl)


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
    spec = session_push_spec(Notification.SESSION_CANCELLED, recipient_role="mentee", moved=False)
    assert spec is not None

    sent = session_push(spec, session_id=SID, other_name="your mentor", unread=0, ttl=spec.ttl)

    assert sent.body == "Your mentor cancelled your session"


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


def test_starting_soon_lives_no_longer_than_its_lead_before_the_start() -> None:
    """Queued five minutes before the start, so past five minutes it would
    announce a session already under way."""
    lead = min(reminder.before for reminder in SESSION_REMINDERS)

    assert lead == SESSION_SOON_TTL


def test_a_moved_booking_skips_the_same_party_as_the_booking() -> None:
    """Who is pushed is read at queue time from the unmoved spec's audience."""
    for notification, moved in MOVED_PUSHES.items():
        assert moved.only == SESSION_PUSHES[notification].only
