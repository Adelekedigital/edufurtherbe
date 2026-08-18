"""A mentor's conferencing options, and how an offering resolves a venue.

**The constraints here are the feature.** `meeting_venue` was a label, and a
label could not tell *which provider* from *whether this mentor can host on it* —
`zoom` had no integration, `custom` had nowhere to keep a URL, so two of four
values could not produce a joinable session. What replaces it is a row the mentor
configured, and four constraints are what make it trustworthy:

* `UNIQUE (user_id, provider)` — one row per provider per mentor
* `UNIQUE (user_id, id)` — exists **only** as the target of the composite key
* partial `UNIQUE (user_id) WHERE is_default` — exactly one default
* symmetric `CHECK ((provider = 'custom') = (custom_url IS NOT NULL))`

Each is tested here rather than trusted, because every one of them is invisible
to `alembic check`: it compares tables, columns, types and regular indexes, and
is blind to partial indexes and `CHECK` constraints.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def add_option(
    engine: AsyncEngine,
    mentor: UUID,
    *,
    provider: str = "daily",
    is_default: bool = False,
    custom_url: str | None = None,
) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO mentor_conferencing_options "
                    "(user_id, provider, is_default, custom_url) "
                    "VALUES (:u, :p, :d, :url) RETURNING id"
                ),
                {"u": mentor, "p": provider, "d": is_default, "url": custom_url},
            )
        ).scalar_one()


def resolved(body: dict[str, object]) -> str:
    (offering,) = body["data"]  # type: ignore[index]
    return str(offering["meeting_venue"])  # type: ignore[index]


# --------------------------------------------------------------------------
# The constraints
# --------------------------------------------------------------------------


async def test_a_mentor_cannot_hold_two_rows_for_one_provider(db_engine: AsyncEngine) -> None:
    """`UNIQUE (user_id, provider)`. Two rows for `daily` would make *which one*
    a question with no answer, and the composite key would happily point at
    either."""
    mentor = await make_public_mentor(db_engine, "dup-provider")
    await add_option(db_engine, mentor, provider="daily")

    with pytest.raises(IntegrityError):
        await add_option(db_engine, mentor, provider="daily")


async def test_two_mentors_may_each_hold_the_same_provider(db_engine: AsyncEngine) -> None:
    """The other half, and the one a bare `UNIQUE (provider)` would break.

    Asserted because the uniqueness is *per mentor* — a constraint written one
    column short would pass the test above and make the second mentor on Daily
    impossible, which is every mentor after the first.
    """
    one = await make_public_mentor(db_engine, "same-one")
    two = await make_public_mentor(db_engine, "same-two")

    await add_option(db_engine, one, provider="daily")
    await add_option(db_engine, two, provider="daily")


async def test_a_mentor_cannot_hold_two_defaults(db_engine: AsyncEngine) -> None:
    """The partial unique index. "Exactly one default" is true by construction
    rather than by every writer remembering to clear the old one."""
    mentor = await make_public_mentor(db_engine, "two-defaults")
    await add_option(db_engine, mentor, provider="daily", is_default=True)

    with pytest.raises(IntegrityError):
        await add_option(db_engine, mentor, provider="google_meet", is_default=True)


async def test_a_mentor_may_hold_many_non_defaults(db_engine: AsyncEngine) -> None:
    """The index is **partial**, and this is what that buys.

    A plain `UNIQUE (user_id)` would pass the test above and limit every mentor
    to one option ever — which is the opposite of why the table exists.
    """
    mentor = await make_public_mentor(db_engine, "many-options")
    await add_option(db_engine, mentor, provider="daily", is_default=True)
    await add_option(db_engine, mentor, provider="google_meet")
    await add_option(db_engine, mentor, provider="custom", custom_url="https://example.test/room")


async def test_custom_without_a_url_is_refused(db_engine: AsyncEngine) -> None:
    """The direction the **old** constraint already had.

    `ck_mentor_profiles_custom_url_requires_custom_venue` was
    `custom_url IS NULL OR venue = 'custom'`, which is satisfied by `custom` with
    no URL — a mentor bookable with nowhere to meet.
    """
    mentor = await make_public_mentor(db_engine, "custom-no-url")

    with pytest.raises(IntegrityError):
        await add_option(db_engine, mentor, provider="custom")


async def test_a_url_on_a_platform_provider_is_refused(db_engine: AsyncEngine) -> None:
    """**The direction that is new, and the reason the CHECK is symmetric.**

    A URL sitting on a `google_meet` row is dead data that survives an edit: the
    mentor switches to a platform provider, the stale room link stays, and
    anything that later learns to read `custom_url` reads a room nobody meant to
    offer. The old one-directional constraint permitted exactly this.
    """
    mentor = await make_public_mentor(db_engine, "url-on-meet")

    with pytest.raises(IntegrityError):
        await add_option(
            db_engine, mentor, provider="google_meet", custom_url="https://example.test/room"
        )


async def test_zoom_is_not_selectable(db_engine: AsyncEngine) -> None:
    """`ConferencingProvider` omits it while `MeetingProvider` keeps it.

    Nothing can mint a Zoom link, so offering it would let a mentor choose a
    venue that cannot host — the same failure the symmetric URL check closes from
    the other side. It joins the vocabulary when a connection exists behind it,
    which is what the three connection columns are for.
    """
    mentor = await make_public_mentor(db_engine, "no-zoom")

    with pytest.raises(IntegrityError):
        await add_option(db_engine, mentor, provider="zoom")


async def test_an_offering_cannot_point_at_another_mentors_option(
    db_engine: AsyncEngine,
) -> None:
    """**The composite key, and the reason it is composite.**

    A single-column `FOREIGN KEY (conferencing_option_id)` is satisfied by *any*
    option row. `(mentor_user_id, conferencing_option_id)` makes one mentor's
    offering pointing at another mentor's venue **unrepresentable** rather than
    merely refused by whatever application code remembers to check.

    This is the test that fails if the key is ever declared one column short —
    which would compile, migrate, and pass everything else in this file.
    """
    mine = await make_public_mentor(db_engine, "fk-mine")
    theirs = await make_public_mentor(db_engine, "fk-theirs")
    my_offering = await add_session_type(db_engine, mine, name="Mine")
    their_option = await add_option(db_engine, theirs, provider="daily")

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE session_types SET conferencing_option_id = :o WHERE id = :t"),
                {"o": their_option, "t": my_offering},
            )


async def test_an_option_in_use_cannot_be_deleted(db_engine: AsyncEngine) -> None:
    """`ON DELETE RESTRICT`. An offering pointing at a deleted venue would
    resolve through the fallback silently, changing where a booked session
    happens without anybody choosing that."""
    mentor = await make_public_mentor(db_engine, "fk-restrict")
    await add_session_type(db_engine, mentor, name="Held", venue="daily")

    with pytest.raises(IntegrityError):
        async with db_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM mentor_conferencing_options WHERE user_id = :u"), {"u": mentor}
            )


# --------------------------------------------------------------------------
# Resolution — one test per step, and never null
# --------------------------------------------------------------------------


async def test_step_one_the_offerings_own_option_wins(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The offering names an option, and the mentor's default says otherwise.

    The two disagree deliberately: a resolution that skipped step one and went
    straight to the default would answer `google_meet` and pass a single-valued
    fixture.
    """
    mentor = await make_public_mentor(db_engine, "resolve-own")
    await add_option(db_engine, mentor, provider="google_meet", is_default=True)
    await add_session_type(db_engine, mentor, name="SOP review", venue="daily")

    body = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()

    assert resolved(body) == "daily"


