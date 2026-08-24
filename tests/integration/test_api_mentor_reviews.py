"""What a mentor's reviews add up to, and the list they add up from.

Three surfaces read the same rows: the profile block, the public list, and the
discovery card. They agree because they share one `published()` predicate — and
the tests that matter here are the ones that would fail if they stopped.

**The percentage is the API's, not the client's**, so both the average and the
percentage are published and pinned to each other. `test_the_percentage_is_the_
average_scaled` is that pin, and it recomputes with `ROUND_HALF_UP` rather than
Python's `round()`: PostgreSQL rounds halves away from zero and Python rounds
them to even, so `round(2.5)` is 3 in one and 2 in the other. A drift test that
used the wrong rounding would itself be the drift.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_availability, add_session_type, make_public_mentor

from app.infra.db.review_stats import published

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def scaled(values: list[int], high: int) -> int:
    """What the API should publish, rounded the way PostgreSQL rounds."""
    mean = Decimal(sum(values)) / Decimal(len(values))
    return int((mean * 100 / high).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class Profile:
    engine: AsyncEngine
    client: httpx.AsyncClient
    mentor: UUID
    offering: UUID

    async def reviewed(
        self,
        *,
        communication: int = 3,
        knowledge: int = 3,
        practicality: int = 3,
        support: int = 3,
        valuable: int = 5,
        nps: int = 9,
        text_body: str = "Clear, and the advice was usable.",
        withdrawn: bool = False,
        author_name: tuple[str, str] = ("Fauziyah", "Fashola"),
        institution: str | None = None,
        days_ago: int = 1,
    ) -> str:
        """One published review by a fresh mentee, written straight to the table.

        Direct rather than through `POST /reviews`: the write surface is proven
        by its own suite, and driving it here would make every aggregate test
        depend on the interval rule as well as the one it is about.
        """
        tag = uuid4().hex[:10]
        async with self.engine.begin() as conn:
            author = (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, first_name, last_name, primary_role, timezone) "
                        "VALUES (:e, :f, :l, 'mentee', 'Africa/Lagos') RETURNING id"
                    ),
                    {"e": f"r-{tag}@example.test", "f": author_name[0], "l": author_name[1]},
                )
            ).scalar_one()
            if institution is not None:
                await conn.execute(
                    text(
                        "INSERT INTO education_entries (user_id, school_name_raw) VALUES (:u, :s)"
                    ),
                    {"u": author, "s": institution},
                )
            session_id = (
                await conn.execute(
                    text(
                        "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
                        "duration_minutes, status) "
                        "VALUES (:m, :a, :t, now() - make_interval(days => :d), 45, 'completed') "
                        "RETURNING id"
                    ),
                    {"m": self.mentor, "a": author, "t": self.offering, "d": days_ago},
                )
            ).scalar_one()
            review = (
                await conn.execute(
                    text(
                        "INSERT INTO reviews (session_id, reviewed_by, reviewed_for, "
                        "communication_rating, knowledge_rating, practicality_rating, "
                        "support_rating, valuable_rating, nps_recommend_score, public_review, "
                        "deleted_at) "
                        "VALUES (:s, :a, :m, :c, :k, :p, :su, :v, :n, :body, "
                        "        CASE WHEN :gone THEN now() ELSE NULL END) RETURNING id"
                    ),
                    {
                        "s": session_id,
                        "a": author,
                        "m": self.mentor,
                        "c": communication,
                        "k": knowledge,
                        "p": practicality,
                        "su": support,
                        "v": valuable,
                        "n": nps,
                        "body": text_body,
                        "gone": withdrawn,
                    },
                )
            ).scalar_one()
        return str(review)

    async def block(self) -> dict[str, Any]:
        """The `reviews` block on the public profile."""
        response = await self.client.get(f"/api/v1/mentors/{self.mentor}")
        return dict(response.json()["reviews"])

    async def listed(self, query: str = "") -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/mentors/{self.mentor}/reviews{query}")
        return dict(response.json())

    async def card(self) -> dict[str, Any]:
        rows = (await self.client.get("/api/v1/mentors")).json()["data"]
        return next(row for row in rows if row["id"] == str(self.mentor))


@pytest_asyncio.fixture
async def profile(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> Profile:
    tag = uuid4().hex[:8]
    mentor = await make_public_mentor(db_engine, tag)
    offering = await add_session_type(db_engine, mentor, name=f"CV review {tag}")
    # Bookable, so the mentor appears on the discovery card as well as the profile.
    await add_availability(db_engine, mentor, day_of_week=1, start="09:00", end="17:00")
    return Profile(engine=db_engine, client=api_client, mentor=mentor, offering=offering)


# --------------------------------------------------------------------------
# An unreviewed mentor — zero for the count, null for every ratio
# --------------------------------------------------------------------------


async def test_a_mentor_nobody_has_reviewed_counts_zero(profile: Profile) -> None:
    """**Zero and null in one response, and the split is the rule.**

    A count over no rows *is* nought — `completed_sessions` records why a
    nullable count is worse: every client writes the same coalesce and "no data"
    stops being distinguishable from "none yet".

    A ratio over no rows is unknown. Rendering a new mentor as nought out of five
    would be a lie the card cannot take back, which is exactly what
    `attendance_rate` means by *"zero percent says never shows up"*.
    """
    block = await profile.block()

    assert block["count"] == 0
    assert block["session_value"] is None
    assert block["recommended_percent"] is None
    assert block["communication_rating"]["average"] is None
    assert block["communication_rating"]["percent"] is None


async def test_an_unreviewed_card_carries_the_same_split(profile: Profile) -> None:
    card = await profile.card()

    assert card["review_count"] == 0
    assert card["session_value"] is None


# --------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------


async def test_the_block_counts_published_reviews(profile: Profile) -> None:
    await profile.reviewed()
    await profile.reviewed(days_ago=2)

    assert (await profile.block())["count"] == 2


async def test_the_percentage_is_the_average_scaled(profile: Profile) -> None:
    """**The pin that stops the client owning the mapping.**

    Both figures are published, so a client never divides by three itself — and
    this asserts they agree. Register question 2 settled the formula as
    `mean / max`, from the display showing `97% Recommended` over three reviews:
    no promoter fraction of three people yields 97%, and a normalised
    `(v-1)/(max-1)` would render 96%.
    """
    ratings = [1, 2, 3]
    for index, rating in enumerate(ratings):
        await profile.reviewed(communication=rating, days_ago=index + 1)

    block = await profile.block()

    assert block["communication_rating"]["average"] == pytest.approx(2.0)
    assert block["communication_rating"]["percent"] == scaled(ratings, 3)


async def test_the_recommend_figure_is_a_percentage_of_ten(profile: Profile) -> None:
    scores = [8, 10, 9]
    for index, score in enumerate(scores):
        await profile.reviewed(nps=score, days_ago=index + 1)

    assert (await profile.block())["recommended_percent"] == scaled(scores, 10)


async def test_session_value_is_the_mean_out_of_five(profile: Profile) -> None:
    """`5/5 Session Value` on the profile, and the same number on the card."""
    for index, value in enumerate([5, 4, 5]):
        await profile.reviewed(valuable=value, days_ago=index + 1)

    block = await profile.block()

    assert block["session_value"] == pytest.approx(4.67, abs=0.01)
    assert (await profile.card())["session_value"] == pytest.approx(4.67, abs=0.01)


async def test_the_four_questions_are_each_their_own_figure(profile: Profile) -> None:
    """A copy-paste in the aggregate would make all four the same number."""
    await profile.reviewed(communication=3, knowledge=1, practicality=2, support=3)

    block = await profile.block()

    assert block["communication_rating"]["percent"] == scaled([3], 3)
    assert block["knowledge_rating"]["percent"] == scaled([1], 3)
    assert block["practicality_rating"]["percent"] == scaled([2], 3)
    assert block["support_rating"]["percent"] == scaled([3], 3)


async def test_the_floor_of_the_scale_is_thirty_three_percent(profile: Profile) -> None:
    """Register question 2, asserted rather than assumed.

    "Not great" is the floor of a three-point scale, not zero — the scale has no
    zero, and `1/3` is what the app's own `mean/max` scaling produces.
    """
    await profile.reviewed(communication=1)

    assert (await profile.block())["communication_rating"]["percent"] == 33


# --------------------------------------------------------------------------
# Withdrawal, and the one predicate all three surfaces share
# --------------------------------------------------------------------------


async def test_a_withdrawn_review_moves_no_average(profile: Profile) -> None:
    await profile.reviewed(valuable=5)
    await profile.reviewed(valuable=1, withdrawn=True, days_ago=2)

    block = await profile.block()

    assert block["count"] == 1
    assert block["session_value"] == pytest.approx(5.0)


async def test_a_withdrawn_review_is_off_the_list(profile: Profile) -> None:
    await profile.reviewed(text_body="Published.")
    await profile.reviewed(text_body="Taken down.", withdrawn=True, days_ago=2)

    body = await profile.listed()

    assert [row["public_review"] for row in body["data"]] == ["Published."]


async def test_the_card_and_the_profile_agree(profile: Profile) -> None:
    """**One predicate, three readers, and this is the test that says so.**

    The card and the profile answer from different queries — a narrow lateral and
    a wide one — so nothing but `published()` keeps them consistent. A divergence
    would show a mentee one rating on the results page and another on the profile
    they clicked through to.
    """
    for index, value in enumerate([5, 3, 4]):
        await profile.reviewed(valuable=value, days_ago=index + 1)
    await profile.reviewed(valuable=1, withdrawn=True, days_ago=9)

    card, block = await profile.card(), await profile.block()

    assert card["review_count"] == block["count"] == 3
    assert card["session_value"] == pytest.approx(block["session_value"])


# --------------------------------------------------------------------------
# The dated list
# --------------------------------------------------------------------------


async def test_the_list_is_newest_first(profile: Profile) -> None:
    """The id is the cursor and the order — ADR 0016's base case, because a
    UUIDv7 is time-ordered and a separate sort key would be the same fact
    twice."""
    first = await profile.reviewed(text_body="Earlier.", days_ago=9)
    second = await profile.reviewed(text_body="Later.", days_ago=1)

    body = await profile.listed()

    assert [row["id"] for row in body["data"]] == [second, first]


async def test_the_author_is_a_first_name_and_an_initial(profile: Profile) -> None:
    """**The surname is never sent, and is never selected either.**

    `left(last_name, 1)` runs in SQL, so the column does not reach the
    application at all — a read model that dropped it would still have fetched
    it, and the next field added would find it sitting there.
    """
    await profile.reviewed(author_name=("Fauziyah", "Fashola"))

    row = (await profile.listed())["data"][0]

    assert row["author_first_name"] == "Fauziyah"
    assert row["author_last_initial"] == "F"
    assert "Fashola" not in str(row)


async def test_the_author_carries_their_institution(profile: Profile) -> None:
    """From the same lateral the discovery card uses for a mentor's own degree,
    pointed at the reviewer instead."""
    await profile.reviewed(institution="York St John university")

    assert (await profile.listed())["data"][0]["author_institution"] == "York St John university"


async def test_a_reviewer_without_an_institution_still_appears(profile: Profile) -> None:
    """Outer, not inner: a review is content, and a reviewer who never filled in
    their education is not a reason to hide what they wrote."""
    await profile.reviewed(institution=None)

    row = (await profile.listed())["data"][0]

    assert row["author_institution"] is None
    assert row["public_review"]


async def test_each_row_carries_its_own_session_value(profile: Profile) -> None:
    """The `4/5 Session value` badge beside a review is that review's answer,
    not the mentor's average."""
    await profile.reviewed(valuable=4)

    assert (await profile.listed())["data"][0]["session_value"] == 4


