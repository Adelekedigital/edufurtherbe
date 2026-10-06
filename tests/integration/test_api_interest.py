"""Asking to be told when something ships (`/me/interest`, #365).

**The authorization case is the load-bearing one here.** Everything else is a
small CRUD surface; the thing that would actually hurt is one account reading or
withdrawing another's, because the list says what a person is waiting for and
the key is chosen by whoever pressed the button.

**Not mentor-gated**, deliberately: Explore's no-mentors state is a signed-in
mentee's, so the tests use plain accounts rather than mentors.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_mentor_status_log import make_user

from app.domain.interest import MAX_FEATURES_PER_ACCOUNT
from conftest import PROBLEM_JSON, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/interest"


async def an_account(engine: AsyncEngine, tag: str) -> tuple[UUID, str]:
    auth_id = uuid4()
    user = await make_user(engine, auth_id, f"{tag}@example.com")
    return user, api_token(auth_id)


async def rows_for(engine: AsyncEngine, user: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT feature, created_at, notified_at FROM feature_interest "
                "WHERE user_id = :u ORDER BY created_at, id"
            ),
            {"u": user},
        )
        return [dict(row) for row in result.mappings()]


# --------------------------------------------------------------------------
# Registering, and reading it back
# --------------------------------------------------------------------------


async def test_registering_then_reading_it_back(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The whole point: the button has to still say *we'll let you know*."""
    user, token = await an_account(db_engine, "asks-for-payments")

    asked = await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))

    assert asked.status_code == 204
    shown = await api_client.get(URL, headers=bearer(token))
    assert shown.status_code == 200
    body = shown.json()
    assert [entry["feature"] for entry in body["data"]] == ["payments"]
    assert body["next_cursor"] is None
    assert body["data"][0]["registered_at"] is not None
    assert [row["feature"] for row in await rows_for(db_engine, user)] == ["payments"]


async def test_an_account_that_has_asked_for_nothing_reads_an_empty_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**200 with no entries, never 404.**

    A `404` would make *asked for nothing* and *no such person* the same answer,
    which is exactly what a client cannot act on — and the frontend keys its
    button off a `200`, so a `404` here would hide the control from everybody
    who had not already pressed it.
    """
    _, token = await an_account(db_engine, "asks-for-nothing")

    shown = await api_client.get(URL, headers=bearer(token))

    assert shown.status_code == 200
    assert shown.json() == {"data": [], "next_cursor": None}


async def test_pressing_twice_is_pressing_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two rows would mean two emails for one request.

    Watched to fail by dropping `on_conflict_do_nothing`: the second press then
    raises the unique violation as a `500`, on a button a client is free to
    press again.
    """
    user, token = await an_account(db_engine, "presses-twice-api")

    first = await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))
    second = await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))

    assert (first.status_code, second.status_code) == (204, 204)
    assert len(await rows_for(db_engine, user)) == 1


async def test_a_repeat_press_does_not_move_when_they_asked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`registered_at` is when they *first* asked.

    It is documented that way, so copy may say "you asked on the 6th" — and an
    `ON CONFLICT DO UPDATE` would quietly make it "you asked just now" for
    somebody who asked nothing new.
    """
    user, token = await an_account(db_engine, "asks-again-later")
    await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))
    first_time = (await rows_for(db_engine, user))[0]["created_at"]

    await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))

    assert (await rows_for(db_engine, user))[0]["created_at"] == first_time


async def test_several_features_come_back_in_the_order_they_were_asked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await an_account(db_engine, "asks-for-two")

    for feature in ("payments", "new_mentors"):
        assert (
            await api_client.post(URL, json={"feature": feature}, headers=bearer(token))
        ).status_code == 204

    shown = await api_client.get(URL, headers=bearer(token))
    assert [entry["feature"] for entry in shown.json()["data"]] == ["payments", "new_mentors"]


# --------------------------------------------------------------------------
# Who may see what
# --------------------------------------------------------------------------


async def test_one_account_cannot_read_another_s_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The assertion this file exists for.**

    `own_interests` filters by `user_id` in the `WHERE`, so a non-owner matches
    no rows rather than matching rows that are then hidden — the difference
    being that the second shape leaks the moment somebody adds a column.
    """
    _, their_token = await an_account(db_engine, "owns-a-list")
    _, my_token = await an_account(db_engine, "owns-nothing")
    await api_client.post(URL, json={"feature": "payments"}, headers=bearer(their_token))

    mine = await api_client.get(URL, headers=bearer(my_token))

    assert mine.status_code == 200
    assert mine.json() == {"data": [], "next_cursor": None}