async def test_step_two_the_mentors_default_when_the_offering_chose_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Null on the offering means *use my default* — the same inherit shape
    `requires_booking_confirmation` uses, and for the same reason: the mentor row
    always exists, so the terminus cannot be missing."""
    mentor = await make_public_mentor(db_engine, "resolve-default")
    await add_option(db_engine, mentor, provider="daily", is_default=True)
    await add_session_type(db_engine, mentor, name="SOP review")

    body = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()

    assert resolved(body) == "daily"


async def test_step_three_the_platform_fallback_when_the_mentor_has_no_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The step that looks unreachable, and is the reason this is not a
    two-step chain.**

    Seeding every mentor a default makes this state look impossible, and *"it
    cannot happen because creation always sets it"* is exactly the reasoning that
    failed for `primary_session_type_id`: true until a trigger made
    release-then-retire legal, at which point the venue cascade had a reachable,
    empty bottom and a required field resolved to null.

    `meeting_venue` is required, so without step three this is a 500 rather than
    a wrong answer.
    """
    mentor = await make_public_mentor(db_engine, "resolve-fallback")
    await add_session_type(db_engine, mentor, name="SOP review")

    body = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()

    assert resolved(body) == "google_meet"


async def test_a_non_default_option_is_not_borrowed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Step two reads `WHERE is_default`, not *any* option this mentor holds.

    Without the predicate a mentor with one non-default option would have it
    silently applied to every offering that chose nothing — which is a venue
    nobody selected.
    """
    mentor = await make_public_mentor(db_engine, "resolve-nondefault")
    await add_option(db_engine, mentor, provider="daily", is_default=False)
    await add_session_type(db_engine, mentor, name="SOP review")

    body = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()

    assert resolved(body) == "google_meet"


async def test_the_custom_url_never_reaches_the_public_payload(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A static room link is a bearer credential, not a description.

    `mentor_profiles.custom_meeting_url` was withheld for the same reason and a
    test asserted it appeared nowhere in the body; that test went with the
    column, and this is its replacement now the URL has somewhere to live again.
    """
    mentor = await make_public_mentor(db_engine, "no-url-leak")
    await add_session_type(db_engine, mentor, name="SOP review", venue="custom")

    response = await api_client.get(f"/api/v1/users/{mentor}/session-types")

    assert response.json()["data"][0]["meeting_venue"] == "custom"
    assert "example.test" not in response.text
    assert "custom_url" not in response.text