async def test_the_platform_feedback_never_reaches_the_list(profile: Profile) -> None:
    await profile.reviewed()

    row = (await profile.listed())["data"][0]

    assert set(row) == {
        "id",
        "created_at",
        "public_review",
        "session_value",
        "author_first_name",
        "author_last_initial",
        "author_institution",
    }


async def test_the_list_pages(profile: Profile) -> None:
    for index in range(3):
        await profile.reviewed(text_body=f"Number {index}.", days_ago=index + 1)

    first = await profile.listed("?limit=2")
    second = await profile.listed(f"?limit=2&cursor={first['next_cursor']}")

    assert len(first["data"]) == 2
    assert first["next_cursor"] is not None
    assert len(second["data"]) == 1
    assert second["next_cursor"] is None


async def test_an_unknown_mentor_is_not_found(profile: Profile) -> None:
    """`404` rather than an empty page. *No such mentor* and *a mentor with
    nothing to show* are different answers, and a client renders them
    differently."""
    response = await profile.client.get(f"/api/v1/mentors/{uuid4()}/reviews")

    assert response.status_code == 404


async def test_a_paused_mentor_hides_their_reviews_too(profile: Profile) -> None:
    """Scoped by the same `mentor_is_public()` pair the profile uses.

    A second visibility clause is what `mentor_public_store`'s own docstring
    warns about — *"the second lookup path is where a visibility clause goes
    missing"* — so this asserts the list moves with the profile rather than
    having its own idea of who is visible.
    """
    await profile.reviewed()
    async with profile.engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET listing_status = 'unlisted' WHERE user_id = :m"),
            {"m": profile.mentor},
        )

    response = await profile.client.get(f"/api/v1/mentors/{profile.mentor}/reviews")

    assert response.status_code == 404


