"""The product analytics events the server sends, and the one shape they take (PRD 30).

**Outcome events come from the server**, through the outbox, so an ad blocker
cannot drop them and each lands in the same transaction as the fact it reports.
Behaviour (pages, clicks) is the client's.

**Ids, enums, counts and buckets only.** Identity is `users.id` (ADR 0014); an
email, name, phone number or free text never leaves for an analytics vendor, so
the builder refuses a key that names one, and any string that is not an id,
enum value or code. The vendor is named only in `infra`, save the batch shape
and its GeoIP opt-out.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID, uuid5

from app.domain.enums import ApprovalStatus, ListingStatus, SessionStatus

__all__ = [
    "HISTORY_NAMESPACE",
    "MENTOR_COUNTERPARTS",
    "PERSONAL_KEYS",
    "PERSONAL_WORDS",
    "SETTLED_EVENTS",
    "AnalyticsEvent",
    "analytics_event",
    "cancelled_properties",
    "first_confirmation",
    "granted_properties",
    "historical_event",
    "history_event_id",
    "mentor_status",
    "person_properties",
    "person_update",
    "requested_properties",
    "review_properties",
    "session_event_names",
    "session_properties",
    "spent_properties",
    "with_person",
]


class AnalyticsEvent(StrEnum):
    """Every server event, named `object_verb_past`. The client sends its own."""

    BOOKING_REQUESTED = "booking_requested"
    SESSION_CONFIRMED = "session_confirmed"
    SESSION_COMPLETED = "session_completed"
    SESSION_CANCELLED = "session_cancelled"
    SESSION_NO_SHOW = "session_no_show"
    CREDIT_SPENT = "credit_spent"
    CREDIT_GRANTED = "credit_granted"
    MENTOR_LISTED = "mentor_listed"
    REVIEW_SUBMITTED = "review_submitted"
    REFERRAL_UNLOCKED = "referral_unlocked"
    BOOKING_RECEIVED = "booking_received"
    SESSION_DELIVERED = "session_delivered"


#: The mentor's side of a mentee's event (decision 337): queued with it, about
#: the session's mentor, with the same properties. Every writer, live and the
#: backfill, reads this one mapping.
MENTOR_COUNTERPARTS = {
    AnalyticsEvent.BOOKING_REQUESTED: AnalyticsEvent.BOOKING_RECEIVED,
    AnalyticsEvent.SESSION_COMPLETED: AnalyticsEvent.SESSION_DELIVERED,
}


#: A key containing one of these, in any case, would carry a person's identity
#: or words (`mentor_name`, `contact_email`): refused whatever it holds.
PERSONAL_WORDS = ("name", "email", "phone", "address", "bio", "text", "message")

#: Keys refused by exact name that no word above catches.
PERSONAL_KEYS = frozenset({"body", "about_me", "invite_code", "referral_code"})

#: Every string value: an id, an enum value or a code. Spaces, `@` and
#: anything longer are what a name, an address or free text would need.
PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

#: The only keys that hold a mapping: PostHog's person updates (decision
#: 340), whose every key and value is guarded like the event's own.
PERSON_UPDATES = ("$set", "$set_once")

#: PostHog would place a server-sent event at the server's address: "To skip
#: it for a single event, set the `$geoip_disable` property to `true` on that
#: event" (https://posthog.com/docs/privacy/data-collection, "Location data
#: when IPs are discarded").
GEOIP_DISABLE = "$geoip_disable"


def analytics_event(
    event: AnalyticsEvent,
    *,
    user_id: UUID,
    at: dt.datetime,
    properties: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """One event as the vendor's batch takes it: `event`, `distinct_id`,
    `timestamp` (ISO 8601, UTC) and `properties`, which always skip GeoIP.

    A property is an id (`UUID`), an enum member, a count (`int`), a flag or a
    short code (`str` matching `PLAIN_VALUE`), or `None`. Anything else, or a
    personal key, is a `ValueError`: the caller is sending what must not leave.
    """
    if at.tzinfo is None:
        raise ValueError("an analytics event's time must carry its zone")
    return {
        "event": event.value,
        "distinct_id": str(user_id),
        "timestamp": at.astimezone(dt.UTC).isoformat(),
        "properties": {**_guarded(properties or {}), GEOIP_DISABLE: True},
    }


def _guarded(properties: Mapping[str, object], *, top: bool = True) -> dict[str, Any]:
    """Every key and value through `_plain`: the one personal-data guard.
    Only the event's own ``top`` level may hold a `PERSON_UPDATES` mapping."""
    return {key: _plain(key, value, top=top) for key, value in properties.items()}


