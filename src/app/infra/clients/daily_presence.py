"""Who Daily saw in a room: its signed webhooks, and its meeting records (#382).

For an EduFurther video session, attendance comes from what the provider
observed rather than from a press of Join (owner, 2026-10-08). Two sources
carry the same fact, and both reduce to a :class:`Sighting`: the room, the
``user_id`` we minted into the person's meeting token, and when they were seen.

- **Webhooks** (``participant.joined``, ``participant.left``) arrive live, so a
  party is shown present while the call runs, and in the room *now* until they
  leave (#394). A leave is a :class:`Departure`: the sighting of its join, and
  when that join ended.
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
import math
from dataclasses import dataclass
from typing import Any

from app.domain.session_minutes import LONGEST_STAY
from app.infra.clients.scheduler import UntrustedCallbackError

__all__ = [
    "STAY_ID_MAX_LENGTH",
    "Departure",
    "Sighting",
    "Stay",
    "UnreadableRecordsError",
    "UntrustedCallbackError",
    "departure_from",
    "sighting_from",
    "sightings_from_records",
    "stays_from_records",
    "verify_daily_signature",
]


class UnreadableRecordsError(ValueError):
    """Daily's meeting records came back in a shape we cannot read."""


@dataclass(frozen=True, slots=True)
class Sighting:
    """One person seen in one room at one instant, from either source."""

    room: str
    user_id: str
    at: dt.datetime
    #: Daily's id for this one join (its ``session_id``), on a webhook only:
    #: what opens and closes a stay in the room (#394). ``None`` from the
    #: records, and from a webhook whose id we could not store.
    stay: str | None = None


@dataclass(frozen=True, slots=True)
class Departure:
    """A ``participant.left``: the join it ends, seen at its own join time, and
    when it ended (``joined_at + duration``)."""

    sighting: Sighting
    left: dt.datetime


#: The longest Daily join id stored, matching the column's ``CHECK``. A longer
#: id is kept as a sighting with no stay, never refused: refusing counts as a
#: failed delivery.
STAY_ID_MAX_LENGTH = 128


@dataclass(frozen=True, slots=True)
class Stay:
    """One person in the room from one instant to another: a participant record
    of a finished meeting, for measured minutes (Integrations PRD, 2026-10-21)."""

    user_id: str
    joined: dt.datetime
    left: dt.datetime


def _moment(value: object) -> dt.datetime | None:
    """Daily's epoch seconds as a UTC instant, or ``None`` when no clock can hold
    it (Codex on #393): `1e300` is a valid JSON number that overflows."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    except OverflowError, OSError, ValueError:
        return None


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


def _stay_id(value: object) -> str | None:
    """Daily's join id, when it is one we can store."""
    if not isinstance(value, str) or not 1 <= len(value) <= STAY_ID_MAX_LENGTH:
        return None
    return value


def _seconds_or_problem(seconds: object) -> float | str:
    """``seconds`` as a length of stay in the room, or why it is not one (a
    ``str``). One rule for the records and for a leave, so a duration settlement
    refuses is one the webhook refuses too.

    Finite too: JSON may carry NaN, which compares false to everything and
    would reach `timedelta` as a crash. And bounded, as a bound on one record
    rather than a cap on minutes (decision 281): no real stay is longer, and a
    finite `2e11` would overflow the stored minutes on every run.
    """
    if (
        not isinstance(seconds, int | float)
        or isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or seconds < 0
    ):
        return "a participant has no usable duration"
    if seconds > LONGEST_STAY.total_seconds():
        return "a participant's duration is longer than any real stay"
    return float(seconds)


def _event_sighting(event: dict[str, Any], kind: str) -> tuple[Sighting, dict[str, Any]] | None:
    """The sighting in a delivery of ``kind``, with its payload, or ``None``."""
    if event.get("type") != kind:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    room, user_id = payload.get("room"), payload.get("user_id")
    at = _moment(payload.get("joined_at"))
    if not room or not user_id or at is None:
        return None
    sighting = Sighting(
        room=str(room), user_id=str(user_id), at=at, stay=_stay_id(payload.get("session_id"))
    )
    return sighting, payload


def sighting_from(event: dict[str, Any]) -> Sighting | None:
    """The sighting in a ``participant.joined`` delivery, or ``None``.

    ``None`` for everything else, deliberately broad: Daily's verification
    ping (``{"test": "test"}``), other event types, and a join missing any of
    the three fields. Those are acknowledged and ignored rather than refused,
    because refusing counts as a failed delivery, and three of those switch the
    webhook off.
    """
    found = _event_sighting(event, "participant.joined")
    return None if found is None else found[0]


