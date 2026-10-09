"""Who Daily saw in a room: its signed webhooks, and its meeting records (#382).

For an EduFurther video session, attendance comes from what the provider
observed rather than from a press of Join (owner, 2026-10-08). Two sources
carry the same fact, and both reduce to a :class:`Sighting`: the room, the
``user_id`` we minted into the person's meeting token, and when they were seen.

- **Webhooks** (``participant.joined``) arrive live, so a party is shown present
  while the call runs.
- **Meeting records** are read at settlement for any session a webhook left
  undecided. Daily stops sending webhooks after three failed deliveries, and
  settling on silence would brand everyone absent and refund the wrong people.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from app.infra.clients.scheduler import UntrustedCallbackError

__all__ = [
    "Sighting",
    "UntrustedCallbackError",
    "sighting_from",
    "sightings_from_records",
    "verify_daily_signature",
]


@dataclass(frozen=True, slots=True)
class Sighting:
    """One person seen in one room at one instant, from either source."""

    room: str
    user_id: str
    at: dt.datetime


def verify_daily_signature(*, secret: str, timestamp: str, body: bytes, signature: str) -> None:
    """Prove a webhook came from Daily, or raise.

    Daily signs ``{timestamp}.{body}`` with HMAC-SHA256 under the base64-decoded
    secret and sends the base64 digest. **The raw body is what is verified**,
    never a re-serialised parse: JSON re-encoding need not reproduce Daily's
    bytes, and a check against our own bytes would prove nothing about theirs.

    A missing signature or timestamp needs no branch of its own: either fails
    the comparison like any other wrong value. Compared in constant time.

    No freshness window is applied: Daily does not document the timestamp's
    unit, and a replayed ``participant.joined`` changes nothing, because a
    sighting only ever keeps the *earliest* time.
    """
    try:
        key = base64.b64decode(secret, validate=True)
        sent = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UntrustedCallbackError("the webhook signature is not base64") from exc
    expected = hmac.new(key, timestamp.encode() + b"." + body, hashlib.sha256).digest()
    if not hmac.compare_digest(sent, expected):
        raise UntrustedCallbackError("the webhook signature does not match its body")


def sighting_from(event: dict[str, Any]) -> Sighting | None:
    """The sighting in a ``participant.joined`` delivery, or ``None``.

    ``None`` for everything else, deliberately broad: Daily's verification
    ping (``{"test": "test"}``), other event types, and a join missing any of
    the three fields. Those are acknowledged and ignored rather than refused,
    because refusing counts as a failed delivery, and three of those switch the
    webhook off.
    """
    if event.get("type") != "participant.joined":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    room, user_id, joined_at = payload.get("room"), payload.get("user_id"), payload.get("joined_at")
    if not room or not user_id or not isinstance(joined_at, int | float):
        return None
    return Sighting(
        room=str(room), user_id=str(user_id), at=dt.datetime.fromtimestamp(joined_at, tz=dt.UTC)
    )


def sightings_from_records(room: str, records: dict[str, Any]) -> list[Sighting]:
    """Everyone Daily's meeting records saw in ``room``.

    **The shape is the one our spike observed** (`docs/daily-spike-guide.md`,
    Q3 and Q4): ``GET /meetings?room=`` returns meetings under ``data``, each
    with ``participants`` carrying the ``user_id`` we minted and a ``join_time``
    in epoch seconds. Daily's own reference is inconsistent about this endpoint,
    so anything short of a user and a time is skipped rather than guessed at. A
    room holds a meeting per rejoin, so every meeting is read.
    """
    found: list[Sighting] = []
    meetings = records.get("data")
    if not isinstance(meetings, list):
        return found
    for meeting in meetings:
        people = meeting.get("participants") if isinstance(meeting, dict) else None
        if not isinstance(people, list):
            continue
        for person in people:
            if not isinstance(person, dict):
                continue
            user_id, joined = person.get("user_id"), person.get("join_time")
            if user_id and isinstance(joined, int | float):
                found.append(
                    Sighting(room, str(user_id), dt.datetime.fromtimestamp(joined, tz=dt.UTC))
                )
    return found
