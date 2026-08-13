"""The public slots endpoint — the first read in this codebase with no viewer.

Every other read scopes to a caller. This one has none, so what stands in place
of authorization is the mentor's own state, and the refusal tests below are
about *that* rather than about tokens.

**The factories here parameterise approval and listing**, which is why they are
not the `make_user` that seven other integration files each define. That
seven-way duplication is real and predates this file; it is recorded rather than
extended.

Availability is declared on **every** weekday so the tests do not depend on
which day they run. Lagos is the zone throughout: it has never observed DST, so
09:00 local is 08:00Z on every date and an expected instant can be written down
rather than computed.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.infra.db.slot_store import list_slots

pytestmark = [pytest.mark.db, pytest.mark.anyio]

LAGOS = "Africa/Lagos"

#: Far enough ahead that no notice window reaches it and nothing is in the past.
FIRST_DAY = dt.date.today() + dt.timedelta(days=7)
LAST_DAY = FIRST_DAY + dt.timedelta(days=7)


def at(day: dt.date, moment: str) -> dt.datetime:
    """A UTC instant on a given day. Lagos is UTC+1, so 09:00 there is 08:00Z."""
    return dt.datetime.combine(day, dt.time.fromisoformat(moment), tzinfo=dt.UTC)


async def make_mentor(
    engine: AsyncEngine,
    tag: str,
    *,
    approved: bool = True,
    listed: bool = True,
    duration: int = 45,
    notice: int = 0,
    active_type: bool = True,
    deleted_type: bool = False,
    config: bool = True,
    windows: bool = True,
) -> tuple[UUID, UUID]:
    """A mentor, their offering, and their weekly availability.

    Returns the mentor's user id and the session type id. Every knob here is a
    reason the endpoint might refuse, so a refusal test flips exactly one.
    """
    async with engine.begin() as conn:
        mentor = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentor', :z) RETURNING id"
                ),
                {"e": f"mentor-{tag}@example.test", "a": uuid4(), "z": LAGOS},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO mentor_profiles (user_id, headline, approval_status, listing_status) "
                "VALUES (:u, 'M', CAST(:a AS approval_status), CAST(:l AS listing_status))"
            ),
            {
                "u": mentor,
                "a": "approved" if approved else "pending",
                "l": "listed" if listed else "unlisted",
            },
        )
        session_type = (
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name, is_active) "
                    "VALUES (:u, 'General Mentorship', :active) RETURNING id"
                ),
                {"u": mentor, "active": active_type},
            )
        ).scalar_one()
        if config:
            await conn.execute(
                text(
                    "INSERT INTO session_type_booking_configs "
                    "(session_type_id, duration_minutes, min_notice_minutes) "
                    "VALUES (:t, :d, :n)"
                ),
                {"t": session_type, "d": duration, "n": notice},
            )
        if deleted_type:
            # Soft-deleted after its config exists, which is the order a real
            # deletion happens in — the config row stays and the join still finds
            # it, so only the predicate keeps this offering out.
            await conn.execute(
                text("UPDATE session_types SET deleted_at = now() WHERE id = :t"),
                {"t": session_type},
            )
        if windows:
            # Every weekday, so no test depends on the date it runs on.
            for day_of_week in range(7):
                await conn.execute(
                    text(
                        "INSERT INTO availability_rules "
                        "(mentor_user_id, day_of_week, start_time, end_time, timezone) "
                        "VALUES (:u, :d, '09:00', '12:00', :z)"
                    ),
                    {"u": mentor, "d": day_of_week, "z": LAGOS},
                )
    return mentor, session_type


async def make_mentee(engine: AsyncEngine, tag: str) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Bo', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"mentee-{tag}@example.test", "a": uuid4()},
            )
        ).scalar_one()


async def book(
    engine: AsyncEngine,
    mentor: UUID,
    mentee: UUID,
    starts_at: dt.datetime,
    *,
    status: str = "confirmed",
    minutes: int = 45,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sessions (mentor_id, mentee_id, status, starts_at, duration_minutes) "
                "VALUES (:m, :e, CAST(:s AS session_status), :t, :d)"
            ),
            {"m": mentor, "e": mentee, "s": status, "t": starts_at, "d": minutes},
        )


def slots_url(
    mentor: UUID, session_type: UUID, *, start: dt.date = FIRST_DAY, end: dt.date = LAST_DAY
) -> str:
    return (
        f"/api/v1/users/{mentor}/availability/slots"
        f"?session_type_id={session_type}&start={start.isoformat()}&end={end.isoformat()}"
    )


async def starts(client: httpx.AsyncClient, url: str) -> list[dt.datetime]:
    """Offered start times as **instants**, never as the strings they arrived in.

    The API renders UTC as `...Z` and `datetime.isoformat()` writes `+00:00`.
    Those are one instant spelled two ways, and comparing the spellings fails
    while the code is correct — which this repository has already spent a
    debugging session on once, against `tstzrange::text`.
    """
    response = await client.get(url)
    assert response.status_code == 200, response.text
    return [dt.datetime.fromisoformat(slot["start"]) for slot in response.json()["data"]]


# --------------------------------------------------------------------------
# Public, and what "public" is bounded by
# --------------------------------------------------------------------------


async def test_slots_are_readable_without_a_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No `Authorization` header at all. This is the only read here like that."""
    mentor, session_type = await make_mentor(db_engine, "public")

    response = await api_client.get(slots_url(mentor, session_type))

    assert response.status_code == 200
    assert response.json()["next_cursor"] is None
    # Seven days, four 45-minute slots in each 09:00-12:00 window.
    assert len(response.json()["data"]) == 28


