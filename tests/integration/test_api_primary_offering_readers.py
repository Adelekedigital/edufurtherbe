"""The owner profile's booking settings: venue from the primary offering,
confirmation from the mentor.

**This file was D88's reader step and half of it is now reversed.** D88 moved
`requires_booking_confirmation` onto `session_type_booking_configs` and this
suite pinned the move; the column has gone back to `mentor_profiles`, so the
assertions follow it rather than being deleted. What the file is *for* has not
changed: a write that answers 200 and reaches nothing anybody reads is invisible
to the write suite (which asserts the response) and to the read suite (which
seeds rows directly), and only a write-then-read through the API sees it.

`default_meeting_venue` still comes from the primary offering and its null cases
are still not an edge: `trg_refuse_retiring_a_primary_offering` refuses to retire
an offering something points at, so *release the pointer, then retire* is the
sanctioned two-step and "live offerings, no primary" is a state mentors pass
through by design. That half leaves with the pointer, in the next release.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def as_mentor(engine: AsyncEngine, tag: str, **kwargs: object) -> tuple[UUID, UUID]:
    """A public mentor who can also authenticate. Returns `(user_id, auth_id)`.

    `make_public_mentor` writes no `auth_id`, because every reader that uses it
    is unauthenticated. The write half here needs one.
    """
    mentor = await make_public_mentor(engine, tag, **kwargs)  # type: ignore[arg-type]
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    return mentor, auth_id


async def make_primary(engine: AsyncEngine, mentor: UUID, session_type: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET primary_session_type_id = :t WHERE user_id = :u"),
            {"t": session_type, "u": mentor},
        )


async def release_primary(engine: AsyncEngine, mentor: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET primary_session_type_id = NULL WHERE user_id = :u"),
            {"u": mentor},
        )


async def set_confirmation(engine: AsyncEngine, mentor: UUID, value: bool) -> None:
    """The mentor's own setting — the authority, and what a reader must resolve."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET requires_booking_confirmation = :v WHERE user_id = :u"
            ),
            {"v": value, "u": mentor},
        )


async def stored_confirmation(engine: AsyncEngine, mentor: UUID) -> bool:
    async with engine.connect() as conn:
        return bool(
            (
                await conn.execute(
                    text(
                        "SELECT requires_booking_confirmation "
                        "FROM mentor_profiles WHERE user_id = :u"
                    ),
                    {"u": mentor},
                )
            ).scalar_one()
        )


async def overrides(engine: AsyncEngine, mentor: UUID) -> dict[str, bool | None]:
    """Every offering this mentor has, by name, with its **override** — normally
    null, meaning it follows the mentor."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT st.name, c.requires_booking_confirmation "
                "  FROM session_types st "
                "  JOIN session_type_booking_configs c ON c.session_type_id = st.id "
                " WHERE st.mentor_user_id = :u"
            ),
            {"u": mentor},
        )
        # `.all()` first: `dict()` on a CursorResult takes the mapping path,
        # because it has `.keys()`, and then fails subscripting it. Ruff's
        # C416 autofix rewrote the comprehension into exactly that.
        return dict(rows.all())


def profile_url(user_id: UUID) -> str:
    return f"/api/v1/users/{user_id}/mentor-profile"


async def read_profile(
    client: httpx.AsyncClient, user_id: UUID, auth_id: UUID
) -> dict[str, object]:
    response = await client.get(profile_url(user_id), headers=bearer(api_token(auth_id)))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------------
# The read: venue from the primary, confirmation from the mentor
# --------------------------------------------------------------------------


async def test_the_owner_profile_reads_the_venue_from_the_primary_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The venue half of D88's reader step, which this release does not touch.

    Non-default on purpose, so a reader that returned the column default still
    fails.
    """
    mentor, auth_id = await as_mentor(db_engine, "reads-primary")
    session_type = await add_session_type(db_engine, mentor, name="Mock interview", venue="zoom")
    await make_primary(db_engine, mentor, session_type)

    profile = await read_profile(api_client, mentor, auth_id)

    assert profile["default_meeting_venue"] == "zoom"


async def test_the_owner_profile_reads_confirmation_from_the_mentor_not_the_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The reversal, in one assertion.**

    The two locations are set to *opposite* values, so only the new source can
    produce this answer. The offering says `False` and the mentor says `True`; a
    reader still resolving through the primary offering — which is what shipped
    before this release — answers `False` and fails here.

    This is the shape the original reader step used and it is worth keeping: a
    single-valued fixture cannot tell a moved reader from an unmoved one.
    """
    mentor, auth_id = await as_mentor(db_engine, "confirmation-source")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")
    await make_primary(db_engine, mentor, session_type)
    await set_confirmation(db_engine, mentor, True)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET requires_booking_confirmation = false "
                "WHERE session_type_id = :t"
            ),
            {"t": session_type},
        )

    profile = await read_profile(api_client, mentor, auth_id)

    assert profile["requires_booking_confirmation"] is True


async def test_a_mentor_with_no_primary_still_reports_a_confirmation_setting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Changed deliberately: this asserted `None` and now asserts a bool.**

    It read `None` because the value lived on a primary offering the mentor did
    not have — a chain with a reachable, empty bottom. `mentor_profiles` is
    `NOT NULL` and a mentor row always exists, so there is no mentor for whom
    this is unknown. That is the entire argument for moving the column back, and
    a test still permitting `None` would allow the terminus to go missing again.

    `default_meeting_venue` keeps its null: venue has no mentor-level home, so a
    mentor with no primary genuinely has no venue. The two fields answering
    differently in the same response is the point, not an inconsistency.
    """
    mentor, auth_id = await as_mentor(db_engine, "no-primary")

    profile = await read_profile(api_client, mentor, auth_id)

    assert profile["default_meeting_venue"] is None
    assert profile["requires_booking_confirmation"] is False


