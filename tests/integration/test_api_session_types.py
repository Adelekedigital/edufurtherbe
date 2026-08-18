"""What a mentor offers, publicly — and the endpoint that makes `/slots` usable.

The second read here with no viewer. As with slots, the refusal tests are about
the *mentor's* state rather than about tokens, because there is no token.

The test that matters most is at the bottom: an id taken from this endpoint,
passed straight to `/slots`. That chain is the only reason this endpoint exists,
and nothing else in either suite asserts it.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def url(mentor: object) -> str:
    return f"/api/v1/users/{mentor}/session-types"


# --------------------------------------------------------------------------
# Public, and what public is bounded by
# --------------------------------------------------------------------------


async def test_a_mentors_offerings_are_readable_without_a_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "public")
    await add_session_type(db_engine, mentor, name="SOP review", duration=45, notice=120)

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["next_cursor"] is None
    (offering,) = response.json()["data"]
    assert offering["name"] == "SOP review"
    assert offering["duration_minutes"] == 45
    assert offering["min_notice_minutes"] == 120


@pytest.mark.parametrize(
    ("tag", "knob"),
    [("unlisted", {"listed": False}), ("unapproved", {"approved": False})],
)
async def test_a_mentor_who_is_not_public_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, knob: dict[str, bool]
) -> None:
    """Both halves of the predicate, separately.

    `apply_mentor_status` writes approval or listing and never both, and no
    CHECK ties them — so `pending` + `listed` is a legal row and listing alone
    would publish an unvetted mentor.
    """
    mentor = await make_public_mentor(db_engine, tag, **knob)
    await add_session_type(db_engine, mentor)

    assert (await api_client.get(url(mentor))).status_code == 404


async def test_a_user_who_is_not_a_mentor_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No mentor profile at all — indistinguishable from an unlisted one."""
    async with db_engine.begin() as conn:
        from uuid import uuid4

        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES ('mentee-only@example.test', :a, 'Bo', 'mentee', 'UTC') RETURNING id"
                ),
                {"a": uuid4()},
            )
        ).scalar_one()

    assert (await api_client.get(url(user))).status_code == 404


async def test_a_public_mentor_offering_nothing_is_an_empty_page_not_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two different statements, and collapsing them would say a mentor who has
    switched everything off does not exist."""
    mentor = await make_public_mentor(db_engine, "nothing")

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.parametrize(
    ("tag", "knob"),
    [("inactive", {"active": False}), ("deleted", {"deleted": True})],
)
async def test_an_offering_that_is_off_or_deleted_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, knob: dict[str, bool]
) -> None:
    """`is_active` covers off, closed and hidden alike (#90); `deleted_at` is
    separate. One covers the other in every happy path, so each gets a test."""
    mentor = await make_public_mentor(db_engine, f"gone-{tag}")
    await add_session_type(db_engine, mentor, name="Live one")
    await add_session_type(db_engine, mentor, name="Gone", **knob)

    response = await api_client.get(url(mentor))

    assert [o["name"] for o in response.json()["data"]] == ["Live one"]


# --------------------------------------------------------------------------
# What the response says, and what it must never say
# --------------------------------------------------------------------------


async def test_a_venue_of_its_own_is_returned_as_is(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**`zoom` used to be the fixture here, and it is no longer selectable.**

    It was chosen precisely because nothing else would produce it, which made it
    a good probe — and that is also why it went: `ConferencingProvider` omits it,
    since nothing can mint a Zoom link and a mentor must not be able to pick a
    venue that cannot host. `daily` does the same job; it is non-default, so a
    reader falling through to the platform fallback still fails here.
    """
    mentor = await make_public_mentor(db_engine, "own-venue")
    await add_session_type(db_engine, mentor, venue="daily")

    (offering,) = (await api_client.get(url(mentor))).json()["data"]

    assert offering["meeting_venue"] == "daily"


# `test_the_offerings_venue_does_not_follow_the_mentors` was deleted by D88's
# contract step. It proved there was no cascade by changing the mentor's
# `default_meeting_venue` after the fact and asserting the offering did not
# follow — and that column no longer exists, so the test could only pass.
#
# The guarantee did not weaken; it moved from a test to the schema. There is
# nothing left to inherit from, and `test_a_venue_of_its_own_is_returned_as_is`
# above still asserts an offering's own venue is what the endpoint returns.


async def test_the_venue_is_never_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """There is no "unset" state, which is why the field is required.

    The guarantee has moved three times and this test has outlived all of them:
    a `COALESCE` onto a `NOT NULL` mentor column, then the config's own
    `NOT NULL DEFAULT`, and now the last step of a three-step resolution. **The
    assertion never changed**, which is the point — it pins the promise rather
    than the mechanism, so each move had to keep it true.
    """
    mentor = await make_public_mentor(db_engine, "always-venue")
    await add_session_type(db_engine, mentor, venue=None)

    (offering,) = (await api_client.get(url(mentor))).json()["data"]

    assert offering["meeting_venue"] == "google_meet"


async def test_an_offering_takes_the_platform_notice_floor(db_engine: AsyncEngine) -> None:
    """**The default is the platform rule, so the default is what needs asserting.**

    The 24-hour no-same-day-booking rule lives in this column's server default,
    because the ETL never sets it and nothing could until offering writes shipped.
    Every migrated offering therefore takes whatever the default says — it said
    120, which permitted booking two hours out against a rule of twenty-four.

    **Passing `notice=None` is the only way to reach the default**, and it did not
    exist until this test needed it: the factory named the column unconditionally,
    as did the slot suite's own fixture, so no test could construct an offering
    that takes it. That is the same trap as `meeting_venue` in the reader step,
    one PR earlier.
    """
    mentor = await make_public_mentor(db_engine, "notice-default")
    session_type = await add_session_type(db_engine, mentor, notice=None)

    async with db_engine.connect() as conn:
        stored = (
            await conn.execute(
                text(
                    "SELECT min_notice_minutes FROM session_type_booking_configs "
                    "WHERE session_type_id = :t"
                ),
                {"t": session_type},
            )
        ).scalar_one()

    assert stored == 1440


@pytest.mark.parametrize("value", [-1, 43201])
async def test_an_insane_notice_is_refused_by_the_database(
    db_engine: AsyncEngine, value: int
) -> None:
    """Sanity, not policy. The 24-hour floor is **not** enforced here — it is a
    product rule and lives at the Pydantic boundary, so that moving it to
    `booking_policies` later is a config change rather than a migration."""
    mentor = await make_public_mentor(db_engine, f"insane-{value}")
    session_type = await add_session_type(db_engine, mentor, config=False)

    with pytest.raises(IntegrityError, match="min_notice_minutes"):
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO session_type_booking_configs "
                    "(session_type_id, duration_minutes, min_notice_minutes) "
                    "VALUES (:t, 45, :n)"
                ),
                {"t": session_type, "n": value},
            )