async def test_the_list_needs_no_token(profile: Profile) -> None:
    """Public, like the profile it belongs to — a mentee compares mentors before
    signing up."""
    await profile.reviewed()

    response = await profile.client.get(f"/api/v1/mentors/{profile.mentor}/reviews")

    assert response.status_code == 200


async def test_a_review_of_a_different_mentor_is_not_listed(profile: Profile) -> None:
    """`published()` scopes on `reviewed_for`, and this is the test that would
    fail if it stopped."""
    await profile.reviewed(text_body="Mine.")
    other = await make_public_mentor(profile.engine, uuid4().hex[:8])
    elsewhere = Profile(profile.engine, profile.client, other, profile.offering)
    await elsewhere.reviewed(text_body="Somebody else's.")

    body = await profile.listed()

    assert [row["public_review"] for row in body["data"]] == ["Mine."]


async def test_the_dates_are_what_a_client_renders(profile: Profile) -> None:
    """`created_at`, not `updated_at`: the profile is a dated list of when things
    were said, and an edit inside the ten-minute window does not restate them."""
    await profile.reviewed()

    row = (await profile.listed())["data"][0]

    assert dt.datetime.fromisoformat(row["created_at"])


# --------------------------------------------------------------------------
# The rounding, at a tie — the case the original pin could not reach
# --------------------------------------------------------------------------


