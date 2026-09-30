"""The booking window is configuration (Round 5, owner 2026-09-29).

`MAX_BOOKING_WINDOW_DAYS` bounds what any offering or mentor may set and clamps
what they already stored — on read, never rewritten, so raising it back restores
their choice. `DEFAULT_BOOKING_WINDOW_DAYS` is what an offering gets when neither
it nor its mentor sets one. Slots, booking legality and the reads all resolve
through `booking_rules.effective_window_days`, so they cannot disagree.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.factories import add_availability, make_public_mentor
from tests.integration.test_api_booking import a_mentee
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body
from tests.integration.test_booking_window_break import (
    NOW,
    at,
    mentor_defaults,
    mentor_with_hours,
    offering,
)
from tests.integration.test_mentor_next_available import FakeCalendar, card

from app.core.config import Settings
from app.core.errors import ValidationError
from app.domain.availability import BookingWindow
from app.infra.db.next_available_store import refresh_next_available
from app.infra.db.session_writer import book_session
from app.infra.db.slot_store import list_slots
from app.infra.storage.supabase import SupabaseStorage
from conftest import PLATFORM_WINDOW, api_token, bearer, build_api_app, client_for

pytestmark = [pytest.mark.db, pytest.mark.anyio]

#: A deployment that lowered the maximum to a fortnight.
FORTNIGHT = BookingWindow(max_days=14, default_days=14)


async def starts(
    engine: AsyncEngine, mentor: object, session_type: object, window: BookingWindow, days: int
) -> list[dt.datetime]:
    async with AsyncSession(engine) as session:
        slots = await list_slots(
            session,
            mentor,  # type: ignore[arg-type]
            session_type,  # type: ignore[arg-type]
            start=NOW.date(),
            end=NOW.date() + dt.timedelta(days=days),
            now=NOW,
            window=window,
        )
    assert slots is not None
    return [slot.start for slot in slots]


#: The smallest window every allowed notice leaves a slot in (#221).
FOUR_DAYS = BookingWindow(max_days=4, default_days=4)

#: Friday, as `availability_rules.day_of_week` counts (0 = Sunday).
FRIDAY = 5


async def friday_only_mentor(engine: AsyncEngine, tag: str) -> tuple[object, object]:
    """Open only on Fridays, so the first slot of a four-day window starting
    Monday afternoon lies in the window's final, partial day."""
    mentor = await make_public_mentor(engine, tag, timezone="UTC")
    await add_availability(
        engine, mentor, day_of_week=FRIDAY, start="09:00", end="17:00", timezone="UTC"
    )
    return mentor, await offering(engine, mentor, "Friday")


def fortnight_client(db_engine: AsyncEngine, storage: SupabaseStorage | None) -> httpx.AsyncClient:
    settings = Settings(_env_file=None, max_booking_window_days=14, default_booking_window_days=14)
    return client_for(build_api_app(db_engine, storage, settings))


# --------------------------------------------------------------------------
# Clamped on read, restored when the maximum comes back
# --------------------------------------------------------------------------


async def test_a_stored_window_above_a_lowered_max_is_clamped(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "clamp-type")
    session_type = await offering(db_engine, mentor, "Long", window=56)

    found = await starts(db_engine, mentor, session_type, FORTNIGHT, days=14)

    assert at(13, 9) in found
    assert max(found) < NOW + dt.timedelta(days=14)


async def test_raising_the_max_back_restores_the_stored_window(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "clamp-restore")
    session_type = await offering(db_engine, mentor, "Long", window=56)

    await starts(db_engine, mentor, session_type, FORTNIGHT, days=14)
    found = await starts(db_engine, mentor, session_type, PLATFORM_WINDOW, days=56)

    assert at(50, 9) in found


async def test_a_mentor_default_above_the_max_is_clamped_too(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "clamp-mentor")
    await mentor_defaults(db_engine, mentor, window=40, brk=None)
    session_type = await offering(db_engine, mentor, "Inherits")

    found = await starts(db_engine, mentor, session_type, FORTNIGHT, days=14)

    assert max(found) < NOW + dt.timedelta(days=14)


async def test_the_default_window_applies_when_nothing_sets_one(db_engine: AsyncEngine) -> None:
    mentor = await mentor_with_hours(db_engine, "default-window")
    session_type = await offering(db_engine, mentor, "Default")
    week = BookingWindow(max_days=56, default_days=7)

    found = await starts(db_engine, mentor, session_type, week, days=14)

    assert at(6, 9) in found
    assert max(found) < NOW + dt.timedelta(days=7)


