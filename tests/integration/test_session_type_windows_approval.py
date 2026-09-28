"""Per-offering approval and dedicated hours (Session Types frontend #16 and #15).

**#16:** `requires_booking_confirmation: bool | null` on an offering — `null`
inherits the mentor's own setting, which booking already resolves with
`COALESCE`. **#15:** an offering's own weekly windows, written through
`/me/session-types/{id}/windows`; an offering with windows is bookable in them
and nowhere else, its mentor's general hours no longer apply to it, and blocked
dates still do. And a mentor whose only hours are an offering's windows is live.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

LAGOS = "Africa/Lagos"


def window(**overrides: object) -> dict[str, object]:
    return {"day_of_week": 3, "start_time": "18:00", "end_time": "19:00", "timezone": LAGOS} | (
        overrides
    )


async def own(client: httpx.AsyncClient, auth: UUID, session_type: str) -> dict[str, object]:
    rows = (await client.get(URL, headers=bearer(api_token(auth)))).json()["data"]
    return next(row for row in rows if row["id"] == session_type)


# --------------------------------------------------------------------------
# #16 — approval per offering
# --------------------------------------------------------------------------


async def test_an_offering_can_say_approve_each_request(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "approve-true")

    created = (
        await api_client.post(
            URL, json=body(requires_booking_confirmation=True), headers=bearer(api_token(auth))
        )
    ).json()

    assert (await own(api_client, auth, created["id"]))["requires_booking_confirmation"] is True


async def test_an_offering_inherits_by_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "approve-default")

    created = (await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))).json()

    assert (await own(api_client, auth, created["id"]))["requires_booking_confirmation"] is None


async def test_patch_sets_clears_and_leaves_the_override(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "approve-patch")
    headers = bearer(api_token(auth))
    type_id = (await api_client.post(URL, json=body(), headers=headers)).json()["id"]

    await api_client.patch(
        f"{URL}/{type_id}", json={"requires_booking_confirmation": False}, headers=headers
    )
    after_false = (await own(api_client, auth, type_id))["requires_booking_confirmation"]
    await api_client.patch(f"{URL}/{type_id}", json={"name": "Renamed"}, headers=headers)
    after_rename = (await own(api_client, auth, type_id))["requires_booking_confirmation"]
    await api_client.patch(
        f"{URL}/{type_id}", json={"requires_booking_confirmation": None}, headers=headers
    )
    after_null = (await own(api_client, auth, type_id))["requires_booking_confirmation"]

    assert (after_false, after_rename, after_null) == (False, False, None)


# --------------------------------------------------------------------------
# #15 — dedicated hours per offering
# --------------------------------------------------------------------------


async def test_a_window_is_added_listed_and_used_for_slots(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The offering's slots come from its window alone — the mentor's Monday
    morning rule no longer applies to it."""
    mentor, auth = await as_mentor(db_engine, "window-slots")
    headers = bearer(api_token(auth))
    await add_availability(db_engine, mentor, day_of_week=1, start="09:00", end="12:00")
    type_id = (await api_client.post(URL, json=body(duration_minutes=30), headers=headers)).json()[
        "id"
    ]

    created = await api_client.post(f"{URL}/{type_id}/windows", json=window(), headers=headers)
    listed = (await api_client.get(f"{URL}/{type_id}/windows", headers=headers)).json()["data"]
    today = dt.datetime.now(dt.UTC).date()
    slots = (
        await api_client.get(
            f"/api/v1/users/{mentor}/availability/slots",
            params={
                "session_type_id": type_id,
                "end": (today + dt.timedelta(days=21)).isoformat(),
            },
        )
    ).json()["data"]

    assert created.status_code == 201
    assert [(w["day_of_week"], w["start_time"]) for w in listed] == [(3, "18:00:00")]
    assert slots, "the window offers slots"
    for slot in slots:
        local = dt.datetime.fromisoformat(slot["start"]).astimezone(ZoneInfo(LAGOS))
        assert local.isoweekday() % 7 == 3
        assert dt.time(18) <= local.time() < dt.time(19)


async def test_an_overlapping_window_on_the_same_offering_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "window-overlap")
    headers = bearer(api_token(auth))
    one = (await api_client.post(URL, json=body(name="One"), headers=headers)).json()["id"]
    two = (await api_client.post(URL, json=body(name="Two"), headers=headers)).json()["id"]
    await api_client.post(f"{URL}/{one}/windows", json=window(), headers=headers)

    clash = await api_client.post(
        f"{URL}/{one}/windows", json=window(start_time="18:30", end_time="19:30"), headers=headers
    )
    other_offering = await api_client.post(f"{URL}/{two}/windows", json=window(), headers=headers)

    assert clash.status_code == 409
    assert other_offering.status_code == 201