async def test_the_grid_is_the_mentors_window_and_slots_fit_whole(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """09:00-12:00 Lagos at 45 minutes: four slots, and never 09:15.

    The fourth ends exactly at 12:00. A fifth would end at 12:45, past the time
    the mentor declared, so it is not offered.
    """
    mentor, session_type = await make_mentor(db_engine, "grid")

    first_day = await starts(
        api_client,
        slots_url(mentor, session_type, start=FIRST_DAY, end=FIRST_DAY + dt.timedelta(days=1)),
    )

    assert first_day == [
        at(FIRST_DAY, "08:00"),
        at(FIRST_DAY, "08:45"),
        at(FIRST_DAY, "09:30"),
        at(FIRST_DAY, "10:15"),
    ]


@pytest.mark.parametrize(
    ("tag", "knob"),
    [
        ("unlisted", {"listed": False}),
        ("unapproved", {"approved": False}),
        ("inactive", {"active_type": False}),
        ("deleted", {"deleted_type": True}),
        ("no-config", {"config": False}),
    ],
)
async def test_a_mentor_who_is_not_publicly_bookable_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, knob: dict[str, bool]
) -> None:
    """One response for every reason.

    **`unapproved` is the one that would have shipped wrong.** `apply_mentor_status`
    writes approval or listing and never both, and no CHECK ties them — so a
    `pending` mentor who is `listed` is a legal row, and gating on listing alone
    would publish an unvetted mentor's calendar.
    """
    mentor, session_type = await make_mentor(db_engine, tag, **knob)

    response = await api_client.get(slots_url(mentor, session_type))

    assert response.status_code == 404


async def test_another_mentors_session_type_is_not_readable_through_this_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both ids are in the query. Without the owner half, a caller could price
    one mentor's slots with another mentor's duration."""
    mentor, _ = await make_mentor(db_engine, "owner-a")
    _, other_type = await make_mentor(db_engine, "owner-b")

    response = await api_client.get(slots_url(mentor, other_type))

    assert response.status_code == 404


# --------------------------------------------------------------------------
# What is subtracted
# --------------------------------------------------------------------------


async def test_a_booked_session_removes_its_slot_and_leaves_the_neighbours(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A session ending at 08:45Z frees 08:45Z exactly — both ends are `[)`."""
    mentor, session_type = await make_mentor(db_engine, "booked")
    mentee = await make_mentee(db_engine, "booked")
    await book(db_engine, mentor, mentee, at(FIRST_DAY, "08:45"))

    first_day = await starts(
        api_client,
        slots_url(mentor, session_type, start=FIRST_DAY, end=FIRST_DAY + dt.timedelta(days=1)),
    )

    assert first_day == [
        at(FIRST_DAY, "08:00"),
        at(FIRST_DAY, "09:30"),
        at(FIRST_DAY, "10:15"),
    ]


