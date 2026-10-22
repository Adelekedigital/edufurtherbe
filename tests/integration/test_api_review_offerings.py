"""Reviews by service offering: each row's offerings, the filter, and the topics.

Decision 283. The three readers share one predicate (`review_topics._covers`),
so the test that matters most is the one holding a topic's `count` against the
total the filter pages through for the same slug.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, token_for
from tests.integration.test_api_mentor_reviews import Profile, a_profile

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

TESTS = "test-preparation"
INTERVIEW = "interview-preparation"
SCHOLARSHIPS = "scholarships-financial-aid"


@pytest_asyncio.fixture
async def profile(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> Profile:
    return await a_profile(db_engine, api_client)


async def classified(profile: Profile, *slugs: str) -> UUID:
    """A session type of the mentor's covering `slugs`, in that order."""
    session_type = await add_session_type(
        profile.engine, profile.mentor, name=f"Type {uuid4().hex[:8]}"
    )
    async with profile.engine.begin() as conn:
        for position, slug in enumerate(slugs):
            await conn.execute(
                text(
                    "INSERT INTO session_type_offerings "
                    "(session_type_id, service_offering_id, position) "
                    "SELECT :t, id, :p FROM service_offerings WHERE slug = :s"
                ),
                {"t": session_type, "p": position, "s": slug},
            )
    return session_type