def _plain(key: str, value: object, *, top: bool = True) -> Any:
    lowered = key.lower()
    if lowered in PERSONAL_KEYS or any(word in lowered for word in PERSONAL_WORDS):
        raise ValueError(f"analytics property {key!r} would carry personal data")
    if isinstance(value, Mapping) and top and key in PERSON_UPDATES:
        return _guarded(value, top=False)
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and not PLAIN_VALUE.fullmatch(value):
        raise ValueError(f"analytics property {key!r} must be an id, enum value or code")
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ValueError(f"analytics property {key!r} must be an id, enum, count or code")


# --------------------------------------------------------------------------
# Who an event is about: the PostHog person's properties (decision 340)
# --------------------------------------------------------------------------


def mentor_status(approval: ApprovalStatus | str, listing: ListingStatus | str) -> str:
    """One mentor status: the listing (`listed`, `unlisted`) once approved,
    else the approval (`pending`, `declined`)."""
    approval = ApprovalStatus(approval)
    return ListingStatus(listing).value if approval == ApprovalStatus.APPROVED else approval.value


def person_properties(
    *,
    user_id: UUID,
    role: object,
    is_migrated: bool,
    signup_source: object,
    signup_channel: object,
    created_at: dt.datetime,
    approval_status: ApprovalStatus | str | None,
    listing_status: ListingStatus | str | None,
) -> dict[str, Any]:
    """What a person page shows, merged into each of their events: current
    state under `$set`, the creation month (UTC, ``YYYY-MM``) under
    `$set_once` (https://posthog.com/docs/product-analytics/person-properties).
    The names are the frontend's on identify. ``mentor_status`` only for a
    mentor, one with a live `mentor_profiles` row (``approval_status`` set)."""
    if created_at.tzinfo is None:
        raise ValueError("an account's creation time must carry its zone")
    current: dict[str, object] = {
        "user_id": user_id,
        "role": role,
        "is_migrated": is_migrated,
        "signup_source": signup_source,
        "signup_channel": signup_channel,
    }
    if approval_status is not None and listing_status is not None:
        current["mentor_status"] = mentor_status(approval_status, listing_status)
    month = created_at.astimezone(dt.UTC).strftime("%Y-%m")
    return _guarded({"$set": current, "$set_once": {"account_created_month": month}})


def with_person(event: Mapping[str, Any], person: Mapping[str, object]) -> dict[str, Any]:
    """``event`` with ``person`` (`person_properties`) beside its own
    properties, through the guard again: nothing reaches PostHog unguarded."""
    return {**event, "properties": {**event["properties"], **_guarded(person)}}


def person_update(user_id: UUID, person: Mapping[str, object]) -> dict[str, Any]:
    """PostHog's own way to update a person with no product event: a `$set`
    event carrying ``person`` (https://posthog.com/docs/product-analytics/person-properties,
    "How to set person properties"). Safe to repeat."""
    return {
        "event": "$set",
        "distinct_id": str(user_id),
        "properties": {**_guarded(person), GEOIP_DISABLE: True},
    }


# --------------------------------------------------------------------------
# What each event carries: one shape, for the live writers and the backfill
# --------------------------------------------------------------------------


def _entered(properties: dict[str, object], entry_point: object) -> dict[str, object]:
    """``properties`` with the session's `entry_point` (PRD 30) when it has one:
    an older session or a client that sent none carries no key at all."""
    return properties if entry_point is None else {**properties, "entry_point": entry_point}


def requested_properties(
    session_id: UUID, session_type_id: UUID | None, entry_point: object
) -> dict[str, object]:
    """`booking_requested` and its mentor's `booking_received`."""
    return _entered({"session_id": session_id, "session_type_id": session_type_id}, entry_point)


