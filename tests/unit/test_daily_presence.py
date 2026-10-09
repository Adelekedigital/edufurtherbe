"""Daily's webhook signature, and what a delivery is turned into (#382).

Daily signs ``{X-Webhook-Timestamp}.{body}`` with HMAC-SHA256 under the
base64-decoded secret and sends the base64 digest in ``X-Webhook-Signature``
(docs.daily.co, *Webhooks*). The endpoint marks a party present, so an
unverified call is a forged attendance record: every refusal is pinned here.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from app.infra.clients.daily_presence import (
    Sighting,
    UnreadableRecordsError,
    UntrustedCallbackError,
    sighting_from,
    sightings_from_records,
    verify_daily_signature,
)
from app.infra.clients.meetings import TIMEOUT
from app.infra.db.session_writer.attendance import RECORDS_READ_BUDGET

SECRET = base64.b64encode(b"a-test-webhook-secret").decode()
TIMESTAMP = "1728432000000"


def signed(body: bytes, *, secret: str = SECRET, timestamp: str = TIMESTAMP) -> str:
    """What Daily would send for this body."""
    message = timestamp.encode() + b"." + body
    digest = hmac.new(base64.b64decode(secret), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


BODY = json.dumps(
    {
        "version": "1.0.0",
        "type": "participant.joined",
        "id": "ptcpt-join-1",
        "payload": {
            "room": "ef-daily-0193a5b2-0000-7000-8000-000000000001",
            "user_id": "0193a5b2-0000-7000-8000-0000000000aa",
            "joined_at": 1728432060.5,
            "session_id": "daily-participant-1",
        },
        "event_ts": 1728432060.6,
    }
).encode()


def test_a_body_daily_signed_is_accepted() -> None:
    verify_daily_signature(secret=SECRET, timestamp=TIMESTAMP, body=BODY, signature=signed(BODY))


@pytest.mark.parametrize(
    ("signature", "timestamp", "body"),
    [
        pytest.param(signed(b"something else"), TIMESTAMP, BODY, id="a forged signature"),
        pytest.param(signed(BODY), TIMESTAMP, BODY.replace(b"aa", b"bb"), id="a different body"),
        pytest.param(signed(BODY), "1728432999999", BODY, id="a moved timestamp"),
        pytest.param(
            signed(BODY, secret=base64.b64encode(b"x").decode()),
            TIMESTAMP,
            BODY,
            id="another secret",
        ),
        pytest.param("", TIMESTAMP, BODY, id="no signature"),
        pytest.param(signed(BODY), "", BODY, id="no timestamp"),
        pytest.param("%%%not-base64%%%", TIMESTAMP, BODY, id="not base64"),
    ],
)
def test_anything_else_is_refused(signature: str, timestamp: str, body: bytes) -> None:
    """The timestamp is inside the signed string, so it cannot be moved either."""
    with pytest.raises(UntrustedCallbackError):
        verify_daily_signature(secret=SECRET, timestamp=timestamp, body=body, signature=signature)


def test_a_participant_joined_is_a_sighting() -> None:
    """The room, our user id (minted into the token), and when Daily saw them."""
    sighting = sighting_from(json.loads(BODY))

    assert sighting == Sighting(
        room="ef-daily-0193a5b2-0000-7000-8000-000000000001",
        user_id="0193a5b2-0000-7000-8000-0000000000aa",
        at=dt.datetime.fromtimestamp(1728432060.5, tz=dt.UTC),
    )


@pytest.mark.parametrize(
    "event",
    [
        {"test": "test"},  # Daily's check when the webhook is created
        {"type": "participant.left", "payload": {"room": "r", "user_id": "u", "joined_at": 1}},
        {"type": "recording.started", "payload": {}},
        {"type": "participant.joined", "payload": {"room": "r", "joined_at": 1}},  # no user
        {"type": "participant.joined", "payload": {"user_id": "u", "joined_at": 1}},  # no room
        {"type": "participant.joined", "payload": {"room": "r", "user_id": "u"}},  # no time
        {"type": "participant.joined", "payload": "not an object"},
    ],
)
def test_anything_but_a_complete_join_is_no_sighting(event: dict[str, object]) -> None:
    """Acknowledged and ignored, never guessed at: a partial join records nothing."""
    assert sighting_from(event) is None


# --------------------------------------------------------------------------
# Meeting records, read at settlement when a webhook did not decide a party
# --------------------------------------------------------------------------

ROOM = "ef-daily-0193a5b2-0000-7000-8000-000000000001"


def test_every_participant_in_every_meeting_of_the_room_is_a_sighting() -> None:
    """The shape the Daily spike recorded from `GET /meetings?room=`: meetings
    under `data`, each with `participants` carrying our `user_id` and a
    `join_time` in epoch seconds. A room can hold more than one meeting when
    somebody leaves and rejoins, so all are read."""
    records = {
        "data": [
            {"participants": [{"user_id": "u-mentee", "join_time": 1728432060}]},
            {"participants": [{"user_id": "u-mentor", "join_time": 1728432120}]},
        ]
    }

    assert sightings_from_records(ROOM, records) == [
        Sighting(ROOM, "u-mentee", dt.datetime.fromtimestamp(1728432060, tz=dt.UTC)),
        Sighting(ROOM, "u-mentor", dt.datetime.fromtimestamp(1728432120, tz=dt.UTC)),
    ]


def test_no_meetings_is_an_empty_room() -> None:
    """A well-formed answer with no meetings is genuinely nobody: settle on it."""
    assert sightings_from_records(ROOM, {"data": []}) == []


@pytest.mark.parametrize(
    "records",
    [
        pytest.param({}, id="no data"),
        pytest.param({"data": "not a list"}, id="data not a list"),
        pytest.param({"error": "invalid-request"}, id="an error body"),
        pytest.param({"data": ["not an object"]}, id="a meeting not an object"),
        pytest.param({"data": [{"participants": "not a list"}]}, id="participants not a list"),
        pytest.param({"data": [{"participants": [{"join_time": 1}]}]}, id="no user"),
        pytest.param({"data": [{"participants": [{"user_id": "u"}]}]}, id="no time"),
        pytest.param(
            {"data": [{"participants": [{"user_id": "u", "join_time": "soon"}]}]}, id="bad time"
        ),
    ],
)
def test_a_record_that_cannot_be_read_is_unreadable_not_empty(records: dict[str, object]) -> None:
    """**Unreadable is not empty** (Codex on #393). A changed or error body read
    as an empty room would settle every party absent and move a refund; raised,
    the session waits its day like any other unreadable read. Skipping one
    incomplete participant is the same mistake for one person."""
    with pytest.raises(UnreadableRecordsError):
        sightings_from_records(ROOM, records)


def test_the_records_budget_leaves_the_settlement_job_room_to_finish() -> None:
    """**Pinned to the schedule's own limit** (Codex on #393). The budget is
    spent before the last read starts, so the worst case is the budget plus one
    full client timeout, and that must stay well inside the job's timeout or the
    settlement never commits."""
    manifest = json.loads(Path("config/runtime-schedules.json").read_text(encoding="utf-8"))
    job = next(j for j in manifest["jobs"] if j["name"] == "settle-sessions")
    limit = dt.timedelta(seconds=int(str(job["timeout"]).rstrip("s")))
    worst = RECORDS_READ_BUDGET + dt.timedelta(seconds=TIMEOUT.read or 0)

    assert worst <= limit / 2


def test_a_partial_page_of_meetings_is_unreadable_not_complete() -> None:
    """**A page is not the room** (Codex on #393). `/meetings` is paginated and
    reports `total_count`; a join on a page we did not read would settle a party
    absent. Fewer meetings than the total is unreadable, so the session waits."""
    page = {"total_count": 3, "data": [{"participants": [{"user_id": "u", "join_time": 1}]}]}

    with pytest.raises(UnreadableRecordsError):
        sightings_from_records(ROOM, page)


def test_a_complete_page_is_read() -> None:
    page = {"total_count": 1, "data": [{"participants": [{"user_id": "u", "join_time": 1}]}]}

    assert [s.user_id for s in sightings_from_records(ROOM, page)] == ["u"]


def test_a_join_at_an_impossible_time_is_no_sighting() -> None:
    """A number no clock can hold (`1e300`) passes the type check and then
    overflows; it is ignored, not a crash of the webhook (Codex on #393)."""
    event = {
        "type": "participant.joined",
        "payload": {"room": "r", "user_id": "u", "joined_at": 1e300},
    }

    assert sighting_from(event) is None


def test_records_at_an_impossible_time_are_unreadable() -> None:
    """The same in the records: unreadable, so the session waits, rather than an
    overflow aborting the whole settlement (Codex on #393)."""
    with pytest.raises(UnreadableRecordsError):
        sightings_from_records(
            ROOM, {"data": [{"participants": [{"user_id": "u", "join_time": 1e300}]}]}
        )