async def test_a_window_that_does_not_move_forward_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "window-backwards")
    headers = bearer(api_token(auth))
    type_id = (await api_client.post(URL, json=body(), headers=headers)).json()["id"]

    backwards = await api_client.post(
        f"{URL}/{type_id}/windows",
        json=window(start_time="19:00", end_time="18:00"),
        headers=headers,
    )
    no_zone = await api_client.post(
        f"{URL}/{type_id}/windows", json=window(timezone="Mars/Olympus"), headers=headers
    )

    assert backwards.status_code == no_zone.status_code == 422


async def test_editing_and_removing_a_window(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "window-edit")
    headers = bearer(api_token(auth))
    type_id = (await api_client.post(URL, json=body(), headers=headers)).json()["id"]
    window_id = (
        await api_client.post(f"{URL}/{type_id}/windows", json=window(), headers=headers)
    ).json()["id"]
    path = f"{URL}/{type_id}/windows/{window_id}"

    edited = await api_client.patch(path, json={"end_time": "20:00"}, headers=headers)
    listed = (await api_client.get(f"{URL}/{type_id}/windows", headers=headers)).json()["data"]
    removed = await api_client.delete(path, headers=headers)
    readded = await api_client.post(f"{URL}/{type_id}/windows", json=window(), headers=headers)

    assert edited.status_code == 200
    assert [(w["start_time"], w["end_time"]) for w in listed] == [("18:00:00", "20:00:00")]
    assert removed.status_code == 200
    assert readded.status_code == 201, "a removed window no longer blocks its hours"


async def test_another_mentors_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, owner_auth = await as_mentor(db_engine, "window-owner")
    _, intruder_auth = await as_mentor(db_engine, "window-intruder")
    owner = bearer(api_token(owner_auth))
    intruder = bearer(api_token(intruder_auth))
    type_id = (await api_client.post(URL, json=body(), headers=owner)).json()["id"]
    window_id = (
        await api_client.post(f"{URL}/{type_id}/windows", json=window(), headers=owner)
    ).json()["id"]
    path = f"{URL}/{type_id}/windows"

    statuses = [
        (await api_client.get(path, headers=intruder)).status_code,
        (await api_client.post(path, json=window(day_of_week=4), headers=intruder)).status_code,
        (
            await api_client.patch(
                f"{path}/{window_id}", json={"end_time": "21:00"}, headers=intruder
            )
        ).status_code,
        (await api_client.delete(f"{path}/{window_id}", headers=intruder)).status_code,
    ]
    still = (await api_client.get(path, headers=owner)).json()["data"]

    assert statuses == [404, 404, 404, 404]
    assert [w["end_time"] for w in still] == ["19:00:00"]


async def test_a_window_id_under_another_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The window is reached through its offering, so the pair must match."""
    _, auth = await as_mentor(db_engine, "window-pair")
    headers = bearer(api_token(auth))
    one = (await api_client.post(URL, json=body(name="One"), headers=headers)).json()["id"]
    two = (await api_client.post(URL, json=body(name="Two"), headers=headers)).json()["id"]
    window_id = (
        await api_client.post(f"{URL}/{one}/windows", json=window(), headers=headers)
    ).json()["id"]

    response = await api_client.delete(f"{URL}/{two}/windows/{window_id}", headers=headers)

    assert response.status_code == 404


# --------------------------------------------------------------------------
# #192 — a mentor whose only hours are an offering's windows is live
# --------------------------------------------------------------------------


async def test_a_mentor_with_only_offering_windows_is_live(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Slots exist, so the profile and Explore must agree."""
    mentor, auth = await as_mentor(db_engine, "windows-only")
    headers = bearer(api_token(auth))
    type_id = (await api_client.post(URL, json=body(), headers=headers)).json()["id"]
    await api_client.post(f"{URL}/{type_id}/windows", json=window(), headers=headers)

    profile = await api_client.get(f"/api/v1/mentors/{mentor}")
    listed = [row["id"] for row in (await api_client.get("/api/v1/mentors")).json()["data"]]

    assert profile.status_code == 200
    assert str(mentor) in listed


async def test_windows_on_a_switched_off_offering_do_not_make_a_mentor_live(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = (await as_mentor(db_engine, "windows-inactive"))[0]
    offering = await add_session_type(db_engine, mentor, active=False)
    await add_session_type(db_engine, mentor, name="Live one")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO session_type_scheduling_windows "
                "(session_type_id, day_of_week, start_time, end_time, timezone) "
                "VALUES (:t, 3, '18:00', '19:00', :z)"
            ),
            {"t": offering, "z": LAGOS},
        )

    assert (await api_client.get(f"/api/v1/mentors/{mentor}")).status_code == 404


async def test_unknown_offering_id_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "window-unknown")

    response = await api_client.post(
        f"{URL}/{uuid4()}/windows", json=window(), headers=bearer(api_token(auth))
    )

    assert response.status_code == 404
