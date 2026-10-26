"""One permanent personal invite link per user (owner, 2026-10-15).

Anyone may sign up through a user's link; each arrival becomes its own
`referrals` row, so the referral rules apply to every arrival unchanged. The
owner may replace the random code with a handle; the old code stops resolving
at once and is held from everyone else for `RETIRED_CODE_HOLD`.

The cases that matter most are the ones where money or attribution could slip:
a second unlock from a second arrival, a stranger collecting arrivals through a
handle somebody just dropped, and two requests racing for one row.
"""

import asyncio
import re
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_api_referrals import (
    CLAIM,
    COMPLETE,
    REFERRALS,
    invite,
    seed,
    sources_of,
    unlocks_of,
)

from app.core.config import Settings
from app.core.errors import ConflictError
from app.domain.credits import credit_ladder
from app.domain.referrals import MAX_CODE_LENGTH, RETIRED_CODE_HOLD, claim_window
from app.infra.db import invite_links
from app.infra.db.invite_links import pick_handle
from app.infra.db.referral_writer import claim_referral
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LINK = "/api/v1/me/invite-link"
LOOKUP = "/api/v1/invites/{code}"
NO_STORE = "private, no-store"
#: The landing lookup's one answer for every code nobody can join through.
NOBODY = {"inviter": None}
#: The default claim window, for the writer-level tests (decision 264).
WINDOW = claim_window(Settings(_env_file=None, invite_claim_window_hours=168))  # type: ignore[call-arg]


async def create_link(client: httpx.AsyncClient, auth_id: UUID) -> httpx.Response:
    return await client.post(LINK, headers=bearer(api_token(auth_id)))


async def link_code(client: httpx.AsyncClient, auth_id: UUID) -> str:
    return str((await create_link(client, auth_id)).json()["code"])


async def pick(client: httpx.AsyncClient, auth_id: UUID, handle: str) -> httpx.Response:
    return await client.put(LINK, json={"code": handle}, headers=bearer(api_token(auth_id)))


async def claim(client: httpx.AsyncClient, auth_id: UUID, code: str) -> httpx.Response:
    return await client.post(CLAIM, json={"code": code}, headers=bearer(api_token(auth_id)))


async def complete(client: httpx.AsyncClient, auth_id: UUID) -> httpx.Response:
    return await client.post(COMPLETE, headers=bearer(api_token(auth_id)))


async def lookup(client: httpx.AsyncClient, code: str, **kwargs: Any) -> httpx.Response:
    return await client.get(LOOKUP.format(code=code), **kwargs)


async def users(engine: AsyncEngine, n: int, *, complete: bool = True) -> list[UUID]:
    """``n`` signed-up users; returns their auth ids."""
    ids = [uuid4() for _ in range(n)]
    for auth_id in ids:
        await seed(engine, auth_id, complete=complete)
    return ids


async def user_id_of(engine: AsyncEngine, auth_id: UUID) -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text("SELECT id FROM users WHERE auth_id = :a"), {"a": auth_id}
                    )
                ).scalar_one()
            )
        )


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.begin() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


# --------------------------------------------------------------------------
# POST /me/invite-link — create or return
# --------------------------------------------------------------------------


async def test_the_first_request_creates_the_link_and_the_second_returns_it(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)

    first = await create_link(api_client, owner)
    second = await create_link(api_client, owner)

    assert first.status_code == 201
    assert first.headers["Location"] == LINK
    assert re.fullmatch(r"friend-[a-z0-9]{4}", first.json()["code"])
    assert second.status_code == 200
    assert "Location" not in second.headers
    assert second.json() == first.json()
    assert set(first.json()) == {"code", "active"}
    assert first.json()["active"] is True