async def test_a_released_pointer_still_reads_rather_than_failing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The state the guard creates: live offerings, no primary.

    This is the exact configuration a mentor is in between the two steps of
    swapping which offering is primary, so it must be a legible profile rather
    than an error — and the public offerings must still resolve a venue, which
    they do because they carry their own.
    """
    mentor, auth_id = await as_mentor(db_engine, "released")
    session_type = await add_session_type(db_engine, mentor, name="SOP review", venue="daily")
    await make_primary(db_engine, mentor, session_type)
    await release_primary(db_engine, mentor)

    profile = await read_profile(api_client, mentor, auth_id)
    offerings = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert profile["default_meeting_venue"] is None
    assert [o["meeting_venue"] for o in offerings] == ["daily"], (
        "an offering stopped resolving a venue once its mentor had no primary"
    )


# --------------------------------------------------------------------------
# The write reaches the mentor row, and only that
# --------------------------------------------------------------------------


async def test_toggling_confirmation_survives_a_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Write-then-read through the API, which is the only shape that sees this.

    Asserting the 200 alone passes against a writer that reaches nothing.
    """
    mentor, auth_id = await as_mentor(db_engine, "toggle")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")
    await make_primary(db_engine, mentor, session_type)

    response = await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )
    assert response.status_code in (200, 204), response.text

    profile = await read_profile(api_client, mentor, auth_id)
    assert profile["requires_booking_confirmation"] is True


async def test_the_toggle_writes_the_mentor_and_leaves_every_offering_inheriting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Replaces `test_the_toggle_reaches_every_live_offering`, inverted.**

    That test asserted the fan-out copied the mentor's choice onto all of their
    live offerings, and it was right for the schema it was written against: the
    config column was `NOT NULL` with no inherit, so a mentor-level toggle had
    nowhere else to land. With the mentor column authoritative the copy is not
    merely unnecessary, it is harmful — every offering it wrote would become a
    permanent override, and the next toggle would change `mentor_profiles` while
    every reader kept resolving the stale copy.

    So the assertion is inverted rather than dropped: the offerings must be left
    **null**. What would bring the old test back is an offering-management
    endpoint that sets an override deliberately, per offering — which is a
    different write with a different shape, not this one.
    """
    mentor, auth_id = await as_mentor(db_engine, "fan-out-gone")
    primary = await add_session_type(db_engine, mentor, name="SOP review")
    await add_session_type(db_engine, mentor, name="Mock interview")
    await make_primary(db_engine, mentor, primary)

    await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )

    assert await stored_confirmation(db_engine, mentor) is True
    assert await overrides(db_engine, mentor) == {"SOP review": None, "Mock interview": None}


async def test_a_mentor_with_no_offerings_can_still_store_the_setting(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The case that was a silent no-op, and is why the column moved back.**

    Under the fan-out this `PATCH` answered 200 and wrote nothing: there were no
    offerings to write to, and the docstring on `_fan_out_booking_confirmation`
    argued that was correct because someone with nothing bookable has no booking
    policy. That argument does not survive the mentor having a column — a mentor
    who sets their policy before creating their first offering is the ordinary
    onboarding order, not an edge case, and their choice must outlive the request.
    """
    mentor, auth_id = await as_mentor(db_engine, "no-offerings")

    response = await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )
    assert response.status_code in (200, 204), response.text

    assert await stored_confirmation(db_engine, mentor) is True
    profile = await read_profile(api_client, mentor, auth_id)
    assert profile["requires_booking_confirmation"] is True


async def test_the_toggle_does_not_reach_another_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scoping half. An `UPDATE` missing its `WHERE` changes the platform."""
    mentor, auth_id = await as_mentor(db_engine, "mine")
    other = await make_public_mentor(db_engine, "theirs")

    await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )

    assert await stored_confirmation(db_engine, other) is False


async def test_an_explicit_null_confirmation_is_refused_rather_than_a_500(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`bool`, not `bool | None`, and this is the difference.

    Every route dumps with `exclude_unset=True`, so `| None` was never what made
    the field optional — omitting it already worked. What it bought was the
    right to send an explicit `null`, which `_sent` forwarded to a `NOT NULL`
    column: an authenticated 500 on a value the schema advertised as legal.

    The column is `NOT NULL` again, on a different table, so the trap is the same
    trap and the guard still earns its place.
    """
    mentor, auth_id = await as_mentor(db_engine, "null-confirm")
    await set_confirmation(db_engine, mentor, True)

    response = await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": None},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 422, response.text
    assert await stored_confirmation(db_engine, mentor) is True


async def test_a_patch_that_says_nothing_about_confirmation_leaves_it_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent is not false.

    `_sent` draws this line, and a `PATCH` of the headline alone would otherwise
    reset the mentor's booking policy.
    """
    mentor, auth_id = await as_mentor(db_engine, "untouched")
    await set_confirmation(db_engine, mentor, True)

    await api_client.patch(
        profile_url(mentor),
        json={"headline": "I help with applications"},
        headers=bearer(api_token(auth_id)),
    )

    assert await stored_confirmation(db_engine, mentor) is True