async def test_one_account_cannot_withdraw_another_s_registration(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A `404`, and **their row survives** — which is the half a status code
    cannot tell you. A delete scoped only by `feature` would answer `204` here
    and remove somebody else's registration."""
    theirs, their_token = await an_account(db_engine, "keeps-its-list")
    _, my_token = await an_account(db_engine, "tries-to-withdraw")
    await api_client.post(URL, json={"feature": "payments"}, headers=bearer(their_token))

    attempt = await api_client.delete(f"{URL}/payments", headers=bearer(my_token))

    assert attempt.status_code == 404
    assert [row["feature"] for row in await rows_for(db_engine, theirs)] == ["payments"]


@pytest.mark.parametrize("method", ["get", "post", "delete"])
async def test_a_guest_cannot_use_any_of_it(api_client: httpx.AsyncClient, method: str) -> None:
    """Nobody can be notified without an account, so nobody may register one."""
    call = getattr(api_client, method)
    answered = await (
        call(URL, json={"feature": "payments"})
        if method == "post"
        else call(f"{URL}/payments")
        if method == "delete"
        else call(URL)
    )

    assert answered.status_code == 401
    assert answered.headers["content-type"].startswith(PROBLEM_JSON)


# --------------------------------------------------------------------------
# What a key may be
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "feature"),
    [
        ("empty", ""),
        ("one-char", "a"),
        ("over-length", "a" * 41),
        ("leading-digit", "1payments"),
        ("hyphen", "pay-ments"),
        ("inner-space", "pay ments"),
        ("padded", " payments "),
        # `null` reached the column as the string "none" until the validator
        # moved to `mode="after"`: `str(None)` satisfies the slug pattern and the
        # `CHECK`, so an uninitialised client prop would have been stored as a
        # real interest and answered `204`.
        ("json-null", None),
        ("json-true", True),
        ("json-number", 12345),
        ("json-list", ["payments"]),
    ],
)
async def test_a_malformed_key_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, case: str, feature: object
) -> None:
    """**Whitespace included**, which is the deliberate part: a padded key is a
    client bug, and accepting it would both hide the bug and collide two
    spellings on one row."""
    user, token = await an_account(db_engine, f"bad-key-{case}")

    refused = await api_client.post(URL, json={"feature": feature}, headers=bearer(token))

    assert refused.status_code == 422
    assert refused.headers["content-type"].startswith(PROBLEM_JSON)
    assert await rows_for(db_engine, user) == []


async def test_case_is_folded_rather_than_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`Payments` and `payments` are one key written two ways.

    Folded, so one intention cannot become two rows — unlike whitespace, which
    is refused above. The two are different kinds of difference, and this pair
    of tests is where that claim is actually held to.
    """
    user, token = await an_account(db_engine, "shouts-the-key")

    assert (
        await api_client.post(URL, json={"feature": "PAYMENTS"}, headers=bearer(token))
    ).status_code == 204

    assert [row["feature"] for row in await rows_for(db_engine, user)] == ["payments"]
    # And so the second spelling is the same registration, not a new one.
    await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))
    assert len(await rows_for(db_engine, user)) == 1


async def test_a_key_naming_nothing_that_exists_is_accepted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The point of an open vocabulary**, and the cost of it in one test.

    A well-formed key for a feature nobody has built is a button shipping before
    its backend, which is the thing this endpoint exists to allow. The same
    behaviour accepts a typo nobody will ever be notified against — accepted
    deliberately, and detectable by reporting distinct keys rather than
    prevented by an enum.
    """
    _, token = await an_account(db_engine, "asks-for-the-unbuilt")

    asked = await api_client.post(
        URL, json={"feature": "a_feature_nobody_has_built"}, headers=bearer(token)
    )

    assert asked.status_code == 204