async def test_a_cancelled_session_still_holds_its_slot(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The settled rule, and the one a reader is most likely to call a bug.

    A mentor who cancelled was busy; handing the time straight back would rebook
    them into it. Releasing it is a deliberate act that does not exist yet, and
    when it does it will be a flag on the session rather than an inference from
    the status.
    """
    mentor, session_type = await make_mentor(db_engine, "cancelled")
    mentee = await make_mentee(db_engine, "cancelled")
    await book(db_engine, mentor, mentee, at(FIRST_DAY, "08:45"), status="cancelled")

    first_day = await starts(
        api_client,
        slots_url(mentor, session_type, start=FIRST_DAY, end=FIRST_DAY + dt.timedelta(days=1)),
    )

    assert at(FIRST_DAY, "08:45") not in first_day


async def test_a_session_starting_before_the_window_still_blocks_inside_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The overlap test is `&&` on the window, not a comparison on `starts_at`.

    A session beginning at 00:30Z runs into the mentor's 08:00Z morning. Its
    start is nowhere near the availability being asked about, so any predicate
    matching starts against the window would miss it and offer time that is
    taken.

    Eight hours, not the thirteen this first tried: `duration_minutes BETWEEN 5
    AND 480` refused that, which also bounds how far any session can reach —
    a useful thing to know about the query, and something the CHECK told us
    rather than anybody remembering.
    """
    mentor, session_type = await make_mentor(db_engine, "spillover")
    mentee = await make_mentee(db_engine, "spillover")
    # 00:30Z + 8h = 08:30Z, through the middle of the first slot.
    await book(
        db_engine,
        mentor,
        mentee,
        at(FIRST_DAY, "00:30"),
        minutes=8 * 60,
    )

    first_day = await starts(
        api_client,
        slots_url(mentor, session_type, start=FIRST_DAY, end=FIRST_DAY + dt.timedelta(days=1)),
    )

    assert at(FIRST_DAY, "08:00") not in first_day, "the session runs through this slot"
    assert at(FIRST_DAY, "08:45") in first_day, "and frees 08:45 exactly, because `[)`"


async def test_notice_hides_the_soonest_slots_without_moving_the_grid(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A notice long enough to swallow the early days leaves the rest starting
    on the same times they always did.

    **Eight days, and the two assertions hold whatever time of day this runs.**
    The cutoff is `now + 8 days`, so every slot on `FIRST_DAY` (seven days out)
    is behind it and every slot on `FIRST_DAY + 2` (nine days out) is ahead of
    it, with a whole day of slack on each side. Nine days was the first attempt
    and it put the cutoff *inside* the day being asserted visible — green or red
    depending on the clock, which is worse than simply wrong.
    """
    mentor, session_type = await make_mentor(db_engine, "notice", notice=8 * 24 * 60)

    offered = await starts(api_client, slots_url(mentor, session_type))

    assert at(FIRST_DAY, "08:00") not in offered
    assert at(FIRST_DAY + dt.timedelta(days=2), "08:00") in offered
    # The grid is untouched: what survives still starts on the mentor's times.
    assert all(slot.minute in {0, 45, 30, 15} for slot in offered)


async def test_a_mentor_with_no_windows_is_bookable_but_has_nothing_free(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """200 with an empty page, not 404.

    "Exists and has nothing free" and "is not publicly bookable" are different
    statements, and collapsing them would tell a caller that a fully booked
    mentor does not exist.
    """
    mentor, session_type = await make_mentor(db_engine, "nowindows", windows=False)

    response = await api_client.get(slots_url(mentor, session_type))

    assert response.status_code == 200
    assert response.json()["data"] == []


# --------------------------------------------------------------------------
# The range
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "start", "end"),
    [
        ("inverted", FIRST_DAY + dt.timedelta(days=3), FIRST_DAY),
        ("equal", FIRST_DAY, FIRST_DAY),
        ("too-wide", FIRST_DAY, FIRST_DAY + dt.timedelta(days=57)),
    ],
)
async def test_an_unusable_range_is_a_client_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, start: dt.date, end: dt.date
) -> None:
    mentor, session_type = await make_mentor(db_engine, f"range-{tag}")

    response = await api_client.get(slots_url(mentor, session_type, start=start, end=end))

    assert response.status_code == 422


async def test_the_widest_allowed_range_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The boundary itself, because an off-by-one here refuses a legal request."""
    mentor, session_type = await make_mentor(db_engine, "range-max")

    response = await api_client.get(
        slots_url(mentor, session_type, start=FIRST_DAY, end=FIRST_DAY + dt.timedelta(days=56))
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------
# The endpoint against the constraint that would refuse the booking
# --------------------------------------------------------------------------


async def test_every_offered_slot_can_actually_be_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The strongest statement available, and it is not a tautology.

    `sessions_no_mentor_double_booking` is the database's own definition of
    "this mentor is busy". Inserting a session at every slot the endpoint
    offered, and requiring all of them to commit, proves the endpoint never
    offers time the database would refuse — without either side asserting the
    other's arithmetic.

    A session already sits in the middle of the first day, so the two mechanisms
    are genuinely being compared rather than both looking at an empty table.
    """
    mentor, session_type = await make_mentor(db_engine, "insertable")
    mentee = await make_mentee(db_engine, "insertable")
    await book(db_engine, mentor, mentee, at(FIRST_DAY, "08:45"))

    offered = await starts(
        api_client,
        slots_url(mentor, session_type, start=FIRST_DAY, end=FIRST_DAY + dt.timedelta(days=3)),
    )
    assert offered, "nothing offered means this test proves nothing"

    for moment in offered:
        await book(db_engine, mentor, mentee, moment)

    async with db_engine.begin() as conn:
        held = (
            await conn.execute(
                text("SELECT count(*) FROM sessions WHERE mentor_id = :m"), {"m": mentor}
            )
        ).scalar_one()
    assert held == len(offered) + 1


# --------------------------------------------------------------------------
# The dates a caller does not send
# --------------------------------------------------------------------------


async def test_omitting_both_dates_returns_a_week_from_the_mentors_today(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The bare request a browse page actually makes."""
    mentor, session_type = await make_mentor(db_engine, "defaults")

    offered = await starts(
        api_client, f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
    )

    assert offered, "a listed mentor with windows every day must offer something"
    span = {slot.date() for slot in offered}
    # Seven days of a Lagos morning, and none of them behind us.
    assert len(span) <= 7
    assert min(offered) >= dt.datetime.now(dt.UTC)


async def test_omitting_only_the_end_takes_a_week_from_the_start_given(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await make_mentor(db_engine, "default-end")

    offered = await starts(
        api_client,
        f"/api/v1/users/{mentor}/availability/slots"
        f"?session_type_id={session_type}&start={FIRST_DAY.isoformat()}",
    )

    # Four 45-minute slots in each of seven 09:00-12:00 windows.
    assert len(offered) == 28
    assert min(offered).date() == FIRST_DAY
    assert max(offered).date() == FIRST_DAY + dt.timedelta(days=6)


async def test_omitting_only_the_start_is_still_range_checked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Validation happens **after** defaulting, which is why it is not at the edge.

    `end` alone is legal to send, and whether the range it implies is legal
    depends on the `start` the server chose. Checking before defaulting would
    check a value the request never carried.
    """
    mentor, session_type = await make_mentor(db_engine, "default-start")

    far_off = dt.date.today() + dt.timedelta(days=300)
    response = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots"
        f"?session_type_id={session_type}&end={far_off.isoformat()}"
    )

    assert response.status_code == 422


async def test_a_missing_session_type_is_still_a_client_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The one parameter that did **not** become optional.

    A slot's length and notice window come from the offering. Falling back to
    "the mentor's only one" would break every caller that omitted it the day a
    mentor adds a second — someone else's edit breaking an unchanged client.
    """
    mentor, _ = await make_mentor(db_engine, "no-type")

    response = await api_client.get(f"/api/v1/users/{mentor}/availability/slots")

    assert response.status_code == 422


async def test_the_default_start_is_the_mentors_today_not_utcs(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor west of UTC, whose evening is still ahead of them.

    At 02:00 UTC a New York mentor is at 21:00 the previous day. Their evening
    window is in the future, and a UTC "today" would start a day later and skip
    it entirely — offering nothing for tonight while the mentor is free. The
    default has to be resolved in *their* zone, and this is the direction that
    fails silently rather than loudly.

    Asserted through the API's own answer rather than by stubbing a clock, so
    this is an end-to-end check that the defaulted path works at all for a mentor
    west of UTC. The *zone* claim is pinned by the test below it, at a fixed
    clock, because a count cannot make it.

    **No exact count here, deliberately.** This is the one test in the file whose
    range starts at the mentor's *today*, so the current day is partly spent: from
    19:00 in New York the evening slots start falling behind `now` and the total
    drops 28, 27, 26, 25, 24, recovering at midnight. An `== 28` therefore failed
    for about five hours in every twenty-four — on unrelated pull requests, and
    reading as a slots defect rather than a clock. The bound below holds at every
    hour: six whole days are always ahead, and today contributes between none and
    four.
    """
    async with db_engine.begin() as conn:
        mentor = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentor', 'America/New_York') RETURNING id"
                ),
                {"e": "mentor-nyc@example.test", "a": uuid4()},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO mentor_profiles (user_id, headline, approval_status, listing_status) "
                "VALUES (:u, 'M', 'approved', 'listed')"
            ),
            {"u": mentor},
        )
        session_type = (
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name) "
                    "VALUES (:u, 'Evenings') RETURNING id"
                ),
                {"u": mentor},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO session_type_booking_configs "
                "(session_type_id, duration_minutes, min_notice_minutes) VALUES (:t, 60, 0)"
            ),
            {"t": session_type},
        )
        for day_of_week in range(7):
            await conn.execute(
                text(
                    "INSERT INTO availability_rules "
                    "(mentor_user_id, day_of_week, start_time, end_time, timezone) "
                    "VALUES (:u, :d, '19:00', '23:00', 'America/New_York')"
                ),
                {"u": mentor, "d": day_of_week},
            )

    offered = await starts(
        api_client, f"/api/v1/users/{mentor}/availability/slots?session_type_id={session_type}"
    )

    assert offered, "an evening-only New York mentor must still offer evenings"
    # Six whole days of four one-hour evening slots are always ahead; today adds
    # between none and four depending on the hour this runs.
    assert 24 <= len(offered) <= 28


async def test_the_default_start_resolves_in_the_mentors_zone_at_a_fixed_clock(
    db_engine: AsyncEngine,
) -> None:
    """The assertion the test above *cannot* make.

    Counting slots for a New York mentor proves nothing about which zone the
    default came from: a UTC "today" and a New York "today" both cover seven
    mentor-local days, so both return 28. The two only diverge in *which* days,
    and only while UTC has rolled over and New York has not — between 00:00 and
    04:00 UTC.

    So this drives the store directly with `now` fixed inside that window. At
    02:00 UTC on the 20th it is 22:00 on the **19th** in New York, and the
    mentor's evening is still going. The default must start on the 19th.
    """
    async with db_engine.begin() as conn:
        mentor = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentor', 'America/New_York') RETURNING id"
                ),
                {"e": "mentor-clock@example.test", "a": uuid4()},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO mentor_profiles (user_id, headline, approval_status, listing_status) "
                "VALUES (:u, 'M', 'approved', 'listed')"
            ),
            {"u": mentor},
        )
        session_type = (
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name) "
                    "VALUES (:u, 'Evenings') RETURNING id"
                ),
                {"u": mentor},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO session_type_booking_configs "
                "(session_type_id, duration_minutes, min_notice_minutes) VALUES (:t, 60, 0)"
            ),
            {"t": session_type},
        )
        for day_of_week in range(7):
            await conn.execute(
                text(
                    "INSERT INTO availability_rules "
                    "(mentor_user_id, day_of_week, start_time, end_time, timezone) "
                    "VALUES (:u, :d, '19:00', '23:00', 'America/New_York')"
                ),
                {"u": mentor, "d": day_of_week},
            )

    # 2026-08-20 02:00Z is 2026-08-19 22:00 in New York.
    now = dt.datetime(2026, 8, 20, 2, 0, tzinfo=dt.UTC)
    async with AsyncSession(db_engine) as session:
        slots = await list_slots(session, mentor, session_type, start=None, end=None, now=now)

    assert slots is not None
    first = min(slot.start for slot in slots)
    # The mentor's own evening of the 19th, one hour of which is still ahead.
    assert first == dt.datetime(2026, 8, 20, 2, 0, tzinfo=dt.UTC), (
        "the default started on UTC's today and skipped the mentor's current evening"
    )


async def test_a_soft_deleted_mentor_profile_has_no_public_slots(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same gap, on the other public endpoint.

    Both read through `mentor_is_public()`, so a missing `deleted_at` clause
    published a removed mentor's calendar as well as their offerings. One
    predicate, one fix, and a test on each endpoint so neither can drift back.
    """
    mentor, session_type = await make_mentor(db_engine, "soft-deleted")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )

    assert (await api_client.get(slots_url(mentor, session_type))).status_code == 404


async def test_a_soft_deleted_user_has_no_public_slots(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same second soft delete, on the other public endpoint."""
    mentor, session_type = await make_mentor(db_engine, "deleted-user")
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    assert (await api_client.get(slots_url(mentor, session_type))).status_code == 404
