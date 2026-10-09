"""Attendance from presence for EduFurther video sessions (#382).

Owner, 2026-10-08: for a Daily session a party attended if Daily saw them in
the room before arrivals stopped, not if they pressed Join. The press still
sets `joined_at`, which the frontend uses to decide who may re-enter.
Meet and custom venues report no presence, so the press still decides there.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_attendance import settle
from tests.integration.test_api_meeting_provisioning import (
    a_mentor_on,
    started,
)
from tests.integration.test_api_session_door import attendance_of, status_of

from app.infra.clients.daily_presence import Sighting
from app.infra.clients.meetings import VenueUnavailableError
from app.infra.db.session_writer import confirm_presence, observe_presence, settle_attendance

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def join_url(session: dict[str, Any]) -> str:
    return f"/api/v1/sessions/{session['id']}/join"


async def party(engine: AsyncEngine, session_id: str, role: str) -> dict[str, Any]:
    return next(r for r in await attendance_of(engine, session_id) if r["role"] == role)


async def room_and_users(engine: AsyncEngine, session_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT external_room_id AS room, mentor_id, mentee_id, starts_at "
                        "FROM sessions WHERE id = :i"
                    ),
                    {"i": session_id},
                )
            )
            .mappings()
            .one()
        )


async def seen(engine: AsyncEngine, sighting: Sighting) -> bool:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        changed = await observe_presence(session, sighting)
        await session.commit()
    return changed


async def in_room_at(engine: AsyncEngine, session_id: str, role: str) -> dt.datetime | None:
    async with engine.connect() as conn:
        value: dt.datetime | None = (
            await conn.execute(
                text(
                    "SELECT in_room_at FROM session_participants "
                    "WHERE session_id = :i AND role = :r"
                ),
                {"i": session_id, "r": role},
            )
        ).scalar_one()
    return value


async def evidence_of(engine: AsyncEngine, session_id: str) -> str | None:
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text(
                    "SELECT metadata->>'evidence' FROM session_events "
                    "WHERE session_id = :i AND actor_type = 'system' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"i": session_id},
            )
        ).scalar_one_or_none()
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# The press, per venue
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("door")
async def test_pressing_join_on_a_daily_session_records_the_press_not_attendance(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The press is the way in, not the evidence.** `joined_at` is set, so the
    party may re-enter, and attendance waits for Daily to see them."""
    setup = await a_mentor_on(db_engine, "pres-press-daily", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)

    joined = await api_client.post(join_url(session), headers=setup["mentee_headers"])

    assert joined.status_code == 200, joined.text
    mentee = await party(db_engine, session["id"], "mentee")
    assert mentee["joined_at"] is not None
    assert mentee["attendance_status"] == "pending"


