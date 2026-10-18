"""`GET /me/mentor-matches`: a mentee's top bookable mentors and why (decision 265)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import (
    add_education,
    make_bookable_mentor,
    make_public_mentor,
    token_for,
)
from tests.integration.test_api_mentors import give_offering, set_study_country

from app.infra.db.mentor_match_store import mentee_answers
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentor-matches"
INTERVIEW = "interview-preparation"
TESTS = "test-preparation"


async def _sql(engine: AsyncEngine, statement: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def set_origin(engine: AsyncEngine, user: UUID, code: str) -> None:
    await _sql(
        engine,
        "INSERT INTO user_profiles (user_id, origin_country_id) "
        "SELECT :u, id FROM countries WHERE code = :c "
        "ON CONFLICT (user_id) DO UPDATE SET origin_country_id = EXCLUDED.origin_country_id",
        u=user,
        c=code,
    )


async def a_mentee(
    engine: AsyncEngine,
    tag: str,
    *,
    goals: tuple[str, ...] = (),
    field: str | None = None,
    degree: str | None = None,
    countries: tuple[str, ...] = (),
    origin: str | None = None,
) -> tuple[UUID, dict[str, str]]:
    """A signed-in mentee holding exactly the answers given."""
    auth_id = uuid4()
    async with engine.begin() as conn:
        user = await add_user(
            conn, f"mentee-{tag}@example.com", auth_id=auth_id, primary_role="mentee"
        )
        await conn.execute(
            text(
                "INSERT INTO mentee_goals (user_id, field_of_study, degree_goal_id) "
                "VALUES (:u, :f, (SELECT id FROM degree_levels WHERE slug = :d))"
            ),
            {"u": user, "f": field, "d": degree},
        )
        for slug in goals:
            await conn.execute(
                text(
                    "INSERT INTO mentee_goal_needs (user_id, service_offering_id) "
                    "SELECT :u, id FROM service_offerings WHERE slug = :s"
                ),
                {"u": user, "s": slug},
            )
        for priority, code in enumerate(countries, start=1):
            await conn.execute(
                text(
                    "INSERT INTO mentee_goal_countries (user_id, country_id, priority) "
                    "SELECT :u, id, :p FROM countries WHERE code = :c"
                ),
                {"u": user, "c": code, "p": priority},
            )
    if origin is not None:
        await set_origin(engine, user, origin)
    return user, bearer(api_token(auth_id))


async def mark_open(engine: AsyncEngine, mentor: UUID) -> None:
    """Give the mentor a fresh next free time, so their state reads `open`."""
    at = (dt.datetime.now(dt.UTC) + dt.timedelta(days=3)).replace(microsecond=0)
    await _sql(
        engine,
        "INSERT INTO mentor_next_availability "
        "(mentor_user_id, next_available_at, bookable_until, computed_at) "
        "VALUES (:u, :at, :until, now())",
        u=mentor,
        at=at,
        until=at - dt.timedelta(hours=1),
    )
    await _sql(
        engine, "DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u", u=mentor
    )


async def matches(
    client: httpx.AsyncClient, headers: dict[str, str], query: str = ""
) -> httpx.Response:
    return await client.get(URL + query, headers=headers)


def ids(body: dict[str, object]) -> list[str]:
    return [m["mentor"]["id"] for m in body["matches"]]  # type: ignore[attr-defined, index]


async def test_the_ranking_end_to_end_with_reasons_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    helper = await make_bookable_mentor(db_engine, "e2e-help")
    await give_offering(db_engine, helper, INTERVIEW)
    # Everything but help areas: field, origin, a slot, the first destination
    # and a higher degree. One shared help area must still outrank it all.
    rest = await make_bookable_mentor(db_engine, "e2e-rest")
    await add_education(db_engine, rest, level="doctorate", course="Économie du Développement")
    await set_origin(db_engine, rest, "NG")
    await set_study_country(db_engine, rest, "GB")
    await mark_open(db_engine, rest)
    nothing = await make_bookable_mentor(db_engine, "e2e-nothing")
    _, headers = await a_mentee(
        db_engine,
        "e2e",
        goals=(INTERVIEW,),
        field="economie du developpement",
        degree="masters",
        countries=("GB", "CA"),
        origin="NG",
    )

    response = await matches(api_client, headers, "?limit=3")

    assert response.status_code == 200
    body = response.json()
    assert ids(body) == [str(helper), str(rest), str(nothing)]
    first, second, third = body["matches"]
    assert [r["code"] for r in first["reasons"]] == ["help_areas"]
    assert [r["code"] for r in second["reasons"]] == ["field", "origin", "study_country", "degree"]
    assert second["reasons"][0] == {"code": "field", "field": "Économie du Développement"}
    assert second["reasons"][1]["country"]["label"] == "Nigeria"
    assert second["reasons"][2]["country"]["label"] == "United Kingdom"
    assert second["reasons"][3]["level"]["label"] == "Doctorate (PhD)"
    assert second["level"]["label"] == "Doctorate (PhD)"
    assert second["study_country"]["label"] == "United Kingdom"
    assert third["reasons"] == []
    assert third["level"] is None and third["study_country"] is None
    assert body["exact"] is False
    assert body["for_mentor_bookable"] is None
    assert response.headers["Cache-Control"] == "private, no-store"


async def test_help_area_reason_names_the_shared_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "help-ids")
    await give_offering(db_engine, mentor, INTERVIEW)
    await give_offering(db_engine, mentor, TESTS)
    _, headers = await a_mentee(db_engine, "help-ids", goals=(TESTS,))

    body = (await matches(api_client, headers)).json()

    async with db_engine.connect() as conn:
        tests_id = (
            await conn.execute(
                text("SELECT id FROM service_offerings WHERE slug = :s"), {"s": TESTS}
            )
        ).scalar_one()
    assert body["matches"][0]["reasons"] == [
        {"code": "help_areas", "offering_ids": [str(tests_id)]}
    ]


async def test_the_mentor_is_the_explore_card_byte_for_byte(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "card")
    await give_offering(db_engine, mentor, INTERVIEW)
    await add_education(db_engine, mentor)
    _, headers = await a_mentee(db_engine, "card", goals=(INTERVIEW,))

    card = (await matches(api_client, headers)).json()["matches"][0]["mentor"]
    listed = (await api_client.get("/api/v1/mentors", headers=headers)).json()["data"]

    assert card == next(row for row in listed if row["id"] == str(mentor))


async def test_exact_when_the_top_meets_every_answered_primary_signal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "exact")
    await give_offering(db_engine, mentor, INTERVIEW)
    await add_education(db_engine, mentor, course="Computer Science")
    await set_origin(db_engine, mentor, "GH")
    _, exact_headers = await a_mentee(
        db_engine, "exact", goals=(INTERVIEW,), field="computer science", origin="GH"
    )
    _, near_headers = await a_mentee(
        db_engine, "near", goals=(INTERVIEW,), field="computer science", origin="KE"
    )

    assert (await matches(api_client, exact_headers)).json()["exact"] is True
    assert (await matches(api_client, near_headers)).json()["exact"] is False


async def test_an_open_slot_outranks_an_otherwise_equal_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    closed = await make_bookable_mentor(db_engine, "slot-closed")
    open_ = await make_bookable_mentor(db_engine, "slot-open")
    await mark_open(db_engine, open_)
    _, headers = await a_mentee(db_engine, "slot")

    assert ids((await matches(api_client, headers)).json()) == [str(open_), str(closed)]


async def test_based_on_is_the_callers_own_answers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_bookable_mentor(db_engine, "basis")
    _, headers = await a_mentee(
        db_engine, "basis", field="Law", degree="masters", countries=("CA", "GB"), origin="NG"
    )

    basis = (await matches(api_client, headers)).json()["based_on"]

    assert basis["field"] == "Law"
    assert basis["degree"]["label"] == "Master's Degree" and basis["degree"]["raw"] is None
    assert basis["country"]["label"] == "Canada"
    assert basis["origin"]["label"] == "Nigeria"


async def test_based_on_is_empty_for_a_mentee_who_answered_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_bookable_mentor(db_engine, "blank")
    auth_id = uuid4()
    async with db_engine.begin() as conn:
        await add_user(conn, "blank@example.com", auth_id=auth_id)

    body = (await matches(api_client, bearer(api_token(auth_id)))).json()

    assert body["based_on"] == {"degree": None, "field": None, "country": None, "origin": None}
    assert len(body["matches"]) == 1
    assert body["exact"] is False


async def test_for_mentor_bookable_returns_only_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await give_offering(db_engine, await make_bookable_mentor(db_engine, "fm-best"), INTERVIEW)
    target = await make_bookable_mentor(db_engine, "fm-target", slug="fm-target")
    await set_origin(db_engine, target, "NG")
    _, headers = await a_mentee(db_engine, "fm", goals=(INTERVIEW,), origin="NG")

    for handle in ("fm-target", str(target)):
        body = (await matches(api_client, headers, f"?for_mentor={handle}")).json()
        assert ids(body) == [str(target)]
        assert body["for_mentor_bookable"] is True
        assert [r["code"] for r in body["matches"][0]["reasons"]] == ["origin"]
        assert body["exact"] is False


@pytest.mark.parametrize("state", ["unlisted", "no-hours", "unknown", "self"])
async def test_for_mentor_not_bookable_returns_the_usual_list_without_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, state: str
) -> None:
    other = await make_bookable_mentor(db_engine, f"fmn-{state}-other")
    handle = "fmn-target"
    if state == "unlisted":
        await make_bookable_mentor(db_engine, f"fmn-{state}", listed=False, slug=handle)
    elif state == "no-hours":
        await make_public_mentor(db_engine, f"fmn-{state}", slug=handle)
    caller, headers = await a_mentee(db_engine, f"fmn-{state}")
    if state == "self":
        await _sql(db_engine, "UPDATE users SET slug = :s WHERE id = :u", s=handle, u=caller)

    body = (await matches(api_client, headers, f"?for_mentor={handle}")).json()

    assert body["for_mentor_bookable"] is False
    assert ids(body) == [str(other)]


async def test_the_caller_is_never_their_own_match(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await make_bookable_mentor(db_engine, "me")
    other = await make_bookable_mentor(db_engine, "me-other")
    headers = bearer(await token_for(db_engine, me))

    assert ids((await matches(api_client, headers, "?limit=10")).json()) == [str(other)]


async def test_a_mentor_who_cannot_be_booked_is_never_matched(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    bookable = await make_bookable_mentor(db_engine, "nb-yes")
    hidden = await make_public_mentor(db_engine, "nb-no-hours")
    await give_offering(db_engine, hidden, INTERVIEW)
    paused = await make_bookable_mentor(db_engine, "nb-paused", listed=False)
    await give_offering(db_engine, paused, INTERVIEW)
    _, headers = await a_mentee(db_engine, "nb", goals=(INTERVIEW,))

    assert ids((await matches(api_client, headers, "?limit=10")).json()) == [str(bookable)]


async def test_empty_only_when_nobody_is_bookable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await make_public_mentor(db_engine, "empty-no-hours")
    _, headers = await a_mentee(db_engine, "empty", goals=(INTERVIEW,))

    body = (await matches(api_client, headers)).json()

    assert body["matches"] == []
    assert body["exact"] is False


async def test_limit_defaults_to_two_and_is_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    for n in range(4):
        await make_bookable_mentor(db_engine, f"limit-{n}")
    _, headers = await a_mentee(db_engine, "limit")

    assert len((await matches(api_client, headers)).json()["matches"]) == 2
    assert len((await matches(api_client, headers, "?limit=1")).json()["matches"]) == 1
    assert len((await matches(api_client, headers, "?limit=10")).json()["matches"]) == 4
    for bad in ("0", "11"):
        response = await matches(api_client, headers, f"?limit={bad}")
        assert response.status_code == 422
        assert response.json()["errors"][0]["pointer"] == "/query/limit"


async def test_a_token_is_required(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401


async def test_no_other_mentees_answers_appear(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "leak")
    await give_offering(db_engine, mentor, INTERVIEW)
    await a_mentee(
        db_engine,
        "leak-other",
        goals=(INTERVIEW,),
        field="Zoological Cryptography",
        countries=("JP",),
        origin="BR",
    )
    _, headers = await a_mentee(db_engine, "leak-me", goals=(INTERVIEW,))

    raw = (await matches(api_client, headers)).text

    for theirs in ("Zoological", "Japan", "Brazil"):
        assert theirs not in raw
    assert '"field":null' in raw.replace(" ", "")


async def test_a_field_inside_a_longer_programme_matches_as_explore_matches_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`FUZZY_FLOOR` is a `strict_word_similarity` floor (Explore's near tier),
    so the field is scored the same way: the mentee's words found in the
    mentor's text. Plain `similarity` reads this pair as 0.34, below the floor."""
    long_name = "Master of Science in Computer Science and Artificial Intelligence"
    mentor = await make_bookable_mentor(db_engine, "field-long")
    await add_education(db_engine, mentor, course=long_name)
    await make_bookable_mentor(db_engine, "field-none")
    _, headers = await a_mentee(db_engine, "field-long", field="Computer Science")

    body = (await matches(api_client, headers)).json()

    assert ids(body)[0] == str(mentor)
    assert body["matches"][0]["reasons"] == [{"code": "field", "field": long_name}]