async def test_concurrent_first_requests_agree_on_one_code(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """`UNIQUE (user_id)` arbitrates: the loser reads the winner's row."""
    (owner,) = await users(db_engine, 1)

    responses = await asyncio.gather(*(create_link(api_client, owner) for _ in range(3)))

    assert sorted(r.status_code for r in responses) == [200, 200, 201]
    assert len({r.json()["code"] for r in responses}) == 1
    owner_id = await user_id_of(db_engine, owner)
    assert (
        await scalar(db_engine, "SELECT count(*) FROM invite_links WHERE user_id = :u", u=owner_id)
        == 1
    )


async def test_two_users_get_two_different_links(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    first, second = await users(db_engine, 2)

    assert await link_code(api_client, first) != await link_code(api_client, second)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("POST", LINK, None), ("PUT", LINK, {"code": "ada-lovelace"})],
)
async def test_the_link_endpoints_need_a_token(
    api_client: httpx.AsyncClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    assert (await api_client.request(method, path, json=body)).status_code == 401


# --------------------------------------------------------------------------
# PUT /me/invite-link — picking a handle
# --------------------------------------------------------------------------


async def test_a_handle_is_picked_lowercased_and_creates_the_link(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """No link yet: picking a handle creates one carrying it."""
    (owner,) = await users(db_engine, 1)

    response = await pick(api_client, owner, "Ada-Lovelace")

    assert response.status_code == 200
    assert response.json() == {"code": "ada-lovelace", "active": True}
    assert (await create_link(api_client, owner)).json() == {"code": "ada-lovelace", "active": True}


async def test_picking_the_handle_you_have_is_idempotent(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A retry must not retire the code onto itself, or the hold would stop
    the owner's own link."""
    (owner,) = await users(db_engine, 1)
    await pick(api_client, owner, "same-handle")

    again = await pick(api_client, owner, "same-handle")

    assert again.status_code == 200
    assert again.json() == {"code": "same-handle", "active": True}
    assert (
        await scalar(
            db_engine,
            "SELECT count(*) FROM invite_link_retired_codes WHERE code = 'same-handle'",
        )
        == 0
    )


@pytest.mark.parametrize("handle", ["ab", "x" * 31, "-ada", "ada-", "ada--x", "ada_x", "a b"])
async def test_a_malformed_handle_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, handle: str
) -> None:
    (owner,) = await users(db_engine, 1)

    response = await pick(api_client, owner, handle)

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/code"


@pytest.mark.parametrize("handle", ["admin", "Support", "edufurther", "undefined"])
async def test_a_reserved_handle_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, handle: str
) -> None:
    (owner,) = await users(db_engine, 1)

    assert (await pick(api_client, owner, handle)).status_code == 422


async def test_a_handle_another_user_holds_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    first, second = await users(db_engine, 2)
    await pick(api_client, first, "taken-one")

    response = await pick(api_client, second, "taken-one")

    assert response.status_code == 409
    assert (await create_link(api_client, first)).json() == {"code": "taken-one", "active": True}


async def test_a_handle_equal_to_a_single_use_code_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Otherwise a claim of that code would mean two invites; the lookup reads
    referrals first, so the handle could never be reached either."""
    referrer, picker = await users(db_engine, 2)
    referrer_id = await user_id_of(db_engine, referrer)
    await execute(
        db_engine,
        "INSERT INTO referrals (referrer_id, code) VALUES (:r, 'single-use-code')",
        r=referrer_id,
    )

    assert (await pick(api_client, picker, "single-use-code")).status_code == 409


async def test_a_retired_handle_is_held_from_everyone_else(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, stranger = await users(db_engine, 2)
    await pick(api_client, owner, "held-handle")
    await pick(api_client, owner, "next-handle")

    assert (await pick(api_client, stranger, "held-handle")).status_code == 409


async def test_the_owner_may_take_their_retired_handle_back(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    await pick(api_client, owner, "mine-first")
    await pick(api_client, owner, "mine-second")

    response = await pick(api_client, owner, "mine-first")

    assert response.status_code == 200
    assert response.json() == {"code": "mine-first", "active": True}


async def _age_retirement(engine: AsyncEngine, code: str, age: Any) -> None:
    await execute(
        engine,
        "UPDATE invite_link_retired_codes "
        "SET created_at = now() - CAST(:age AS interval) WHERE code = :c",
        age=age,
        c=code,
    )


async def test_after_the_hold_anyone_may_pick_it(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, stranger = await users(db_engine, 2)
    await pick(api_client, owner, "expired-hold")
    await pick(api_client, owner, "moved-on")
    await _age_retirement(db_engine, "expired-hold", RETIRED_CODE_HOLD + RETIRED_CODE_HOLD / 30)

    response = await pick(api_client, stranger, "expired-hold")

    assert response.status_code == 200
    assert response.json() == {"code": "expired-hold", "active": True}


async def test_the_hold_still_applies_just_inside_it(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The edge of the range, not the comfortable middle: a day short of it."""
    owner, stranger = await users(db_engine, 2)
    await pick(api_client, owner, "nearly-free")
    await pick(api_client, owner, "moved-on-2")
    await _age_retirement(db_engine, "nearly-free", RETIRED_CODE_HOLD - RETIRED_CODE_HOLD / 30)

    assert (await pick(api_client, stranger, "nearly-free")).status_code == 409


async def test_changing_the_code_retires_the_old_one(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    random_code = await link_code(api_client, owner)

    await pick(api_client, owner, "fresh-handle")

    assert (
        await scalar(
            db_engine,
            "SELECT count(*) FROM invite_link_retired_codes WHERE code = :c",
            c=random_code,
        )
        == 1
    )


async def test_concurrent_picks_of_one_free_handle_give_one_winner(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    first, second = await users(db_engine, 2)
    await link_code(api_client, first)
    await link_code(api_client, second)

    responses = await asyncio.gather(
        pick(api_client, first, "contested"), pick(api_client, second, "contested")
    )

    assert sorted(r.status_code for r in responses) == [200, 409]


async def test_a_pick_waits_for_a_concurrent_pick_of_the_same_handle(
    db_engine: AsyncEngine,
) -> None:
    """**Deterministic**, at the writer: the second pick starts while the first
    is uncommitted. With the lock it waits and is refused; without it, the
    unique index refuses it as an `IntegrityError` — a 500."""
    first, second = await users(db_engine, 2)
    first_id = await user_id_of(db_engine, first)
    second_id = await user_id_of(db_engine, second)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async with maker() as one, maker() as two:
        await pick_handle(one, first_id, "racing-free")
        racer = asyncio.create_task(pick_handle(two, second_id, "racing-free"))
        await asyncio.sleep(0.3)
        await one.commit()

        with pytest.raises(ConflictError):
            await racer


async def test_a_pick_cannot_slip_past_a_retirement_in_flight(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The owner is moving off a handle while a stranger picks it.

    Before the rename commits the code is still the owner's; after, it is held.
    The stranger's check sees one or the other and is refused either way.
    """
    owner, stranger = await users(db_engine, 2)
    await pick(api_client, owner, "leaving-now")
    await link_code(api_client, stranger)
    owner_id = await user_id_of(db_engine, owner)
    stranger_id = await user_id_of(db_engine, stranger)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async with maker() as one, maker() as two:
        await pick_handle(one, owner_id, "arrived-now")
        racer = asyncio.create_task(pick_handle(two, stranger_id, "leaving-now"))
        await asyncio.sleep(0.3)
        await one.commit()

        with pytest.raises(ConflictError):
            await racer


# --------------------------------------------------------------------------
# Claiming through a personal link
# --------------------------------------------------------------------------


async def test_each_arrival_through_a_link_is_its_own_referral(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, one, two = await users(db_engine, 3, complete=False)
    code = await link_code(api_client, owner)

    first = await claim(api_client, one, code)
    second = await claim(api_client, two, code)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    listed = (await api_client.get(REFERRALS, headers=bearer(api_token(owner)))).json()
    assert {row["id"] for row in listed} == {first.json()["id"], second.json()["id"]}
    for row in listed:
        assert row["invitee_email"] is None
        assert row["signed_up_at"] is not None
        assert row["qualified_at"] is None
        assert row["code"] != code


async def test_the_claim_result_says_nothing_about_the_link_owner(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    code = await link_code(api_client, owner)

    body = (await claim(api_client, arrival, code)).json()

    assert set(body) == {"id", "code", "invited_at", "signed_up_at"}
    assert body["code"] != code
    assert str(await user_id_of(db_engine, owner)) not in str(body)


async def test_your_own_link_cannot_be_claimed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    code = await link_code(api_client, owner)

    assert (await claim(api_client, owner, code)).status_code == 409
    assert await scalar(db_engine, "SELECT count(*) FROM referrals") == 0


async def test_reclaiming_the_same_link_returns_the_same_referral(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    code = await link_code(api_client, owner)

    first = await claim(api_client, arrival, code)
    again = await claim(api_client, arrival, code)

    assert again.status_code == 200
    assert again.json() == first.json()


async def test_somebody_else_s_invite_already_claimed_refuses_the_link(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """One invite per arrival: `uq_referrals_invitee`."""
    owner, other, arrival = await users(db_engine, 3, complete=False)
    single_use = (await invite(api_client, other)).json()["code"]
    await claim(api_client, arrival, single_use)
    code = await link_code(api_client, owner)

    assert (await claim(api_client, arrival, code)).status_code == 409
    owner_id = await user_id_of(db_engine, owner)
    assert (
        await scalar(db_engine, "SELECT count(*) FROM referrals WHERE referrer_id = :u", u=owner_id)
        == 0
    )


async def test_a_single_use_invite_from_the_link_owner_then_the_link_is_idempotent(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Same referrer: the arrival already holds this person's invite."""
    owner, arrival = await users(db_engine, 2, complete=False)
    single_use = await invite(api_client, owner)
    held = await claim(api_client, arrival, single_use.json()["code"])
    code = await link_code(api_client, owner)

    again = await claim(api_client, arrival, code)

    assert again.status_code == 200
    assert again.json() == held.json()


async def test_an_unknown_code_is_not_found(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (arrival,) = await users(db_engine, 1)

    assert (await claim(api_client, arrival, "no-such-code")).status_code == 404


async def test_a_retired_code_cannot_be_claimed_and_the_current_one_can(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, late, prompt = await users(db_engine, 3, complete=False)
    random_code = await link_code(api_client, owner)
    await pick(api_client, owner, "current-handle")

    assert (await claim(api_client, late, random_code)).status_code == 404
    assert (await claim(api_client, prompt, "current-handle")).status_code == 200


async def test_a_deleted_owner_s_link_cannot_be_claimed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    code = await link_code(api_client, owner)
    await execute(db_engine, "UPDATE users SET deleted_at = now() WHERE auth_id = :a", a=owner)

    assert (await claim(api_client, arrival, code)).status_code == 404


async def test_concurrent_claims_of_one_link_by_one_arrival_make_one_referral(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    code = await link_code(api_client, owner)

    responses = await asyncio.gather(*(claim(api_client, arrival, code) for _ in range(3)))

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len({r.json()["id"] for r in responses}) == 1


async def test_a_link_claim_racing_its_own_retry_returns_one_referral(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Deterministic**, at the writer: the retry starts while the first claim
    is uncommitted, so both find nothing held. The retry's insert waits on
    `uq_referrals_invitee`, then `DO NOTHING` reads the winner's row; without
    it, the index refuses the retry as an `IntegrityError` — a 500."""
    owner, arrival = await users(db_engine, 2, complete=False)
    code = await link_code(api_client, owner)
    arrival_id = await user_id_of(db_engine, arrival)
    ladder = credit_ladder(Settings(_env_file=None))  # type: ignore[call-arg]
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async with maker() as one, maker() as two:
        first = await claim_referral(one, arrival_id, code, ladder=ladder, window=WINDOW)
        racer = asyncio.create_task(
            claim_referral(two, arrival_id, code, ladder=ladder, window=WINDOW)
        )
        await asyncio.sleep(0.3)
        await one.commit()
        second = await racer

    assert second.id == first.id


async def test_a_link_claim_racing_a_single_use_claim_never_errors(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Two different referrers at once: one wins, the other is a 409, never a 500."""
    owner, other, arrival = await users(db_engine, 3, complete=False)
    code = await link_code(api_client, owner)
    single_use = (await invite(api_client, other)).json()["code"]

    responses = await asyncio.gather(
        claim(api_client, arrival, code), claim(api_client, arrival, single_use)
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    arrival_id = await user_id_of(db_engine, arrival)
    assert (
        await scalar(
            db_engine, "SELECT count(*) FROM referrals WHERE invitee_user_id = :u", u=arrival_id
        )
        == 1
    )


async def test_the_loser_of_a_race_for_one_single_use_code_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Deterministic**, at the writer: two people claim one invite at once.
    The loser's update matches no row; it must be told the invite is taken,
    not handed the winner's referral with a 200."""
    referrer, winner, loser = await users(db_engine, 3, complete=False)
    code = (await invite(api_client, referrer)).json()["code"]
    winner_id = await user_id_of(db_engine, winner)
    loser_id = await user_id_of(db_engine, loser)
    ladder = credit_ladder(Settings(_env_file=None))  # type: ignore[call-arg]
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async with maker() as one, maker() as two:
        await claim_referral(one, winner_id, code, ladder=ladder, window=WINDOW)
        racer = asyncio.create_task(
            claim_referral(two, loser_id, code, ladder=ladder, window=WINDOW)
        )
        await asyncio.sleep(0.3)
        await one.commit()

        with pytest.raises(ConflictError):
            await racer


async def test_a_deleted_referrer_s_single_use_code_cannot_be_claimed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Claim and the landing lookup agree: a deleted inviter is not found."""
    referrer, arrival = await users(db_engine, 2, complete=False)
    code = (await invite(api_client, referrer)).json()["code"]
    await execute(db_engine, "UPDATE users SET deleted_at = now() WHERE auth_id = :a", a=referrer)

    assert (await claim(api_client, arrival, code)).status_code == 404


async def test_a_handle_resolves_whatever_its_case_and_a_single_use_code_does_not(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A link code is lowercase by rule, so `Ada-Smith` typed or auto-capitalised
    still finds `ada-smith`. A single-use code is mixed-case and stays exact."""
    owner, arrival, other = await users(db_engine, 3, complete=False)
    await _named(db_engine, owner, "Ada", None)
    single_use = (await invite(api_client, other)).json()["code"]
    await pick(api_client, owner, "ada-smith")

    assert (await lookup(api_client, "Ada-Smith")).json()["inviter"]["first_name"] == "Ada"
    assert (await claim(api_client, arrival, "ADA-SMITH")).status_code == 200
    swapped = single_use.swapcase()
    if swapped != single_use:
        assert (await lookup(api_client, swapped)).json() == NOBODY


async def test_two_single_use_claims_racing_never_error(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The latent race this PR closes on the single-use path."""
    one, two, arrival = await users(db_engine, 3, complete=False)
    first = (await invite(api_client, one)).json()["code"]
    second = (await invite(api_client, two)).json()["code"]

    responses = await asyncio.gather(
        claim(api_client, arrival, first), claim(api_client, arrival, second)
    )

    assert sorted(r.status_code for r in responses) == [200, 409]


# --------------------------------------------------------------------------
# Qualification and the once-only unlock
# --------------------------------------------------------------------------


async def test_one_qualifying_arrival_unlocks_the_link_owner(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2)
    code = await link_code(api_client, owner)
    await claim(api_client, arrival, code)

    assert (await complete(api_client, arrival)).status_code in {200, 201}

    owner_id = await user_id_of(db_engine, owner)
    # The bonus is paid now; the unlock waits for an attended session (decision 295).
    assert await unlocks_of(db_engine, owner_id) == 0
    assert (await sources_of(db_engine, owner_id)).count("referral_unlock") == 1


async def test_a_second_qualifying_arrival_grants_nothing_more(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Many arrivals cannot farm the unlock: it is once per referrer, ever."""
    owner, one, two = await users(db_engine, 3)
    code = await link_code(api_client, owner)
    for arrival in (one, two):
        await claim(api_client, arrival, code)
        await complete(api_client, arrival)

    owner_id = await user_id_of(db_engine, owner)
    # The bonus is paid now; the unlock waits for an attended session (decision 295).
    assert await unlocks_of(db_engine, owner_id) == 0
    assert (await sources_of(db_engine, owner_id)).count("referral_unlock") == 1
    qualified = await scalar(
        db_engine,
        "SELECT count(*) FROM referrals WHERE referrer_id = :u AND qualified_at IS NOT NULL",
        u=owner_id,
    )
    assert qualified == 2


async def test_an_arrival_who_finished_first_qualifies_at_the_claim(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2)
    await complete(api_client, arrival)
    code = await link_code(api_client, owner)

    await claim(api_client, arrival, code)

    # The bonus is paid now; the unlock waits for an attended session (decision 295).
    owner_id = await user_id_of(db_engine, owner)
    assert await unlocks_of(db_engine, owner_id) == 0
    assert (await sources_of(db_engine, owner_id)).count("referral_unlock") == 1


# --------------------------------------------------------------------------
# GET /invites/{code} — the public landing lookup
# --------------------------------------------------------------------------


async def _named(engine: AsyncEngine, auth_id: UUID, first: str, avatar: str | None) -> None:
    user_id = await user_id_of(engine, auth_id)
    await execute(
        engine,
        "UPDATE users SET first_name = :f, last_name = 'Secret' WHERE id = :u",
        f=first,
        u=user_id,
    )
    await execute(
        engine, "UPDATE user_profiles SET avatar_url = :a WHERE user_id = :u", a=avatar, u=user_id
    )


async def test_a_current_link_shows_its_owner_s_first_name_and_photo(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    await _named(db_engine, owner, "Ada", "https://cdn.example/ada.png")
    code = await link_code(api_client, owner)

    response = await lookup(api_client, code)

    assert response.status_code == 200
    assert response.json() == {
        "inviter": {"first_name": "Ada", "avatar_url": "https://cdn.example/ada.png"}
    }
    assert response.headers["Cache-Control"] == NO_STORE


async def test_a_picked_handle_resolves(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    await _named(db_engine, owner, "Grace", None)
    await pick(api_client, owner, "grace-h")

    response = await lookup(api_client, "grace-h")

    assert response.json() == {"inviter": {"first_name": "Grace", "avatar_url": None}}


async def test_an_unclaimed_single_use_code_resolves_and_a_claimed_one_does_not(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    referrer, arrival = await users(db_engine, 2, complete=False)
    await _named(db_engine, referrer, "Alan", None)
    code = (await invite(api_client, referrer)).json()["code"]

    assert (await lookup(api_client, code)).json()["inviter"]["first_name"] == "Alan"

    await claim(api_client, arrival, code)

    assert (await lookup(api_client, code)).json() == NOBODY


async def test_a_retired_code_looks_exactly_like_one_that_never_existed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    random_code = await link_code(api_client, owner)
    await pick(api_client, owner, "renamed-one")

    retired = await lookup(api_client, random_code)
    unknown = await lookup(api_client, "never-was-a-code")

    assert retired.status_code == unknown.status_code == 200
    assert retired.content == unknown.content
    assert retired.json() == NOBODY
    assert retired.headers["Cache-Control"] == unknown.headers["Cache-Control"] == NO_STORE
    assert retired.headers["Content-Type"] == unknown.headers["Content-Type"]


async def test_a_deleted_inviter_is_nobody(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    code = await link_code(api_client, owner)
    await execute(db_engine, "UPDATE users SET deleted_at = now() WHERE auth_id = :a", a=owner)

    assert (await lookup(api_client, code)).json() == NOBODY


@pytest.mark.parametrize(
    "code", ["x" * (MAX_CODE_LENGTH + 1), "a.b", "a%20b", "a%2Fb", "has/slash", "é"]
)
async def test_a_malformed_code_is_the_same_nobody(
    api_client: httpx.AsyncClient, code: str
) -> None:
    """Never a 422 or a 404: the landing page has one answer for a bad link."""
    unknown = await lookup(api_client, "never-was")

    response = await lookup(api_client, code)

    assert response.status_code == 200
    assert response.content == unknown.content
    assert response.json() == NOBODY
    assert response.headers["Cache-Control"] == NO_STORE


async def test_the_lookup_ignores_any_token(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Signed in or not, valid or not, the answer is the same."""
    (owner,) = await users(db_engine, 1)
    await _named(db_engine, owner, "Ada", None)
    code = await link_code(api_client, owner)

    anonymous = await lookup(api_client, code)
    signed_in = await lookup(api_client, code, headers=bearer(api_token(owner)))
    garbage = await lookup(api_client, code, headers=bearer("not-a-token"))

    assert anonymous.status_code == signed_in.status_code == garbage.status_code == 200
    assert anonymous.json() == signed_in.json() == garbage.json()


# --------------------------------------------------------------------------
# The default code: {first-name}-{suffix}
# --------------------------------------------------------------------------


async def test_a_new_link_is_named_after_its_owner(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    await _named(db_engine, owner, "Damilola", None)

    code = await link_code(api_client, owner)

    assert re.fullmatch(r"damilola-[a-z0-9]{4}", code)
    assert (await lookup(api_client, code)).json()["inviter"]["first_name"] == "Damilola"


def _scripted(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> list[int]:
    """Make `default_code` answer ``codes`` in turn; returns the suffix lengths
    it was asked for."""
    asked: list[int] = []
    queue = iter(codes)

    def scripted(_first_name: str | None, *, suffix_length: int = 4) -> str:
        asked.append(suffix_length)
        return next(queue)

    monkeypatch.setattr(invite_links, "default_code", scripted)
    return asked


async def test_a_default_code_clashing_with_anything_taken_is_redrawn(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another link's code, a held retired code and a single-use code are each
    passed over; the first free one is used."""
    holder, retirer, referrer, owner = await users(db_engine, 4)
    await pick(api_client, holder, "clash-aaaa")
    await pick(api_client, retirer, "clash-bbbb")
    await pick(api_client, retirer, "moved-away")
    await execute(
        db_engine,
        "INSERT INTO referrals (referrer_id, code) VALUES (:r, 'clash-cccc')",
        r=await user_id_of(db_engine, referrer),
    )
    asked = _scripted(monkeypatch, ["clash-aaaa", "clash-bbbb", "clash-cccc", "free-dddd"])

    response = await create_link(api_client, owner)

    assert response.status_code == 201
    assert response.json()["code"] == "free-dddd"
    assert asked == [4, 4, 4, 4]


async def test_after_five_clashes_the_suffix_grows(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder, owner = await users(db_engine, 2)
    await pick(api_client, holder, "busy-aaaa")
    asked = _scripted(monkeypatch, ["busy-aaaa"] * 5 + ["busy-aaaaa"])

    response = await create_link(api_client, owner)

    assert response.json()["code"] == "busy-aaaaa"
    assert asked == [4, 4, 4, 4, 4, 5]


async def test_running_out_of_default_codes_is_a_conflict_not_an_error(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder, owner = await users(db_engine, 2)
    await pick(api_client, holder, "busy-aaaa")
    _scripted(monkeypatch, ["busy-aaaa"] * 20)

    assert (await create_link(api_client, owner)).status_code == 409


# --------------------------------------------------------------------------
# Revoking and reactivating
# --------------------------------------------------------------------------


async def toggle(client: httpx.AsyncClient, auth_id: UUID, active: bool) -> httpx.Response:
    return await client.patch(LINK, json={"active": active}, headers=bearer(api_token(auth_id)))


async def test_a_revoked_link_cannot_be_claimed_or_looked_up(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    await _named(db_engine, owner, "Ada", None)
    code = await link_code(api_client, owner)

    revoked = await toggle(api_client, owner, False)

    assert revoked.status_code == 200
    assert revoked.json() == {"code": code, "active": False}
    assert (await claim(api_client, arrival, code)).status_code == 404
    looked_up = await lookup(api_client, code)
    assert looked_up.content == (await lookup(api_client, "never-was-a-code")).content
    assert looked_up.json() == NOBODY


async def test_reactivating_makes_the_same_code_work_again(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    await _named(db_engine, owner, "Ada", None)
    code = await link_code(api_client, owner)
    await toggle(api_client, owner, False)

    again = await toggle(api_client, owner, True)

    assert again.json() == {"code": code, "active": True}
    assert (await lookup(api_client, code)).json()["inviter"]["first_name"] == "Ada"
    assert (await claim(api_client, arrival, code)).status_code == 200


async def test_revoking_and_reactivating_are_idempotent(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A second revoke keeps when the link was first revoked."""
    (owner,) = await users(db_engine, 1)
    code = await link_code(api_client, owner)
    owner_id = await user_id_of(db_engine, owner)
    when = "SELECT revoked_at FROM invite_links WHERE user_id = :u"

    await toggle(api_client, owner, False)
    first = await scalar(db_engine, when, u=owner_id)
    second = await toggle(api_client, owner, False)

    assert second.json() == {"code": code, "active": False}
    assert await scalar(db_engine, when, u=owner_id) == first
    assert (await toggle(api_client, owner, True)).json() == {"code": code, "active": True}
    assert (await toggle(api_client, owner, True)).json() == {"code": code, "active": True}


async def test_the_owner_still_reads_a_revoked_link(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)
    code = await link_code(api_client, owner)
    await toggle(api_client, owner, False)

    response = await create_link(api_client, owner)

    assert response.status_code == 200
    assert response.json() == {"code": code, "active": False}


async def test_renaming_a_revoked_link_keeps_it_revoked(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2, complete=False)
    await link_code(api_client, owner)
    await toggle(api_client, owner, False)

    renamed = await pick(api_client, owner, "quiet-rename")

    assert renamed.json() == {"code": "quiet-rename", "active": False}
    assert (await claim(api_client, arrival, "quiet-rename")).status_code == 404
    assert (await lookup(api_client, "quiet-rename")).json() == NOBODY


async def test_a_revoked_code_stays_its_owner_s(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, stranger = await users(db_engine, 2)
    await pick(api_client, owner, "switched-off")
    await toggle(api_client, owner, False)

    assert (await pick(api_client, stranger, "switched-off")).status_code == 409


async def test_an_arrival_before_the_revoke_still_qualifies_and_pays(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, arrival = await users(db_engine, 2)
    code = await link_code(api_client, owner)
    await claim(api_client, arrival, code)
    await toggle(api_client, owner, False)

    await complete(api_client, arrival)

    owner_id = await user_id_of(db_engine, owner)
    # The bonus is paid now; the unlock waits for an attended session (decision 295).
    assert await unlocks_of(db_engine, owner_id) == 0
    assert (await sources_of(db_engine, owner_id)).count("referral_unlock") == 1


async def test_patch_creates_the_link_when_there_is_none(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    (owner,) = await users(db_engine, 1)

    response = await toggle(api_client, owner, False)

    assert response.status_code == 200
    assert re.fullmatch(r"friend-[a-z0-9]{4}", response.json()["code"])
    assert response.json()["active"] is False


@pytest.mark.parametrize("body", [{}, {"active": "maybe"}, {"active": True, "extra": 1}])
async def test_patch_refuses_a_malformed_body(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    (owner,) = await users(db_engine, 1)

    response = await api_client.patch(LINK, json=body, headers=bearer(api_token(owner)))

    assert response.status_code == 422


async def test_patch_needs_a_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.patch(LINK, json={"active": False})).status_code == 401


async def test_a_pick_racing_a_new_link_s_default_code_is_refused_not_an_error(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Deterministic**, at the writer: a new link's default code is inserted
    but not committed when somebody picks the same string. Locked, the pick
    waits and is refused; unlocked, its update hits the unique index — a 500."""
    newcomer, picker = await users(db_engine, 2)
    await link_code(api_client, picker)
    newcomer_id = await user_id_of(db_engine, newcomer)
    picker_id = await user_id_of(db_engine, picker)
    _scripted(monkeypatch, ["drawn-aaaa"])
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async with maker() as one, maker() as two:
        await invite_links.ensure_invite_link(one, newcomer_id)
        racer = asyncio.create_task(pick_handle(two, picker_id, "drawn-aaaa"))
        await asyncio.sleep(0.3)
        await one.commit()

        with pytest.raises(ConflictError):
            await racer


async def test_a_first_pick_creates_the_link_with_the_handle_and_retires_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """No default code is drawn and thrown away: it would be held for a month
    though nobody ever saw it."""
    (owner,) = await users(db_engine, 1)

    response = await pick(api_client, owner, "straight-in")

    assert response.json() == {"code": "straight-in", "active": True}
    assert await scalar(db_engine, "SELECT count(*) FROM invite_link_retired_codes") == 0


async def test_a_first_pick_of_a_taken_handle_creates_nothing(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    holder, owner = await users(db_engine, 2)
    await pick(api_client, holder, "already-mine")

    assert (await pick(api_client, owner, "already-mine")).status_code == 409
    owner_id = await user_id_of(db_engine, owner)
    assert (
        await scalar(db_engine, "SELECT count(*) FROM invite_links WHERE user_id = :u", u=owner_id)
        == 0
    )


async def test_a_code_outside_the_url_safe_alphabet_never_resolves_on_claim(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Claim and lookup share one plausibility rule: the Kelvin sign lowercases
    to `k`, and must not reach `kda-...` through the handle match."""
    owner, arrival = await users(db_engine, 2, complete=False)
    await pick(api_client, owner, "kda-handle")

    assert (await claim(api_client, arrival, "\u212ada-handle")).status_code == 404
    assert (await claim(api_client, arrival, "KDA-HANDLE")).status_code == 200