async def test_a_slots_range_wider_than_the_max_is_refused(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """The cap on what a client may request follows the maximum: its calendar
    days plus a day of margin each side (`range_cap_days`), so a viewer up to 26
    hours from the mentor can cover the whole window in their own dates. `/slots`
    applies it, not the store's internal lookups."""
    mentor = await mentor_with_hours(db_engine, "range-cap")
    session_type = await offering(db_engine, mentor, "Any")
    start = NOW.date()

    async with fortnight_client(db_engine, api_storage) as client:
        wide = await client.get(
            f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
            f"&start={start}&end={start + dt.timedelta(days=17)}"
        )
        fits = await client.get(
            f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
            f"&start={start}&end={start + dt.timedelta(days=16)}"
        )

    assert wide.status_code == 422, wide.text
    assert "at most 16 days" in wide.text
    assert fits.status_code == 200, fits.text


async def test_booking_beyond_a_lowered_max_is_refused(db_engine: AsyncEngine) -> None:
    """The legality check asks the same grid, so a clamped window refuses the
    booking the stored one would have allowed."""
    mentor = await mentor_with_hours(db_engine, "clamp-book")
    session_type = await offering(db_engine, mentor, "Long", window=56)
    mentee, _ = await a_mentee(db_engine, "clamp-book")
    payload = {"session_type_id": session_type, "starts_at": at(20, 9)}
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        with pytest.raises(ValidationError, match="not available"):
            await book_session(
                session, mentee, payload, now=NOW, require_answers=False, window=FORTNIGHT
            )
        await session.rollback()
        await book_session(
            session, mentee, payload, now=NOW, require_answers=False, window=PLATFORM_WINDOW
        )


# --------------------------------------------------------------------------
# Writes are bounded by the configured maximum
# --------------------------------------------------------------------------


async def test_a_session_type_window_over_the_max_is_refused_by_name(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    _, auth = await as_mentor(db_engine, "write-over-max")
    headers = bearer(api_token(auth))

    async with fortnight_client(db_engine, api_storage) as client:
        created = await client.post(URL, json=body(booking_window_days=30), headers=headers)
        allowed = await client.post(
            URL, json=body(name="Fits", booking_window_days=14), headers=headers
        )
        patched = await client.patch(
            f"{URL}/{allowed.json()['id']}", json={"booking_window_days": 15}, headers=headers
        )

    assert created.status_code == 422, created.text
    assert created.json()["errors"][0]["pointer"] == "/booking_window_days"
    assert allowed.status_code == 201, allowed.text
    assert patched.status_code == 422, patched.text
    assert patched.json()["errors"][0]["pointer"] == "/booking_window_days"


async def test_a_mentor_default_window_over_the_max_is_refused_by_name(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentor, auth = await as_mentor(db_engine, "profile-over-max")
    headers = bearer(api_token(auth))
    url = f"/api/v1/users/{mentor}/mentor-profile"

    async with fortnight_client(db_engine, api_storage) as client:
        refused = await client.patch(url, json={"booking_window_days": 30}, headers=headers)
        allowed = await client.patch(url, json={"booking_window_days": 14}, headers=headers)

    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["pointer"] == "/booking_window_days"
    assert allowed.status_code == 200, allowed.text


# --------------------------------------------------------------------------
# The reads carry what the frontend builds its options and modal from
# --------------------------------------------------------------------------


async def test_the_reads_carry_the_resolved_window_and_the_platform_bounds(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentor, auth = await as_mentor(db_engine, "reads-window")
    headers = bearer(api_token(auth))
    session_type = await offering(db_engine, mentor, "Long", window=56)

    async with fortnight_client(db_engine, api_storage) as client:
        public = (await client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
        own = (await client.get(URL, headers=headers)).json()["data"]
        profile = (
            await client.get(f"/api/v1/users/{mentor}/mentor-profile", headers=headers)
        ).json()

    (listed,) = [t for t in public if t["id"] == str(session_type)]
    (mine,) = [t for t in own if t["id"] == str(session_type)]
    assert listed["booking_window_days"] == 14
    assert (mine["booking_window_days"], mine["effective_booking_window_days"]) == (56, 14)
    assert (profile["max_booking_window_days"], profile["default_booking_window_days"]) == (14, 14)


async def test_with_the_defaults_nothing_changes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "reads-default")
    session_type = await offering(db_engine, mentor, "Plain")

    public = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]
    profile = (
        await api_client.get(
            f"/api/v1/users/{mentor}/mentor-profile", headers=bearer(api_token(auth))
        )
    ).json()

    (listed,) = [t for t in public if t["id"] == str(session_type)]
    assert listed["booking_window_days"] == PLATFORM_WINDOW.default_days
    assert profile["max_booking_window_days"] == PLATFORM_WINDOW.max_days


# --------------------------------------------------------------------------
# A small maximum must not break the internal lookups (review of #309)
# --------------------------------------------------------------------------


async def test_booking_inside_the_smallest_window_is_legal(db_engine: AsyncEngine) -> None:
    """Legality asks the grid over its own few-day span; the client range cap
    must not apply to that internal lookup, or a small window refuses everything."""
    mentor = await mentor_with_hours(db_engine, "tiny-book")
    session_type = await offering(db_engine, mentor, "Tiny")
    mentee, _ = await a_mentee(db_engine, "tiny-book")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        booked = await book_session(
            session,
            mentee,
            {"session_type_id": session_type, "starts_at": at(1, 9)},
            now=NOW,
            require_answers=False,
            window=FOUR_DAYS,
        )
        assert booked is not None
        with pytest.raises(ValidationError, match="not available"):
            await book_session(
                session,
                mentee,
                {"session_type_id": session_type, "starts_at": at(5, 9)},
                now=NOW,
                require_answers=False,
                window=FOUR_DAYS,
            )


async def test_slots_without_an_end_fit_a_small_maximum(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """The implicit range is the default week, cut to the window (plus its last day)."""
    mentor = await mentor_with_hours(db_engine, "tiny-slots")
    session_type = await offering(db_engine, mentor, "Tiny")
    settings = Settings(_env_file=None, max_booking_window_days=4, default_booking_window_days=4)

    async with client_for(build_api_app(db_engine, api_storage, settings)) as client:
        response = await client.get(
            f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
        )

    assert response.status_code == 200, response.text


async def test_a_replayed_create_is_not_revalidated_against_a_lowered_max(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """A retry of a create that already succeeded must get its stored answer,
    whatever the configuration became in between."""
    _, auth = await as_mentor(db_engine, "replay-window")
    headers = bearer(api_token(auth)) | {"Idempotency-Key": "window-replay-1"}
    payload = body(booking_window_days=56)

    first = await api_client.post(URL, json=payload, headers=headers)
    async with fortnight_client(db_engine, api_storage) as client:
        retried = await client.post(URL, json=payload, headers=headers)

    assert first.status_code == 201, first.text
    assert retried.status_code == 201, retried.text
    assert retried.json() == first.json()


# --------------------------------------------------------------------------
# A form resending the stored window after the maximum was lowered
# --------------------------------------------------------------------------


async def test_resending_the_stored_window_is_accepted_after_the_max_drops(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """A form sends back every field it shows. A mentor who only renamed the
    offering must not be refused for a window they set while it was allowed."""
    _, auth = await as_mentor(db_engine, "resend-type")
    headers = bearer(api_token(auth))
    created = (
        await api_client.post(URL, json=body(booking_window_days=40), headers=headers)
    ).json()
    url = f"{URL}/{created['id']}"

    async with fortnight_client(db_engine, api_storage) as client:
        kept = await client.patch(
            url, json={"name": "Renamed", "booking_window_days": 40}, headers=headers
        )
        raised = await client.patch(url, json={"booking_window_days": 41}, headers=headers)
        own = (await client.get(URL, headers=headers)).json()["data"]
    restored = (await api_client.get(URL, headers=headers)).json()["data"]

    assert kept.status_code == 200, kept.text
    assert raised.status_code == 422, raised.text
    assert raised.json()["errors"][0]["pointer"] == "/booking_window_days"
    (mine,) = [t for t in own if t["id"] == created["id"]]
    assert (mine["name"], mine["booking_window_days"]) == ("Renamed", 40)
    assert mine["effective_booking_window_days"] == 14
    (back,) = [t for t in restored if t["id"] == created["id"]]
    assert back["effective_booking_window_days"] == 40


async def test_resending_the_stored_mentor_default_is_accepted_after_the_max_drops(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    mentor, auth = await as_mentor(db_engine, "resend-profile")
    headers = bearer(api_token(auth))
    url = f"/api/v1/users/{mentor}/mentor-profile"
    await api_client.patch(url, json={"booking_window_days": 40}, headers=headers)

    async with fortnight_client(db_engine, api_storage) as client:
        kept = await client.patch(
            url, json={"headline": "New line", "booking_window_days": 40}, headers=headers
        )
        raised = await client.patch(url, json={"booking_window_days": 41}, headers=headers)
        profile = (await client.get(url, headers=headers)).json()

    assert kept.status_code == 200, kept.text
    assert raised.status_code == 422, raised.text
    assert (profile["headline"], profile["booking_window_days"]) == ("New line", 40)


async def test_a_new_mentor_profile_over_the_max_is_refused(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """POST has no stored row to resend, so it keeps the plain check."""
    auth = uuid4()
    async with db_engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"new-mentor-{auth}@example.test", "a": auth},
            )
        ).scalar_one()

    async with fortnight_client(db_engine, api_storage) as client:
        response = await client.post(
            f"/api/v1/users/{user}/mentor-profile",
            json={"booking_window_days": 30},
            headers=bearer(api_token(auth)),
        )

    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["pointer"] == "/booking_window_days"


# --------------------------------------------------------------------------
# Next-available honours the window (Codex re-review of #309)
# --------------------------------------------------------------------------


async def stored_next(engine: AsyncEngine, mentor: object) -> dt.datetime | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT next_available_at FROM mentor_next_availability "
                    "WHERE mentor_user_id = :m"
                ),
                {"m": mentor},
            )
        ).scalar_one_or_none()


async def test_the_refresh_projects_through_the_windows_last_partial_day(
    db_engine: AsyncEngine,
) -> None:
    """A four-day window from Monday 15:00 ends Friday 15:00, so Friday 09:00 is
    bookable — the projection must reach into Friday to find it."""
    mentor, session_type = await friday_only_mentor(db_engine, "partial-day")
    now = NOW + dt.timedelta(hours=15)

    async with AsyncSession(db_engine) as session:
        await refresh_next_available(
            session,
            now=now,
            max_age=dt.timedelta(minutes=5),
            reader=FakeCalendar(),
            window=FOUR_DAYS,
        )
        slots = await list_slots(
            session,
            mentor,
            session_type,
            start=now.date(),
            end=now.date() + dt.timedelta(days=6),
            now=now,
            window=FOUR_DAYS,
        )

    assert await stored_next(db_engine, mentor) == at(4, 9)
    assert slots is not None
    assert all(slot.start < now + dt.timedelta(days=4) for slot in slots)


async def test_a_cached_slot_beyond_a_lowered_window_is_not_advertised(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """The cache was vouched for under the old maximum; `/slots` and booking
    refuse that time under the new one, so the card and profile must not offer it."""
    mentor = await mentor_with_hours(db_engine, "cached-far")
    await offering(db_engine, mentor, "Any")
    async with AsyncSession(db_engine) as session:
        await refresh_next_available(
            session, max_age=dt.timedelta(minutes=5), reader=FakeCalendar(), window=PLATFORM_WINDOW
        )
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_next_availability SET "
                "next_available_at = now() + interval '40 days', "
                "bookable_until = now() + interval '39 days' WHERE mentor_user_id = :m"
            ),
            {"m": mentor},
        )

    wide = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()
    async with fortnight_client(db_engine, api_storage) as client:
        profile = (await client.get(f"/api/v1/mentors/{mentor}")).json()
        listed = await card(client, mentor)

    assert wide["next_available_state"] == "open"
    for shown in (profile, listed):
        assert shown["next_available_state"] != "open"
        assert shown["next_available_at"] is None


async def cache_far_slot(engine: AsyncEngine, mentor: object, days: int) -> None:
    """Refresh under the platform window, then push the cached time out."""
    async with AsyncSession(engine) as session:
        await refresh_next_available(
            session, max_age=dt.timedelta(minutes=5), reader=FakeCalendar(), window=PLATFORM_WINDOW
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_next_availability SET "
                "next_available_at = now() + make_interval(days => :d), "
                "bookable_until = now() + make_interval(days => :d - 1) "
                "WHERE mentor_user_id = :m"
            ),
            {"m": mentor, "d": days},
        )