async def test_zero_notice_is_still_accepted_by_the_database(db_engine: AsyncEngine) -> None:
    """The line between sanity and policy, asserted from the permissive side.

    A `CHECK` at 1440 would have been the obvious move and would have forbidden
    every fixture in this suite from building an offering with no notice window —
    which is how the slot tests exercise the slot maths without a 24-hour gap in
    the way. Fifty-four call sites depend on `0` remaining legal.
    """
    mentor = await make_public_mentor(db_engine, "zero-notice")
    session_type = await add_session_type(db_engine, mentor, notice=0)

    async with db_engine.connect() as conn:
        stored = (
            await conn.execute(
                text(
                    "SELECT min_notice_minutes FROM session_type_booking_configs "
                    "WHERE session_type_id = :t"
                ),
                {"t": session_type},
            )
        ).scalar_one()

    assert stored == 0


# `test_a_config_cannot_be_written_without_a_venue` was deleted by
# `d9e2b74c1f36`. It asserted that an explicit NULL into
# `session_type_booking_configs.meeting_venue` was refused — the trap where a
# writer naming a column unconditionally overrides a server default instead of
# falling back to it, which this repository has now hit three times.
#
# The column is gone, so there is nothing to write a NULL into. The guarantee it
# protected — that a published offering always names a venue — did not weaken; it
# moved to the last step of the resolution and is asserted by
# `test_the_venue_is_never_null` above and by the three resolution tests in
# `test_conferencing_options.py`.
#
# **What would bring it back** is any future `NOT NULL` column with a server
# default that a writer might name unconditionally. The trap belongs to that
# shape, not to this column.