async def test_a_retired_goal_offering_matches_nobody(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only live goals count, as on Explore (`_live_goals`)."""
    mentor = await make_bookable_mentor(db_engine, "retired")
    await give_offering(db_engine, mentor, TESTS)
    _, headers = await a_mentee(db_engine, "retired", goals=(TESTS,))
    await _sql(db_engine, "UPDATE service_offerings SET is_active = false WHERE slug = :s", s=TESTS)

    body = (await matches(api_client, headers)).json()

    assert body["matches"][0]["reasons"] == []


async def test_a_deleted_education_entry_is_no_field_match(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "deleted-edu")
    await add_education(db_engine, mentor, course="Computer Science", deleted=True)
    _, headers = await a_mentee(db_engine, "deleted-edu", field="Computer Science")

    body = (await matches(api_client, headers)).json()

    assert body["matches"][0]["reasons"] == []
    assert body["exact"] is False


async def test_the_mentees_goal_offerings_are_their_live_ones_only(db_engine: AsyncEngine) -> None:
    """At its own level (decision 157): the card's offerings also drop a retired
    offering, so the API test above would pass without this guard."""
    caller, _ = await a_mentee(db_engine, "live-only", goals=(TESTS, INTERVIEW))
    await _sql(db_engine, "UPDATE service_offerings SET is_active = false WHERE slug = :s", s=TESTS)

    async with AsyncSession(db_engine) as session:
        answers = await mentee_answers(session, caller)
        live = (
            await session.execute(
                text("SELECT id FROM service_offerings WHERE slug = :s"), {"s": INTERVIEW}
            )
        ).scalar_one()

    assert answers["goal_offering_ids"] == [live]