def departure_from(event: dict[str, Any]) -> Departure | None:
    """The departure in a ``participant.left`` delivery, or ``None``.

    ``None`` for anything else, and for a leave missing the room, the user, the
    join time, or a usable ``duration``: acknowledged and ignored, as for a
    join. A leave with no storable join id is still its join's sighting, so a
    join whose own delivery was lost still counts.
    """
    found = _event_sighting(event, "participant.left")
    if found is None:
        return None
    sighting, payload = found
    seconds = _seconds_or_problem(payload.get("duration"))
    if isinstance(seconds, str):
        return None
    try:
        left = sighting.at + dt.timedelta(seconds=seconds)
    except OverflowError:
        return None
    return Departure(sighting=sighting, left=left)


def _participants(
    records: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any], str, dt.datetime]]:
    """Every participant record in every meeting, with its meeting, user and join.

    **The one reader of ``GET /meetings?room=``**, which attendance and measured
    minutes both go through, so neither can accept a body the other refuses.
    The shape is the one our spike observed (`docs/daily-spike-guide.md`, Q3
    and Q4): meetings under ``data``, each with ``participants`` carrying the
    ``user_id`` we minted and a ``join_time`` in epoch seconds. A room holds a
    meeting per rejoin, so every meeting is read.

    **Anything else raises** :class:`UnreadableRecordsError` (Codex on #393).
    Daily's reference is inconsistent about this endpoint, and a changed or
    error body read as an empty room would settle every party absent and move a
    refund. Only a well-formed ``data`` with nobody in it is an empty room.
    """
    meetings = records.get("data")
    if not isinstance(meetings, list):
        raise UnreadableRecordsError("the records carry no list of meetings")
    # **A page is not the room** (Codex on #393): `/meetings` is paginated, and
    # a join on a page we did not read would settle a party absent.
    total = records.get("total_count")
    if isinstance(total, int) and total > len(meetings):
        raise UnreadableRecordsError(f"one page of {total} meetings is not the room")
    found: list[tuple[dict[str, Any], dict[str, Any], str, dt.datetime]] = []
    for meeting in meetings:
        people = meeting.get("participants") if isinstance(meeting, dict) else None
        if not isinstance(people, list):
            raise UnreadableRecordsError("a meeting carries no list of participants")
        for person in people:
            user_id = person.get("user_id") if isinstance(person, dict) else None
            joined = _moment(person.get("join_time")) if isinstance(person, dict) else None
            if not user_id or joined is None:
                raise UnreadableRecordsError("a participant has no user or readable join time")
            found.append((meeting, person, str(user_id), joined))
    return found


def sightings_from_records(room: str, records: dict[str, Any]) -> list[Sighting]:
    """Everyone Daily's meeting records saw in ``room``, when they joined.

    Read at settlement for any party a webhook left undecided; a meeting still
    going is fine here, since its joins are already written.
    """
    return [Sighting(room, user_id, joined) for _, _, user_id, joined in _participants(records)]


def stays_from_records(room: str, records: dict[str, Any]) -> list[Stay]:
    """How long each person was in ``room``: ``join_time`` plus ``duration``
    (seconds, Daily's reference and the spike's Q3), per participant record.

    **Only from finished meetings.** A record read mid-call carries a partial
    duration (the spike's ``ongoing`` trap), and a measured figure is stored
    once, so a meeting not explicitly ``ongoing: false``, or a record without a
    usable length, is unreadable rather than short. So is one stay longer than
    ``LONGEST_STAY``, refused whole and never clipped. ``room`` names the room
    in the error only; the records were fetched for it.
    """
    stays: list[Stay] = []
    for meeting, person, user_id, joined in _participants(records):
        if meeting.get("ongoing") is not False:
            raise UnreadableRecordsError(f"a meeting in {room} is still going or unmarked")
        seconds = _seconds_or_problem(person.get("duration"))
        if isinstance(seconds, str):
            raise UnreadableRecordsError(seconds)
        try:
            left = joined + dt.timedelta(seconds=seconds)
        except OverflowError as exc:
            raise UnreadableRecordsError("a participant's duration overflows") from exc
        stays.append(Stay(user_id, joined, left))
    return stays