async def test_a_lowered_default_hides_a_cached_slot_the_inheriting_type_refuses(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """The cache is checked against the cached type's **effective** window, not
    only the maximum: with the default at 14, a type that inherits it refuses
    day 40, while a type that sets its own 56 still offers it."""
    inherits = await mentor_with_hours(db_engine, "default-inherits")
    await offering(db_engine, inherits, "Inherits")
    own = await mentor_with_hours(db_engine, "default-own")
    await offering(db_engine, own, "Own", window=56)
    await cache_far_slot(db_engine, inherits, 40)
    await cache_far_slot(db_engine, own, 40)
    settings = Settings(_env_file=None, max_booking_window_days=56, default_booking_window_days=14)

    async with client_for(build_api_app(db_engine, api_storage, settings)) as client:
        inheriting = (await client.get(f"/api/v1/mentors/{inherits}")).json()
        owning = (await client.get(f"/api/v1/mentors/{own}")).json()
        inheriting_card = await card(client, inherits)

    assert inheriting["next_available_state"] != "open"
    assert inheriting_card["next_available_state"] != "open"
    assert owning["next_available_state"] == "open"


# --------------------------------------------------------------------------
# /slots reaches the window's last, partial day (Codex review of a422584)
# --------------------------------------------------------------------------


async def test_slots_with_no_end_reach_the_windows_last_partial_day(
    db_engine: AsyncEngine,
) -> None:
    """Monday 15:00 plus four days is Friday 15:00: Friday 09:00 is bookable, so
    the implicit range must include Friday, and the instant cutoff drops 15:00."""
    mentor, session_type = await friday_only_mentor(db_engine, "slots-partial")
    now = NOW + dt.timedelta(hours=15)

    async with AsyncSession(db_engine) as session:
        slots = await list_slots(
            session, mentor, session_type, start=None, end=None, now=now, window=FOUR_DAYS
        )

    assert slots is not None
    found = [slot.start for slot in slots]
    assert at(4, 9) in found
    assert all(start < now + dt.timedelta(days=4) for start in found)


async def test_an_explicit_range_through_the_last_partial_day_is_allowed(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> None:
    """A client asking for the window plus its partial final day is not refused."""
    mentor, session_type = await friday_only_mentor(db_engine, "slots-explicit")
    settings = Settings(_env_file=None, max_booking_window_days=4, default_booking_window_days=4)
    start = dt.date.today()
    base = f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"

    async with client_for(build_api_app(db_engine, api_storage, settings)) as client:
        fits = await client.get(f"{base}&start={start}&end={start + dt.timedelta(days=6)}")
        wide = await client.get(f"{base}&start={start}&end={start + dt.timedelta(days=7)}")

    assert fits.status_code == 200, fits.text
    assert wide.status_code == 422, wide.text


# --------------------------------------------------------------------------
# One minimum window, from the longest allowed notice (#311)
# --------------------------------------------------------------------------


async def test_a_window_shorter_than_the_longest_notice_is_refused_on_write(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Three days cannot hold a slot at 72 hours' notice; four always can."""
    mentor, auth = await as_mentor(db_engine, "min-window")
    headers = bearer(api_token(auth))

    short = await api_client.post(URL, json=body(booking_window_days=3), headers=headers)
    fits = await api_client.post(
        URL, json=body(name="Fits", booking_window_days=4), headers=headers
    )
    profile = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile", json={"booking_window_days": 3}, headers=headers
    )

    assert short.status_code == 422, short.text
    assert short.json()["errors"][0]["pointer"] == "/booking_window_days"
    assert fits.status_code == 201, fits.text
    assert profile.status_code == 422, profile.text


async def test_a_stored_window_below_the_minimum_reads_and_books_as_the_minimum(
    db_engine: AsyncEngine,
) -> None:
    mentor = await mentor_with_hours(db_engine, "min-stored")
    session_type = await offering(db_engine, mentor, "Legacy", window=1)

    found = await starts(db_engine, mentor, session_type, PLATFORM_WINDOW, days=7)

    assert at(3, 9) in found
    assert max(found) < NOW + dt.timedelta(days=4)


async def test_the_longest_notice_still_leaves_slots_in_the_smallest_window(
    db_engine: AsyncEngine,
) -> None:
    mentor = await mentor_with_hours(db_engine, "min-notice")
    session_type = await offering(db_engine, mentor, "Slow")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET min_notice_minutes = 4320 "
                "WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )

    found = await starts(db_engine, mentor, session_type, FOUR_DAYS, days=5)

    assert found
    assert at(3, 9) in found


async def test_a_stored_window_below_the_minimum_may_be_resent_unchanged(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A form resending a legacy 2 alongside another edit is not refused; any
    other value below the minimum still is."""
    _, auth = await as_mentor(db_engine, "min-resend")
    headers = bearer(api_token(auth))
    created = (await api_client.post(URL, json=body(), headers=headers)).json()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET booking_window_days = 2 "
                "WHERE session_type_id = :t"
            ),
            {"t": created["id"]},
        )
    url = f"{URL}/{created['id']}"

    kept = await api_client.patch(
        url, json={"name": "Renamed", "booking_window_days": 2}, headers=headers
    )
    lowered = await api_client.patch(url, json={"booking_window_days": 3}, headers=headers)

    assert kept.status_code == 200, kept.text
    assert lowered.status_code == 422, lowered.text
