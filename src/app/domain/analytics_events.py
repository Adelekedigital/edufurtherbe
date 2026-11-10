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
from uuid import UUID

__all__ = ["PERSONAL_KEYS", "PERSONAL_WORDS", "AnalyticsEvent", "analytics_event"]


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


#: A key containing one of these, in any case, would carry a person's identity
#: or words (`mentor_name`, `contact_email`): refused whatever it holds.
PERSONAL_WORDS = ("name", "email", "phone", "address", "bio", "text", "message")

#: Keys refused by exact name that no word above catches.
PERSONAL_KEYS = frozenset({"body", "about_me", "invite_code", "referral_code"})

#: Every string value: an id, an enum value or a code. Spaces, `@` and
#: anything longer are what a name, an address or free text would need.
PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

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
        "properties": {
            **{key: _plain(key, value) for key, value in (properties or {}).items()},
            GEOIP_DISABLE: True,
        },
    }


def _plain(key: str, value: object) -> str | int | bool | None:
    lowered = key.lower()
    if lowered in PERSONAL_KEYS or any(word in lowered for word in PERSONAL_WORDS):
        raise ValueError(f"analytics property {key!r} would carry personal data")
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and not PLAIN_VALUE.fullmatch(value):
        raise ValueError(f"analytics property {key!r} must be an id, enum value or code")
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ValueError(f"analytics property {key!r} must be an id, enum, count or code")