def session_properties(session_id: UUID, entry_point: object) -> dict[str, object]:
    """`session_confirmed`, `session_completed` (and its mentor's
    `session_delivered`) and `session_no_show`."""
    return _entered({"session_id": session_id}, entry_point)


def cancelled_properties(
    session_id: UUID, by: str | None, reason_code: object, entry_point: object
) -> dict[str, object]:
    """`session_cancelled`: who cancelled (`session_stats.canceller`) and why."""
    return _entered({"session_id": session_id, "by": by, "reason_code": reason_code}, entry_point)


def spent_properties(amount: int, session_id: UUID | None) -> dict[str, object]:
    """`credit_spent`."""
    return {"amount": amount, "session_id": session_id}


def granted_properties(amount: int, source: object) -> dict[str, object]:
    """`credit_granted`: the lot's `CreditSource`."""
    return {"amount": amount, "source": source}


def review_properties(rating: int | None) -> dict[str, object]:
    """`review_submitted`: the review's value (`review_stats.review_value`), 1-5."""
    return {"rating": rating}


#: The event each settled outcome reports.
SETTLED_EVENTS = {
    SessionStatus.COMPLETED: AnalyticsEvent.SESSION_COMPLETED,
    SessionStatus.NO_SHOW: AnalyticsEvent.SESSION_NO_SHOW,
}


def first_confirmation(moved_from: SessionStatus | None) -> bool:
    """Whether a session booked confirmed is confirmed for the first time.

    ``moved_from`` is the status the session a move ended had, ``None`` for a
    new booking. A new booking is; a move only when what it ended was still
    waiting on the mentor: a confirmed session moved stays confirmed (decision 300).
    """
    return moved_from is None or moved_from == SessionStatus.PENDING_MENTOR_APPROVAL


def session_event_names(
    from_status: SessionStatus | None,
    to_status: SessionStatus,
    *,
    moved_from: SessionStatus | None,
    moved_on: bool,
) -> list[AnalyticsEvent]:
    """The events one `session_events` row reports, as the live writers queue them.

    A creation event (``from_status`` null) is a request unless it starts a
    move (``moved_from`` set), and a confirmation when `first_confirmation`
    says so. An acceptance is a confirmation; a cancellation is one unless it
    ended a session that was moved on (``moved_on``); a settled outcome is
    `SETTLED_EVENTS`'. Anything else (declined, withdrawn, expired) is none.
    """
    if from_status is None:
        names = [] if moved_from is not None else [AnalyticsEvent.BOOKING_REQUESTED]
        if to_status == SessionStatus.CONFIRMED and first_confirmation(moved_from):
            names.append(AnalyticsEvent.SESSION_CONFIRMED)
        return names
    if to_status == SessionStatus.CONFIRMED:
        return [AnalyticsEvent.SESSION_CONFIRMED]
    if to_status == SessionStatus.CANCELLED and not moved_on:
        return [AnalyticsEvent.SESSION_CANCELLED]
    settled = SETTLED_EVENTS.get(to_status)
    return [settled] if settled is not None else []


# --------------------------------------------------------------------------
# History (the one-off backfill, PRD 30 PR 7)
# --------------------------------------------------------------------------

#: Fixed for ever: every backfilled event's `uuid` is derived under it, so a
#: second run gives each fact the uuid the first did. **Never change it.**
HISTORY_NAMESPACE = UUID("401bd1b7-b938-49ad-ae51-48cb1b06581f")


def history_event_id(event: AnalyticsEvent, fact_id: UUID) -> UUID:
    """The same id for the same event about the same stored fact, on every run.

    PostHog treats events sharing `uuid`, `event`, `timestamp` and
    `distinct_id` as duplicates (https://posthog.com/docs/data/events, "Event
    deduplication"), and all four are fixed by the fact, so a re-run or an
    overlapping run is dropped rather than counted twice.
    """
    return uuid5(HISTORY_NAMESPACE, f"{event.value}:{fact_id}")


def historical_event(
    event: AnalyticsEvent,
    *,
    fact_id: UUID,
    user_id: UUID,
    at: dt.datetime,
    properties: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """`analytics_event`, guard and all, with its `history_event_id`."""
    built = analytics_event(event, user_id=user_id, at=at, properties=properties)
    built["uuid"] = str(history_event_id(event, fact_id))
    return built