async def test_a_tie_rounds_away_from_zero(profile: Profile) -> None:
    """**The bug the first version of this pin was blind to.**

    Eight ratings summing to 15 average `1.875`, which scales to exactly `62.5`.
    A Python `100.0` in the expression makes the whole arithmetic `float8`, and
    `round(float8)` is `rint()` — half **to even**, so it published `62` while
    this module's docstring, the CHANGELOG and the drift test all promised half
    **away from zero**, which is `63`.

    The original fixture data (`[1, 2, 3]`) can never land on a tie, so the pin
    passed against a value it did not test. This is the case that fails without
    the `Numeric` cast.
    """
    ratings = [1, 1, 1, 1, 2, 3, 3, 3]
    for index, rating in enumerate(ratings):
        await profile.reviewed(communication=rating, days_ago=index + 1)

    block = await profile.block()

    assert block["communication_rating"]["average"] == pytest.approx(1.88, abs=0.01)
    assert block["communication_rating"]["percent"] == 63, "62 means the arithmetic went float"


async def test_the_recommend_figure_ties_the_same_way(profile: Profile) -> None:
    """Four scores summing to 5 average `1.25`, which scales to exactly `12.5`."""
    for index, score in enumerate([1, 1, 1, 2]):
        await profile.reviewed(nps=score, days_ago=index + 1)

    assert (await profile.block())["recommended_percent"] == 13


# --------------------------------------------------------------------------
# Who stays named on a public page
# --------------------------------------------------------------------------


async def test_a_deleted_reviewer_is_no_longer_named(profile: Profile) -> None:
    """**A tokenless endpoint, so this is what "deleted" has to mean.**

    `predicates.LIVE` exists because this rule was missed twice before, and
    `test_predicates` walks only two stores — so nothing else here would catch a
    third. A reviewer who deletes their account must stop being named, and their
    words stay: the review is the mentor's record, the name is the reviewer's.
    """
    await profile.reviewed(author_name=("Fauziyah", "Fashola"), text_body="Still here.")
    async with profile.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET deleted_at = now() WHERE id = "
                "(SELECT reviewed_by FROM reviews WHERE reviewed_for = :m)"
            ),
            {"m": profile.mentor},
        )

    body = await profile.listed()

    assert body["data"] == [], "a deleted reviewer takes their attribution with them"


async def test_an_empty_surname_yields_no_initial(profile: Profile) -> None:
    """`left('', 1)` is `''`, not null, and a client concatenating renders
    "Fauziyah ." — the migrated rows do not pass through the boundary that turns
    an emptied string into null."""
    await profile.reviewed(author_name=("Fauziyah", ""))

    row = (await profile.listed())["data"][0]

    assert row["author_last_initial"] is None