async def test_pressing_join_on_a_meet_session_still_marks_attendance(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Meet reports no presence, so the press still decides (unchanged)."""
    setup = await a_mentor_on(db_engine, "pres-press-meet", "google_meet")
    session = await started(db_engine, api_client, setup, minutes_ago=1)

    joined = await api_client.post(join_url(session), headers=setup["mentee_headers"])

    assert joined.status_code == 200, joined.text
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "attended"


# --------------------------------------------------------------------------
# A sighting
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("door")
async def test_a_sighting_inside_the_window_marks_the_party_present(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup = await a_mentor_on(db_engine, "pres-seen", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=2)
    venue = await room_and_users(db_engine, session["id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)

    changed = await seen(db_engine, Sighting(venue["room"], str(venue["mentee_id"]), at))

    assert changed is True
    mentee = await party(db_engine, session["id"], "mentee")
    assert mentee["attendance_status"] == "attended"
    assert await in_room_at(db_engine, session["id"], "mentee") == at
    assert (await party(db_engine, session["id"], "mentor"))["attendance_status"] == "pending"


@pytest.mark.usefixtures("door")
async def test_the_earliest_sighting_is_kept_whatever_order_they_arrive_in(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Daily delivers roughly in order and may repeat. A later or repeated
    sighting never moves `in_room_at`; an earlier one does."""
    setup = await a_mentor_on(db_engine, "pres-order", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=5)
    venue = await room_and_users(db_engine, session["id"])
    mentee = str(venue["mentee_id"])
    first, second = (venue["starts_at"] + dt.timedelta(minutes=m) for m in (1, 3))

    await seen(db_engine, Sighting(venue["room"], mentee, second))
    await seen(db_engine, Sighting(venue["room"], mentee, first))
    await seen(db_engine, Sighting(venue["room"], mentee, second))

    assert await in_room_at(db_engine, session["id"], "mentee") == first


@pytest.mark.usefixtures("door")
async def test_a_sighting_after_arrivals_stop_is_kept_but_does_not_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Present, but too late to change the outcome: shown, never counted."""
    setup = await a_mentor_on(db_engine, "pres-late", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    venue = await room_and_users(db_engine, session["id"])
    at = venue["starts_at"] + dt.timedelta(minutes=16)

    await seen(db_engine, Sighting(venue["room"], str(venue["mentee_id"]), at))

    assert await in_room_at(db_engine, session["id"], "mentee") == at
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "pending"


@pytest.mark.usefixtures("door")
@pytest.mark.parametrize("who", ["a stranger", "a party of another room", "not a uuid"])
async def test_a_sighting_matches_only_a_party_of_that_room(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    """**Room and user, together, in the query.** A forged or mistaken sighting
    for anyone else, or for a party of a different session, changes nothing."""
    setup = await a_mentor_on(db_engine, f"pres-scope-{who[:5]}", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=2)
    other_setup = await a_mentor_on(db_engine, f"pres-other-{who[:5]}", "daily")
    other = await started(db_engine, api_client, other_setup, minutes_ago=2)
    # The fake names every room "room-1"; a real Daily room is named for its
    # session, so give the other session its own, as production would.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET external_room_id = 'room-other' WHERE id = :i"),
            {"i": other["id"]},
        )
    venue = await room_and_users(db_engine, session["id"])
    elsewhere = await room_and_users(db_engine, other["id"])
    user = {
        "a stranger": "0193a5b2-0000-7000-8000-00000000dead",
        "a party of another room": str(elsewhere["mentee_id"]),
        "not a uuid": "spike-mentee-55e2d5c4",
    }[who]

    changed = await seen(db_engine, Sighting(venue["room"], user, venue["starts_at"]))

    assert changed is False
    for row in await attendance_of(db_engine, session["id"]):
        assert row["attendance_status"] == "pending"
    assert (await party(db_engine, other["id"], "mentee"))["attendance_status"] == "pending"


# --------------------------------------------------------------------------
# Settling
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("door")
async def test_a_daily_session_both_parties_were_seen_in_settles_completed_observed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup = await a_mentor_on(db_engine, "pres-settle-both", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    venue = await room_and_users(db_engine, session["id"])
    for user in (venue["mentee_id"], venue["mentor_id"]):
        await seen(db_engine, Sighting(venue["room"], str(user), venue["starts_at"]))

    await settle(db_engine)

    assert await status_of(db_engine, session["id"]) == "completed"
    assert await evidence_of(db_engine, session["id"]) == "observed"


@pytest.mark.usefixtures("door")
async def test_pressing_join_without_entering_a_daily_room_is_a_no_show(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The point of #382.** Both pressed Join; only the mentor was seen."""
    setup = await a_mentor_on(db_engine, "pres-press-only", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    for headers in (setup["mentee_headers"], setup["mentor_headers"]):
        assert (await api_client.post(join_url(session), headers=headers)).status_code == 200
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '20 minutes' WHERE id = :i"),
            {"i": session["id"]},
        )
    # Seen inside the window as it now stands: a sighting is judged against
    # when arrivals stopped, so it must fall before that.
    venue = await room_and_users(db_engine, session["id"])
    in_time = venue["starts_at"] + dt.timedelta(minutes=1)
    await seen(db_engine, Sighting(venue["room"], str(venue["mentor_id"]), in_time))

    await settle(db_engine)

    assert await status_of(db_engine, session["id"]) == "no_show"
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "no_show"
    assert (await party(db_engine, session["id"], "mentor"))["attendance_status"] == "attended"


async def test_a_meet_session_settles_on_the_press_as_reported(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup = await a_mentor_on(db_engine, "pres-meet-settle", "google_meet")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    for headers in (setup["mentee_headers"], setup["mentor_headers"]):
        assert (await api_client.post(join_url(session), headers=headers)).status_code == 200
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '20 minutes' WHERE id = :i"),
            {"i": session["id"]},
        )

    await settle(db_engine)

    assert await status_of(db_engine, session["id"]) == "completed"
    assert await evidence_of(db_engine, session["id"]) == "reported"


async def test_a_daily_session_whose_room_was_never_made_settles_on_the_press(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Nowhere to be seen, so the press decides.** Provisioning leaves the room
    null when Daily is down; without this every party would be settled absent
    and the wrong person refunded."""
    setup = await a_mentor_on(db_engine, "pres-no-room", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=1)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET external_room_id = NULL, meeting_provider = 'daily' "
                "WHERE id = :i"
            ),
            {"i": session["id"]},
        )
    for headers in (setup["mentee_headers"], setup["mentor_headers"]):
        assert (await api_client.post(join_url(session), headers=headers)).status_code == 200
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - interval '20 minutes' WHERE id = :i"),
            {"i": session["id"]},
        )

    await settle(db_engine)

    assert await status_of(db_engine, session["id"]) == "completed"
    assert await evidence_of(db_engine, session["id"]) == "reported"


# --------------------------------------------------------------------------
# Meeting records: the check before settling (#382)
# --------------------------------------------------------------------------


class Records:
    """Daily's meeting records, faked: what each room saw, or a failure."""

    def __init__(
        self, seen: dict[str, list[Sighting]] | None = None, *, error: Exception | None = None
    ):
        self.seen = seen or {}
        self.error = error
        self.asked: list[str] = []

    def sightings(self, room: str) -> list[Sighting]:
        self.asked.append(room)
        if self.error is not None:
            raise self.error
        return self.seen.get(room, [])


async def confirm_then_settle(engine: AsyncEngine, records: Records) -> None:
    """The settlement job's order: check the records, then settle the rest."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        now = dt.datetime.now(dt.UTC)
        unverified = await confirm_presence(session, now=now, rooms=records)
        await settle_attendance(session, now=now, unverified=unverified)
        await session.commit()


async def ended(engine: AsyncEngine, session_id: str, minutes_ago: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET starts_at = now() - make_interval(mins => :m) WHERE id = :i"),
            {"m": minutes_ago, "i": session_id},
        )


@pytest.mark.usefixtures("door")
async def test_the_records_decide_a_party_no_webhook_reported(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The safety net.** Daily stops sending webhooks after three failed
    deliveries; the records still say who was there."""
    setup = await a_mentor_on(db_engine, "rec-found", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    venue = await room_and_users(db_engine, session["id"])
    at = venue["starts_at"] + dt.timedelta(minutes=2)
    records = Records(
        {
            venue["room"]: [
                Sighting(venue["room"], str(u), at)
                for u in (venue["mentee_id"], venue["mentor_id"])
            ]
        }
    )

    await confirm_then_settle(db_engine, records)

    assert records.asked == [venue["room"]]
    assert await status_of(db_engine, session["id"]) == "completed"
    assert await evidence_of(db_engine, session["id"]) == "observed"


@pytest.mark.usefixtures("door")
async def test_unreadable_records_leave_the_session_for_the_next_run(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Silence is not absence.** Settling while Daily is unreachable would
    brand both parties absent and refund the wrong person."""
    setup = await a_mentor_on(db_engine, "rec-down", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    await confirm_then_settle(db_engine, Records(error=VenueUnavailableError("daily is down")))

    assert await status_of(db_engine, session["id"]) == "confirmed"


@pytest.mark.usefixtures("door")
async def test_after_a_day_unreadable_records_no_longer_hold_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Nothing waits forever.** A day after arrivals stopped, the session
    settles on what is known."""
    setup = await a_mentor_on(db_engine, "rec-stale", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    await ended(db_engine, session["id"], 26 * 60)

    await confirm_then_settle(db_engine, Records(error=VenueUnavailableError("daily is down")))

    assert await status_of(db_engine, session["id"]) == "no_show"


@pytest.mark.usefixtures("door")
async def test_a_session_every_party_was_already_seen_in_is_not_looked_up(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only a party still pending sends the job to Daily."""
    setup = await a_mentor_on(db_engine, "rec-skip", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    venue = await room_and_users(db_engine, session["id"])
    for user in (venue["mentee_id"], venue["mentor_id"]):
        await seen(db_engine, Sighting(venue["room"], str(user), venue["starts_at"]))
    records = Records()

    await confirm_then_settle(db_engine, records)

    assert records.asked == []
    assert await status_of(db_engine, session["id"]) == "completed"


# --------------------------------------------------------------------------
# The webhook endpoint (#382)
# --------------------------------------------------------------------------

WEBHOOK = "/api/v1/callbacks/daily"
SECRET = base64.b64encode(b"integration-webhook-secret-32b!").decode()


def with_webhook_secret(api_client: httpx.AsyncClient, secret: str | None) -> None:
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(
        update={"daily_webhook_secret": SecretStr(secret) if secret else None}
    )


def delivery(event: dict[str, Any], *, secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    """A body and the headers Daily would send with it."""
    body = json.dumps(event).encode()
    stamp = "1728432000000"
    digest = hmac.new(base64.b64decode(secret), stamp.encode() + b"." + body, hashlib.sha256)
    return body, {
        "X-Webhook-Timestamp": stamp,
        "X-Webhook-Signature": base64.b64encode(digest.digest()).decode(),
        "Content-Type": "application/json",
    }


def joined_event(room: str, user_id: str, at: dt.datetime) -> dict[str, Any]:
    return {
        "type": "participant.joined",
        "id": f"evt-{user_id}",
        "payload": {"room": room, "user_id": user_id, "joined_at": at.timestamp()},
    }


@pytest.mark.usefixtures("door")
async def test_a_signed_join_marks_the_party_present(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    with_webhook_secret(api_client, SECRET)
    setup = await a_mentor_on(db_engine, "hook-signed", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=2)
    venue = await room_and_users(db_engine, session["id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    body, headers = delivery(joined_event(venue["room"], str(venue["mentee_id"]), at))

    answered = await api_client.post(WEBHOOK, content=body, headers=headers)

    assert answered.status_code == 200, answered.text
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "attended"


@pytest.mark.usefixtures("door")
@pytest.mark.parametrize("forgery", ["unsigned", "wrong secret", "body altered"])
async def test_a_join_that_is_not_daily_s_is_refused_and_records_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, forgery: str
) -> None:
    """**A forged attendance record is the threat.** Anyone on the internet can
    reach this URL, and a valid-looking join would mark a party present."""
    with_webhook_secret(api_client, SECRET)
    setup = await a_mentor_on(db_engine, f"hook-forged-{forgery[:4]}", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=2)
    venue = await room_and_users(db_engine, session["id"])
    event = joined_event(venue["room"], str(venue["mentee_id"]), venue["starts_at"])
    body, headers = delivery(
        event,
        secret=base64.b64encode(b"not ours").decode() if forgery == "wrong secret" else SECRET,
    )
    if forgery == "unsigned":
        headers = {"Content-Type": "application/json"}
    if forgery == "body altered":
        body = body.replace(b"participant.joined", b"participant.joined ")

    answered = await api_client.post(WEBHOOK, content=body, headers=headers)

    assert answered.status_code == 401, answered.text
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "pending"


async def test_a_delivery_signed_with_an_empty_key_is_refused_even_if_one_is_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """**Fails closed below the config layer too.** Config refuses an empty
    secret at start-up; if one were ever installed some other way, the endpoint
    still must not verify against a key anyone can compute."""
    app = api_client._transport.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_construct(
        **(app.state.settings.model_dump() | {"daily_webhook_secret": SecretStr("")})
    )
    body = json.dumps({"test": "test"}).encode()
    stamp = "1"
    forged = base64.b64encode(hmac.new(b"", stamp.encode() + b"." + body, hashlib.sha256).digest())

    answered = await api_client.post(
        WEBHOOK,
        content=body,
        headers={"X-Webhook-Timestamp": stamp, "X-Webhook-Signature": forged.decode()},
    )

    assert answered.status_code == 401, answered.text


async def test_with_no_secret_configured_every_delivery_is_refused(
    api_client: httpx.AsyncClient,
) -> None:
    """**Refused rather than waved through**, as the QStash callback does: an
    unconfigured verifier on a public endpoint must not be quietly open."""
    with_webhook_secret(api_client, None)
    body, headers = delivery({"test": "test"})

    answered = await api_client.post(WEBHOOK, content=body, headers=headers)

    assert answered.status_code == 401, answered.text


@pytest.mark.parametrize(
    "event",
    [{"test": "test"}, {"type": "participant.left", "payload": {}}, {"type": "recording.started"}],
)
async def test_a_signed_delivery_that_is_not_a_join_is_acknowledged(
    api_client: httpx.AsyncClient, event: dict[str, Any]
) -> None:
    """`200`, because Daily's creation check is `{"test": "test"}` and must get a
    200, and refusing other events counts as a failed delivery: three of those
    switch the webhook off."""
    with_webhook_secret(api_client, SECRET)
    body, headers = delivery(event)

    answered = await api_client.post(WEBHOOK, content=body, headers=headers)

    assert answered.status_code == 200, answered.text


# --------------------------------------------------------------------------
# Published to the parties (#382)
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("door")
async def test_the_session_read_shows_when_each_party_was_seen_in_the_room(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`in_room_at` for the party Daily saw, `null` for the one it did not, and
    `joined_at` left as the press."""
    setup = await a_mentor_on(db_engine, "pub-in-room", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=2)
    venue = await room_and_users(db_engine, session["id"])
    at = venue["starts_at"] + dt.timedelta(minutes=1)
    await seen(db_engine, Sighting(venue["room"], str(venue["mentee_id"]), at))

    shown = (
        await api_client.get(f"/api/v1/sessions/{session['id']}", headers=setup["mentor_headers"])
    ).json()

    assert dt.datetime.fromisoformat(shown["mentee"]["in_room_at"]) == at
    assert shown["mentor"]["in_room_at"] is None
    assert shown["mentee"]["joined_at"] is None


# --------------------------------------------------------------------------
# Codex on #393
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("door")
async def test_an_unconfigured_provider_waits_like_an_unreachable_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A removed key must not settle on silence.** Rooms provisioned while a
    key was set still had parties in them; with the key gone the records cannot
    be read, which is the same as unreachable, so the session waits its day."""
    setup = await a_mentor_on(db_engine, "rec-unconfigured", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)

    await confirm_then_settle(db_engine, Records(error=NotImplementedError("no provider")))

    assert await status_of(db_engine, session["id"]) == "confirmed"


@pytest.mark.usefixtures("door")
async def test_an_attended_row_with_no_sighting_does_not_count_for_a_daily_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Presence decides, whatever the row says.** A press recorded by the old
    code during a rolling deploy left `attended` with no `in_room_at`; settling
    on that would call a session nobody entered `completed`. The records are
    checked for it, and without a sighting it is a no-show."""
    setup = await a_mentor_on(db_engine, "rec-old-press", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_participants SET attendance_status = 'attended', "
                "joined_at = now() WHERE session_id = :i"
            ),
            {"i": session["id"]},
        )
    records = Records()

    await confirm_then_settle(db_engine, records)

    assert records.asked, "the records must be read for a party with no sighting"
    assert await status_of(db_engine, session["id"]) == "no_show"
    assert (await party(db_engine, session["id"], "mentee"))["attendance_status"] == "no_show"


@pytest.mark.usefixtures("door")
async def test_once_the_read_budget_is_spent_the_rest_wait_for_the_next_run(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The job must finish** (Codex on #393). Reads are serial and each may
    take Daily's full timeout, so past the budget the remaining sessions are
    left for the next run instead of running the job past its own limit, where
    nothing would settle and every retry would start over."""
    first = await a_mentor_on(db_engine, "budget-a", "daily")
    second = await a_mentor_on(db_engine, "budget-b", "daily")
    sessions = [await started(db_engine, api_client, s, minutes_ago=20) for s in (first, second)]
    records = Records()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        now = dt.datetime.now(dt.UTC)
        unverified = await confirm_presence(session, now=now, rooms=records, budget=dt.timedelta(0))
        await settle_attendance(session, now=now, unverified=unverified)
        await session.commit()

    assert records.asked == []
    for booked in sessions:
        assert await status_of(db_engine, booked["id"]) == "confirmed"


@pytest.mark.usefixtures("door")
async def test_a_stale_session_skipped_for_time_still_waits_for_a_real_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Skipped is not read** (Codex on #393). The day's patience covers records
    Daily would not give; a session the budget never reached was never asked, so
    it waits for a run that asks, however old it is."""
    setup = await a_mentor_on(db_engine, "budget-stale", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=20)
    await ended(db_engine, session["id"], 26 * 60)
    records = Records()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session_:
        now = dt.datetime.now(dt.UTC)
        unverified = await confirm_presence(
            session_, now=now, rooms=records, budget=dt.timedelta(0)
        )
        await settle_attendance(session_, now=now, unverified=unverified)
        await session_.commit()

    assert records.asked == []
    assert await status_of(db_engine, session["id"]) == "confirmed"


@pytest.mark.usefixtures("door")
async def test_records_are_not_read_until_daily_has_had_time_to_write_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Daily writes a join only after ten seconds in the room** (docs.daily.co,
    Meetings), with ~15s granularity. Read at the boundary, a party who arrived
    at the last moment looks absent, so the session waits out the lag."""
    setup = await a_mentor_on(db_engine, "rec-lag", "daily")
    session = await started(db_engine, api_client, setup, minutes_ago=15)
    records = Records()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session_:
        venue = await room_and_users(db_engine, session["id"])
        now = venue["starts_at"] + dt.timedelta(minutes=15, seconds=30)
        unverified = await confirm_presence(session_, now=now, rooms=records)
        await settle_attendance(session_, now=now, unverified=unverified)
        await session_.commit()

    assert records.asked == []
    assert await status_of(db_engine, session["id"]) == "confirmed"