async def names(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT slug, display_name FROM service_offerings"))
        return {row.slug: row.display_name for row in rows}


async def retire(engine: AsyncEngine, slug: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE service_offerings SET is_active = false WHERE slug = :s"), {"s": slug}
        )


def ids(body: dict[str, object]) -> list[str]:
    return [row["id"] for row in body["data"]]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Each row's offerings
# --------------------------------------------------------------------------


async def test_each_review_names_its_offerings_in_the_mentors_order(profile: Profile) -> None:
    both = await classified(profile, INTERVIEW, TESTS)
    await profile.reviewed(session_type=both)
    label = await names(profile.engine)

    (row,) = (await profile.listed())["data"]

    assert row["service_offerings"] == [
        {"slug": INTERVIEW, "display_name": label[INTERVIEW]},
        {"slug": TESTS, "display_name": label[TESTS]},
    ]


async def test_a_migrated_review_names_no_offering(profile: Profile) -> None:
    await profile.reviewed(legacy=True)

    (row,) = (await profile.listed())["data"]

    assert row["service_offerings"] == []


async def test_an_unclassified_session_type_names_no_offering(profile: Profile) -> None:
    await profile.reviewed()  # the fixture's own type covers no offering

    (row,) = (await profile.listed())["data"]

    assert row["service_offerings"] == []


# --------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------


async def test_the_filter_keeps_reviews_about_any_of_its_offerings(profile: Profile) -> None:
    tests = await profile.reviewed(session_type=await classified(profile, TESTS), days_ago=3)
    interview = await profile.reviewed(
        session_type=await classified(profile, INTERVIEW), days_ago=2
    )
    await profile.reviewed(session_type=await classified(profile, SCHOLARSHIPS), days_ago=1)
    await profile.reviewed(legacy=True)
    await profile.reviewed()

    either = await profile.listed(f"?offering={TESTS}&offering={INTERVIEW}")
    one = await profile.listed(f"?offering={TESTS}")

    assert ids(either) == [interview, tests]
    assert ids(one) == [tests]


async def test_the_filter_combines_with_session_type(profile: Profile) -> None:
    first = await classified(profile, TESTS)
    second = await classified(profile, TESTS)
    mine = await profile.reviewed(session_type=first)
    await profile.reviewed(session_type=second)

    both = await profile.listed(f"?session_type={first}&offering={TESTS}")
    neither = await profile.listed(f"?session_type={first}&offering={INTERVIEW}")

    assert ids(both) == [mine]
    assert ids(neither) == []


async def test_the_filtered_list_pages_within_the_offering(profile: Profile) -> None:
    tests = await classified(profile, TESTS)
    older = await profile.reviewed(session_type=tests, days_ago=5)
    await profile.reviewed(session_type=await classified(profile, INTERVIEW), days_ago=3)
    newer = await profile.reviewed(session_type=tests, days_ago=1)
    query = f"?offering={TESTS}&limit=1"

    first = await profile.listed(query)
    second = await profile.listed(f"{query}&cursor={first['next_cursor']}")

    assert ids(first) == [newer]
    assert ids(second) == [older]
    assert second["next_cursor"] is None


@pytest.mark.parametrize("slug", ["no-such-offering", TESTS])
async def test_an_unknown_or_retired_slug_is_refused(profile: Profile, slug: str) -> None:
    """Retired counts as unknown, exactly as on `GET /mentors?offering=`."""
    await profile.reviewed(session_type=await classified(profile, TESTS))
    await retire(profile.engine, TESTS)

    refused = await profile.client.get(f"/api/v1/mentors/{profile.mentor}/reviews?offering={slug}")
    live = await profile.client.get(
        f"/api/v1/mentors/{profile.mentor}/reviews?offering={INTERVIEW}"
    )

    assert refused.status_code == 422
    assert live.status_code == 200


async def test_a_blank_slug_is_no_filter(profile: Profile) -> None:
    review = await profile.reviewed()

    body = await profile.listed("?offering=%20")

    assert ids(body) == [review]


# --------------------------------------------------------------------------
# The profile's topics
# --------------------------------------------------------------------------


async def test_each_topic_counts_what_its_filter_pages_through(profile: Profile) -> None:
    tests = await classified(profile, TESTS)
    both = await classified(profile, INTERVIEW, TESTS)
    scholarships = await classified(profile, SCHOLARSHIPS)
    for session_type in (tests, tests, both, scholarships):
        await profile.reviewed(session_type=session_type)
    await profile.reviewed(session_type=tests, withdrawn=True)
    await profile.reviewed(legacy=True)
    await profile.reviewed()

    topics = (await profile.block())["review_topics"]

    assert (topics[0]["slug"], topics[0]["count"]) == (TESTS, 3)
    assert {topic["slug"]: topic["count"] for topic in topics} == {
        TESTS: 3,
        INTERVIEW: 1,
        SCHOLARSHIPS: 1,
    }
    for topic in topics:
        listed = await profile.listed(f"?offering={topic['slug']}&limit=50")
        assert len(listed["data"]) == topic["count"], topic["slug"]


async def test_topics_tied_on_count_are_ordered_by_name(profile: Profile) -> None:
    label = await names(profile.engine)
    for slug in (TESTS, INTERVIEW, SCHOLARSHIPS):
        await profile.reviewed(session_type=await classified(profile, slug))

    topics = (await profile.block())["review_topics"]

    assert [topic["display_name"] for topic in topics] == sorted(
        label[slug] for slug in (TESTS, INTERVIEW, SCHOLARSHIPS)
    )
    assert {topic["count"] for topic in topics} == {1}


async def test_an_unreviewed_mentor_has_no_topics(profile: Profile) -> None:
    assert (await profile.block())["review_topics"] == []


async def test_a_retired_offering_leaves_the_rows_and_the_topics(profile: Profile) -> None:
    await profile.reviewed(session_type=await classified(profile, TESTS, INTERVIEW))
    await retire(profile.engine, TESTS)

    (row,) = (await profile.listed())["data"]
    topics = (await profile.block())["review_topics"]

    assert [offering["slug"] for offering in row["service_offerings"]] == [INTERVIEW]
    assert [(topic["slug"], topic["count"]) for topic in topics] == [(INTERVIEW, 1)]


# --------------------------------------------------------------------------
# Visibility holds under the filter and for the topics
# --------------------------------------------------------------------------


async def test_a_hidden_mentor_is_404_under_the_filter_and_readable_by_themself(
    profile: Profile,
) -> None:
    review = await profile.reviewed(session_type=await classified(profile, TESTS))
    async with profile.engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :u"),
            {"u": profile.mentor},
        )
    own = bearer(await token_for(profile.engine, profile.mentor))
    reviews = f"/api/v1/mentors/{profile.mentor}/reviews?offering={TESTS}"
    client: httpx.AsyncClient = profile.client

    stranger = await client.get(reviews)
    stranger_profile = await client.get(f"/api/v1/mentors/{profile.mentor}")
    mine = await client.get(reviews, headers=own)
    my_profile = await client.get(f"/api/v1/mentors/{profile.mentor}", headers=own)

    assert stranger.status_code == 404
    assert stranger_profile.status_code == 404
    assert ids(mine.json()) == [review]
    assert [(t["slug"], t["count"]) for t in my_profile.json()["reviews"]["review_topics"]] == [
        (TESTS, 1)
    ]