async def test_an_unknown_field_in_the_body_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`extra="forbid"`, so a client misspelling `feature` learns now rather
    than wondering why its button never confirms."""
    _, token = await an_account(db_engine, "sends-a-typo-field")

    refused = await api_client.post(
        URL, json={"feature": "payments", "featuer": "payments"}, headers=bearer(token)
    )

    assert refused.status_code == 422


# --------------------------------------------------------------------------
# Withdrawing, and the cap
# --------------------------------------------------------------------------


async def test_withdrawing_removes_it_and_withdrawing_again_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The second `404` is the rule calendar disconnect follows: somebody who
    believes they turned something off needs to know if they did not."""
    user, token = await an_account(db_engine, "withdraws")
    await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))

    gone = await api_client.delete(f"{URL}/payments", headers=bearer(token))
    again = await api_client.delete(f"{URL}/payments", headers=bearer(token))

    assert (gone.status_code, again.status_code) == (204, 404)
    assert await rows_for(db_engine, user) == []
    assert (await api_client.get(URL, headers=bearer(token))).json()["data"] == []


async def test_withdrawing_something_never_asked_for_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, token = await an_account(db_engine, "withdraws-nothing")

    assert (await api_client.delete(f"{URL}/payments", headers=bearer(token))).status_code == 404


async def test_withdrawing_a_key_that_could_never_be_stored_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**404, not 422.** The key is absent, which is the truthful answer; a
    `422` would tell a client its spelling is wrong when the fact is that it is
    not waiting for that."""
    _, token = await an_account(db_engine, "withdraws-a-bad-key")

    assert (await api_client.delete(f"{URL}/NOT-A-SLUG", headers=bearer(token))).status_code == 404


async def test_withdrawing_only_removes_the_one_named(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The accepting case for the delete's scoping. Without it, a delete keyed
    on `user_id` alone would pass every test above."""
    user, token = await an_account(db_engine, "withdraws-one-of-two")
    for feature in ("payments", "new_mentors"):
        await api_client.post(URL, json={"feature": feature}, headers=bearer(token))

    assert (await api_client.delete(f"{URL}/payments", headers=bearer(token))).status_code == 204

    assert [row["feature"] for row in await rows_for(db_engine, user)] == ["new_mentors"]


async def test_the_cap_refuses_one_feature_past_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A `409`: the key was well formed and the request legal, there is just no
    room. Nobody reaches this by using the product.

    Watched to fail by removing the count check — the account then grows a row
    per distinct key for as long as anything keeps posting.
    """
    user, token = await an_account(db_engine, "hits-the-cap")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO feature_interest (user_id, feature) "
                "SELECT :u, 'f_' || generate_series(1, :n)"
            ),
            {"u": user, "n": MAX_FEATURES_PER_ACCOUNT},
        )

    refused = await api_client.post(URL, json={"feature": "payments"}, headers=bearer(token))

    assert refused.status_code == 409
    assert len(await rows_for(db_engine, user)) == MAX_FEATURES_PER_ACCOUNT


async def test_at_the_cap_a_repeat_press_still_works(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The reason the cap is checked after the insert, not before.**

    Checking first would refuse a repeat press from an account already at the
    cap — a button that worked yesterday and errors today, for somebody asking
    for nothing new. Inserting first makes the repeat a no-op that returns
    before the count is ever taken.
    """
    user, token = await an_account(db_engine, "repeats-at-the-cap")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO feature_interest (user_id, feature) "
                "SELECT :u, 'f_' || generate_series(1, :n)"
            ),
            {"u": user, "n": MAX_FEATURES_PER_ACCOUNT},
        )

    again = await api_client.post(URL, json={"feature": "f_1"}, headers=bearer(token))

    assert again.status_code == 204
    assert len(await rows_for(db_engine, user)) == MAX_FEATURES_PER_ACCOUNT