async def test_the_response_carries_nothing_it_should_not(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An allowlist, asserted as one.

    **Changed deliberately: `service_offering`, `application_stage` and
    `custom_stage_label` are now *in* the allowlist.** They were withheld because
    they were free text with no vocabulary, so publishing them would have
    committed this contract to a shape nobody had designed. Both have a designed
    shape now — a reference to the closed taxonomy, and a five-value closed set —
    so the reason lapsed rather than being overruled, and adding a field is
    additive where removing one is breaking.

    The assertion is *changed*, not relaxed: it is still an exact set, so a
    fourth field arriving unannounced still fails.

    **This test used to guard a third thing and no longer can.**
    `custom_meeting_url` was a static room link — a bearer credential anyone
    holding it can walk into — and the assertion that it never reached a public
    response went with the column in D88's contract step. There is nothing left
    to leak. The allowlist above is what still earns its place, and it is also
    what would catch a custom URL reappearing in this payload: a field added to
    the response fails the set comparison whatever it is called.
    """
    mentor = await make_public_mentor(db_engine, "leak")
    await add_session_type(
        db_engine, mentor, service_offering="document-preparation", application_stage="other"
    )

    response = await api_client.get(url(mentor))
    body = response.text
    (offering,) = response.json()["data"]

    assert set(offering) == {
        "id",
        "name",
        "description",
        "duration_minutes",
        "min_notice_minutes",
        "meeting_venue",
        "service_offering",
        "application_stage",
        "custom_stage_label",
    }
    assert offering["service_offering"] == {
        "code": "document-preparation",
        "display_name": "Document Preparation",
    }
    assert offering["application_stage"] == "other"
    assert offering["custom_stage_label"] == "My own wording"
    assert "created_by" not in body


async def test_offerings_are_ordered_by_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Name is unique per mentor among live rows, so the order is total."""
    mentor = await make_public_mentor(db_engine, "ordered")
    for name in ("Visa questions", "Application review", "Mock interview"):
        await add_session_type(db_engine, mentor, name=name)

    names = [o["name"] for o in (await api_client.get(url(mentor))).json()["data"]]

    assert names == ["Application review", "Mock interview", "Visa questions"]


# --------------------------------------------------------------------------
# The reason this endpoint exists
# --------------------------------------------------------------------------


async def test_an_id_from_here_works_against_the_slots_endpoint(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The chain, end to end, with no id known in advance.

    `/slots` requires a `session_type_id` and nothing handed one out until this
    shipped. A client discovers the offering here and asks for its slots — which
    is the whole point, and is asserted nowhere else in either suite.
    """
    mentor = await make_public_mentor(db_engine, "chain")
    await add_session_type(db_engine, mentor, duration=60, notice=0)
    async with db_engine.begin() as conn:
        for day_of_week in range(7):
            await conn.execute(
                text(
                    "INSERT INTO availability_rules "
                    "(mentor_user_id, day_of_week, start_time, end_time, timezone) "
                    "VALUES (:u, :d, '09:00', '12:00', 'Africa/Lagos')"
                ),
                {"u": mentor, "d": day_of_week},
            )

    discovered = (await api_client.get(url(mentor))).json()["data"]
    assert len(discovered) == 1

    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots?session_type_id={discovered[0]['id']}"
    )

    assert slots.status_code == 200
    assert slots.json()["data"], "the discovered offering produced no slots"


async def test_the_list_holds_only_this_mentors_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The owner scope, tested against a database that contains someone else.

    Without `mentor_user_id` in the WHERE this returns every mentor's offerings
    to every caller. A single-mentor fixture cannot tell that apart from correct
    behaviour, which is exactly how a scoping bug survives a green suite.
    """
    mine = await make_public_mentor(db_engine, "scope-mine")
    theirs = await make_public_mentor(db_engine, "scope-theirs")
    await add_session_type(db_engine, mine, name="Mine")
    await add_session_type(db_engine, theirs, name="Theirs")

    names = [o["name"] for o in (await api_client.get(url(mine))).json()["data"]]

    assert names == ["Mine"]


async def test_a_soft_deleted_mentor_profile_is_not_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Approved and listed is not sufficient — the profile must also still exist.

    `mentor_profiles` carries `deleted_at`, and the status columns are untouched
    by a soft delete: a removed mentor stays `approved` + `listed` on the row.
    Checking only those two publishes a mentor who has been deleted.
    """
    mentor = await make_public_mentor(db_engine, "soft-deleted")
    await add_session_type(db_engine, mentor)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )

    assert (await api_client.get(url(mentor))).status_code == 404


async def test_an_offering_with_no_booking_config_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An inner join, and the behaviour it produces is the intended one.

    Without a config there is no duration, so there is no slot length and
    nothing bookable — and `/slots` already 404s for such an offering. Listing
    it here would advertise something no caller could act on.

    Pinned because it is join *shape* rather than an explicit predicate: a
    later change to `LEFT JOIN`, made to support the inheritance in #88, would
    start publishing unconfigured offerings with null durations and nothing
    else would notice.
    """
    mentor = await make_public_mentor(db_engine, "no-config")
    await add_session_type(db_engine, mentor, name="Configured")
    await add_session_type(db_engine, mentor, name="Unconfigured", config=False)

    names = [o["name"] for o in (await api_client.get(url(mentor))).json()["data"]]

    assert names == ["Configured"]


async def test_a_soft_deleted_user_is_not_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The **second** soft delete, on the table beside the one already checked.

    A mentor is a `users` row and a `mentor_profiles` row, each with its own
    `deleted_at`, and nothing ties them together: deleting the user leaves the
    profile approved, listed and undeleted. The predicate checked the profile's
    and published the offerings of a user who no longer exists.
    """
    mentor = await make_public_mentor(db_engine, "deleted-user")
    await add_session_type(db_engine, mentor)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    assert (await api_client.get(url(mentor))).status_code == 404