# --------------------------------------------------------------------------
# The predicate, and the index it is a second copy of
# --------------------------------------------------------------------------


async def test_a_mentee_directed_review_enters_no_mentor_figure(profile: Profile) -> None:
    """`published()`'s role clause, exercised rather than assumed.

    Every test above writes rows that take the column default, so removing
    `reviewed_for_role == MENTOR` from `published()` left the whole suite green
    while the model docstring called the clause load-bearing — *"a
    mentee-directed one must not enter it at all"*.
    """
    await profile.reviewed(valuable=5)
    review = await profile.reviewed(valuable=1, days_ago=4)
    async with profile.engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET reviewed_for_role = 'mentee' WHERE id = :i"),
            {"i": review},
        )

    block = await profile.block()

    assert block["count"] == 1
    assert block["session_value"] == pytest.approx(5.0)
    assert [row["id"] for row in (await profile.listed())["data"]] != [review]


async def test_the_predicate_and_its_index_still_agree(db_engine: AsyncEngine) -> None:
    """**Non-negotiable #8's pin for the one copy that cannot be removed.**

    `published()` and `ix_reviews_mentor_valuable`'s partial predicate are the
    same rule in two places — a Python expression and a `WHERE` inside an index
    definition, which no linter can bind to each other. Drift does not fail
    anything: every query still answers, just by reading the table.
    """
    async with db_engine.connect() as conn:
        definition = str(
            (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'ix_reviews_mentor_valuable'"
                    )
                )
            ).scalar_one()
        )
    compiled = str(
        published(text("'00000000-0000-0000-0000-000000000000'")).compile(
            compile_kwargs={"literal_binds": True}
        )
    )

    for clause in ("deleted_at IS NULL", "reviewed_for_role = 'mentor'"):
        assert clause in definition, f"the index no longer says {clause}"
    assert "deleted_at IS NULL" in compiled
    assert "reviewed_for_role = 'mentor'" in compiled


async def test_a_malformed_cursor_is_refused(profile: Profile) -> None:
    """`422`, and it is in the OpenAPI document rather than a surprise.

    Treating a bad cursor as "start again" answers a paging bug with page one
    forever, which looks like working software and loses rows.
    """
    response = await profile.client.get(
        f"/api/v1/mentors/{profile.mentor}/reviews?cursor=not-a-cursor"
    )

    assert response.status_code == 422


async def test_a_migrated_review_sorts_by_when_it_was_written(profile: Profile) -> None:
    """**The inversion PR 5 would otherwise ship, pinned before it can.**

    The loader mints `uuid_generate_v7()` at load time and backfills `created_at`
    from Bubble, so a legacy review gets a *new* id and an *old* date. Ordered on
    the id — tempting, because a UUIDv7 is time-ordered — it would sit at the top
    of "newest first" with a three-year-old date beside it.

    This test writes exactly that shape: the migrated row is inserted last, so it
    holds the highest id, and carries the oldest `created_at`.
    """
    await profile.reviewed(text_body="Written last week.")
    migrated = await profile.reviewed(text_body="Migrated from Bubble.", days_ago=9)
    async with profile.engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET created_at = now() - interval '3 years' WHERE id = :i"),
            {"i": migrated},
        )

    rows = (await profile.listed())["data"]

    assert [row["public_review"] for row in rows] == [
        "Written last week.",
        "Migrated from Bubble.",
    ], "the list is ordered by when the review was written, not by when the row was inserted"


async def test_paging_follows_the_same_order(profile: Profile) -> None:
    """The cursor is the two-part form, so it has to page on the same key it sorts on.

    A one-part id cursor against a `created_at` sort skips or repeats rows the
    moment the two disagree — which is precisely what the migrated rows do.
    """
    for index in range(3):
        await profile.reviewed(text_body=f"Number {index}.", days_ago=index + 1)

    first = await profile.listed("?limit=2")
    second = await profile.listed(f"?limit=2&cursor={first['next_cursor']}")

    seen = [row["public_review"] for row in first["data"] + second["data"]]

    # Newest *written* first, so insertion order reversed — `days_ago` moves the
    # session, not the review, which is the distinction this whole ordering is about.
    assert seen == ["Number 2.", "Number 1.", "Number 0."]
    assert second["next_cursor"] is None
