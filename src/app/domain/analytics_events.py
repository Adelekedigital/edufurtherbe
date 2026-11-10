"""The product analytics events the server sends, and the one shape they take (PRD 30).

**Outcome events come from the server**, through the outbox, so an ad blocker
cannot drop them and each lands in the same transaction as the fact it reports.
Behaviour (pages, clicks) is the client's.

**Ids, enums, counts and buckets only.** Identity is `users.id` (ADR 0014); an
email, name, phone number or free text never leaves for an analytics vendor, so
the builder refuses a key that names one and any value that is not a plain
scalar. The vendor is named only in `infra`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID

__all__ = ["PERSONAL_KEYS", "AnalyticsEvent", "analytics_event"]


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


#: Property keys that would carry a person's identity or words. Refused by
#: name: a caller that reaches for one has the wrong value, whatever it holds.
PERSONAL_KEYS = frozenset(
    {
        "email",
        "name",
        "first_name",
        "last_name",
        "full_name",
        "phone",
        "phone_number",
        "message",
        "text",
        "body",
        "bio",
        "about_me",
        "invite_code",
        "referral_code",
    }
)


def analytics_event(
    event: AnalyticsEvent,
    *,
    user_id: UUID,
    at: dt.datetime,
    properties: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """One event as the vendor's batch takes it: `event`, `distinct_id`,
    `timestamp` (ISO 8601, UTC) and `properties`.

    A property is an id (`UUID`), an enum member, a count (`int`), a flag or a
    short code (`str`), or `None`. Anything else, or a key in `PERSONAL_KEYS`,
    is a `ValueError`: the caller is sending what must not leave.
    """
    if at.tzinfo is None:
        raise ValueError("an analytics event's time must carry its zone")
    return {
        "event": event.value,
        "distinct_id": str(user_id),
        "timestamp": at.astimezone(dt.UTC).isoformat(),
        "properties": {key: _plain(key, value) for key, value in (properties or {}).items()},
    }


def _plain(key: str, value: object) -> str | int | bool | None:
    if key.lower() in PERSONAL_KEYS:
        raise ValueError(f"analytics property {key!r} would carry personal data")
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and "@" in value:
        # No id, enum, code or bucket has one; an address does.
        raise ValueError(f"analytics property {key!r} looks like an email address")
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ValueError(f"analytics property {key!r} must be an id, enum, count or code")
