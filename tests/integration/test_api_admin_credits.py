"""An admin putting credits into somebody's balance.

**The tests that carry weight are the refusals and the replay.** This is the one
path that creates credits without anything automatic deciding, so what stops it
being abused or double-fired is the whole of its safety: a caller with no grant
must not learn the endpoint exists, and a retried request must not credit twice.

The second subject is partial resolution. An admin correcting six people should
not lose five because one id was stale — so an unresolved id is a *result*, not
an error, and it comes back by name.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.domain.credits import credit_ladder
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CREDITS = "/api/v1/admin/credits"


async def a_user(
    engine: AsyncEngine,
    auth_id: UUID | None = None,
    *,
    role: str | None = None,
    revoked: bool = False,
    deleted: bool = False,
) -> UUID:
    tag = uuid4()
    async with engine.begin() as conn:
        user_id = UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO users (email, auth_id, first_name, primary_role, "
                            "timezone, deleted_at) VALUES (:e, :a, 'Ada', 'mentee', 'UTC', :d) "
                            "RETURNING id"
                        ),
                        {
                            "e": f"{tag}@example.com",
                            "a": auth_id,
                            "d": dt.datetime(2026, 1, 1, tzinfo=dt.UTC) if deleted else None,
                        },
                    )
                ).scalar_one()
            )
        )
        if role:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
                    "VALUES (:u, :r, now())"
                    if revoked
                    else "INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"
                ),
                {"u": user_id, "r": role},
            )
    return user_id


async def grant(
    client: httpx.AsyncClient,
    auth_id: UUID,
    user_ids: list[UUID],
    *,
    quantity: int = 2,
    note: str | None = None,
    key: str = "grant-1",
) -> httpx.Response:
    body: dict[str, Any] = {"user_ids": [str(u) for u in user_ids], "quantity": quantity}
    if note is not None:
        body["note"] = note
    return await client.post(
        CREDITS, json=body, headers={**bearer(api_token(auth_id)), "Idempotency-Key": key}
    )


async def lots_of(engine: AsyncEngine, user_id: UUID) -> list[tuple[str, int, Any]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT source, quantity_remaining, expires_at FROM credit_lots "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return [(r[0], r[1], r[2]) for r in rows]


async def authorisations_for(engine: AsyncEngine, user_id: UUID) -> list[tuple[UUID, str | None]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT granted_by, note FROM admin_credit_grants WHERE user_id = :u "
                "ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return [(r[0], r[1]) for r in rows]


# --------------------------------------------------------------------------
# Who may call it
# --------------------------------------------------------------------------


async def test_any_live_admin_grant_may_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Deliberately wider than moderation's `super_admin` gate.** Crediting
    somebody is support work that whoever is on shift has to be able to do."""
    auth = uuid4()
    admin = await a_user(db_engine, auth, role="limited_access")
    target = await a_user(db_engine)

    response = await grant(api_client, auth, [target])

    assert response.status_code == 200
    source, remaining, _ = (await lots_of(db_engine, target))[0]
    assert (source, remaining) == ("admin_grant", 2)
    assert [row[0] for row in await authorisations_for(db_engine, target)] == [admin]


async def test_a_caller_with_no_grant_gets_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """404, not 403. A 403 would tell somebody without a grant that the endpoint
    is real and that others may call it — which is the one thing an unprivileged
    caller should not learn from asking."""
    auth = uuid4()
    await a_user(db_engine, auth)
    target = await a_user(db_engine)

    assert (await grant(api_client, auth, [target])).status_code == 404
    assert await lots_of(db_engine, target) == []


async def test_a_revoked_grant_cannot_credit(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A grant that was taken away is not a grant. Asserted separately from the
    no-grant case because the row still exists — only `revoked_at` distinguishes
    them, and a predicate that forgets it looks correct."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin", revoked=True)
    target = await a_user(db_engine)

    assert (await grant(api_client, auth, [target])).status_code == 404
    assert await lots_of(db_engine, target) == []


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    response = await api_client.post(
        CREDITS,
        json={"user_ids": [str(uuid4())], "quantity": 1},
        headers={"Idempotency-Key": "no-token"},
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# What it writes
# --------------------------------------------------------------------------


async def test_the_credit_expires_like_every_other_grant(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Not `NON_EXPIRING`.** Only the starter never expires, and that is
    because it exists to give somebody a first taste of the platform rather than
    because grants are permanent. A never-expiring admin credit would also be
    invisible to the expiry sweep forever."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)

    await grant(api_client, auth, [target])

    (_, _, expires_at) = (await lots_of(db_engine, target))[0]
    assert expires_at is not None


async def test_the_ledger_explains_the_lot(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A balance that rose with nothing saying why is the counter behaviour D8
    chose a ledger to prevent — admin grants included."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)

    await grant(api_client, auth, [target], quantity=3)

    async with db_engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT delta, reason FROM credit_transactions WHERE user_id = :u"),
            {"u": target},
        )
        assert [(r[0], r[1]) for r in rows] == [(3, "grant")]


async def test_the_note_is_optional_and_stored_when_given(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Optional because an admin may credit people who never complained —
    goodwill after an outage reaches a cohort, not a queue of requests."""
    auth = uuid4()
    admin = await a_user(db_engine, auth, role="super_admin")
    with_note = await a_user(db_engine)
    without = await a_user(db_engine)

    await grant(api_client, auth, [with_note], note="outage on the 3rd", key="k1")
    await grant(api_client, auth, [without], key="k2")

    assert await authorisations_for(db_engine, with_note) == [(admin, "outage on the 3rd")]
    assert await authorisations_for(db_engine, without) == [(admin, None)]


async def test_every_lot_is_authorised_by_somebody(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The invariant the table exists for.** An `admin_grant` lot with no row
    in `admin_credit_grants` is credits nobody can be asked about."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    targets = [await a_user(db_engine) for _ in range(3)]

    await grant(api_client, auth, targets)

    async with db_engine.begin() as conn:
        orphans = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM credit_lots l WHERE l.source = 'admin_grant' "
                    "AND NOT EXISTS (SELECT 1 FROM admin_credit_grants a "
                    "WHERE a.credit_lot_id = l.id)"
                )
            )
        ).scalar_one()

    assert orphans == 0


# --------------------------------------------------------------------------
# Bulk, and what it does with what it cannot find
# --------------------------------------------------------------------------


async def test_several_users_are_credited_in_one_call(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The real case. An outage costs a cohort, not a person."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    targets = [await a_user(db_engine) for _ in range(3)]

    response = await grant(api_client, auth, targets)

    assert response.status_code == 200
    assert sorted(response.json()["granted"]) == sorted(str(t) for t in targets)
    for target in targets:
        assert (await lots_of(db_engine, target))[0][1] == 2


async def test_an_unknown_id_does_not_lose_the_others(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**A result, not an error.** An admin correcting six people should not lose
    five because one id was stale — and the sixth comes back by name, which is
    the only form they can act on."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    real = await a_user(db_engine)
    ghost = uuid4()

    response = await grant(api_client, auth, [real, ghost])

    assert response.status_code == 200
    assert response.json()["granted"] == [str(real)]
    assert response.json()["unresolved"] == [str(ghost)]
    assert (await lots_of(db_engine, real))[0][1] == 2


async def test_a_deleted_account_is_unresolved_rather_than_credited(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The foreign key would accept them; `LIVE` is the only thing that would
    not. Crediting a deleted account is credits nobody can spend."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    gone = await a_user(db_engine, deleted=True)

    response = await grant(api_client, auth, [gone])

    assert response.json()["unresolved"] == [str(gone)]
    assert await lots_of(db_engine, gone) == []


async def test_a_repeated_id_is_credited_once(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Harmless rather than refused: a caller who pasted a list with a duplicate
    meant one grant, and the response says how many landed."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)

    response = await grant(api_client, auth, [target, target])

    assert response.json()["granted"] == [str(target)]
    assert len(await lots_of(db_engine, target)) == 1


# --------------------------------------------------------------------------
# The cap, and the retry
# --------------------------------------------------------------------------


async def test_an_over_cap_quantity_is_refused_not_clamped(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Refused, because an admin who typed a larger number meant it.** Quietly
    granting three would leave them believing the larger number went in — and
    they would have no reason to look."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)

    response = await grant(api_client, auth, [target], quantity=credit_ladder().monthly + 1)

    assert response.status_code == 422
    assert await lots_of(db_engine, target) == []


async def test_the_cap_is_the_configured_monthly_grant(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Follows the ladder rather than a literal.** Raising the monthly grant
    raises what an admin may hand out in one action, and the two cannot drift."""
    monkeypatch.setenv("CREDIT_MONTHLY_ALLOWANCE", "9")
    get_settings.cache_clear()
    try:
        auth = uuid4()
        await a_user(db_engine, auth, role="super_admin")
        target = await a_user(db_engine)

        response = await grant(api_client, auth, [target], quantity=9)

        assert response.status_code == 200
        assert (await lots_of(db_engine, target))[0][1] == 9
    finally:
        get_settings.cache_clear()


async def test_a_replayed_key_grants_once(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The one a double-click would otherwise cost real credits.**

    An admin cannot notice and undo a double grant — the credits are spendable
    the moment they land — which is why the header is required rather than
    recommended, following the booking path's argument.
    """
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)

    first = await grant(api_client, auth, [target], key="same")
    second = await grant(api_client, auth, [target], key="same")

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.headers["Idempotent-Replay"] == "false"
    assert second.headers["Idempotent-Replay"] == "true"
    assert len(await lots_of(db_engine, target)) == 1


async def test_a_different_body_under_the_same_key_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Replaying the first answer for a different request would tell the admin
    their second grant landed when it did not."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)
    await grant(api_client, auth, [target], quantity=1, key="reused")

    response = await grant(api_client, auth, [target], quantity=3, key="reused")

    assert response.status_code == 422
    assert len(await lots_of(db_engine, target)) == 1


async def test_the_key_is_required(db_engine: AsyncEngine, api_client: httpx.AsyncClient) -> None:
    """Required rather than optional, so the guarantee is not opt-in for exactly
    the caller who most needs it."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)

    response = await api_client.post(
        CREDITS,
        json={"user_ids": [str(target)], "quantity": 1},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422
    assert await lots_of(db_engine, target) == []


# --------------------------------------------------------------------------
# The history — the record the write path exists to leave
# --------------------------------------------------------------------------


async def history(client: httpx.AsyncClient, auth_id: UUID, **params: Any) -> dict[str, Any]:
    response = await client.get(CREDITS, params=params, headers=bearer(api_token(auth_id)))
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_a_grant_appears_in_the_history_with_both_parties_named(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """`granted_by` is the whole point of the table; the recipient's name is what
    makes a page of ids readable at all."""
    auth = uuid4()
    admin = await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)
    await grant(api_client, auth, [target], quantity=2, note="outage on the 3rd")

    row = (await history(api_client, auth))["data"][0]

    assert row["user_id"] == str(target)
    assert row["granted_by"] == str(admin)
    assert row["note"] == "outage on the 3rd"
    assert row["recipient_name"] and row["granted_by_name"]


async def test_the_history_shows_granted_and_remaining_separately(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The two answer different questions.** A history showing only the amount
    granted cannot say whether a goodwill gesture reached anybody, or whether it
    expired unspent — which is the outcome most worth knowing."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)
    await grant(api_client, auth, [target], quantity=3)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credit_lots SET quantity_remaining = 1 "
                "WHERE user_id = :u AND source = 'admin_grant'"
            ),
            {"u": target},
        )

    row = (await history(api_client, auth))["data"][0]

    assert (row["quantity_granted"], row["quantity_remaining"]) == (3, 1)


async def test_every_admin_sees_every_grant(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**An audit only one person can read is not an audit.** The second admin
    never granted anything and must still see what the first did."""
    first_auth, second_auth = uuid4(), uuid4()
    await a_user(db_engine, first_auth, role="super_admin")
    await a_user(db_engine, second_auth, role="limited_access")
    target = await a_user(db_engine)
    await grant(api_client, first_auth, [target])

    assert len((await history(api_client, second_auth))["data"]) == 1


async def test_granted_by_narrows_to_one_admin(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """*What did I hand out* — the second question this table gets asked, and the
    one somebody asks about themselves."""
    first_auth, second_auth = uuid4(), uuid4()
    first = await a_user(db_engine, first_auth, role="super_admin")
    await a_user(db_engine, second_auth, role="super_admin")
    a, b = await a_user(db_engine), await a_user(db_engine)
    await grant(api_client, first_auth, [a], key="k1")
    await grant(api_client, second_auth, [b], key="k2")

    mine = await history(api_client, first_auth, granted_by=str(first))

    assert [row["granted_by"] for row in mine["data"]] == [str(first)]


async def test_a_recipient_who_closed_their_account_is_still_listed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**Exactly what an audit is for.** Filtering `LIVE` would quietly shorten
    the history rather than answer the question asked of it."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    target = await a_user(db_engine)
    await grant(api_client, auth, [target])
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": target})

    assert [row["user_id"] for row in (await history(api_client, auth))["data"]] == [str(target)]


async def test_the_history_pages_and_does_not_repeat_a_row(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**A bulk grant writes every row in one transaction**, so they share a
    `created_at` to the microsecond. Keyed on that alone the cursor would skip
    or repeat — which is why the keyset is `(created_at, id)`."""
    auth = uuid4()
    await a_user(db_engine, auth, role="super_admin")
    targets = [await a_user(db_engine) for _ in range(5)]
    await grant(api_client, auth, targets)

    first = await history(api_client, auth, limit=2)
    second = await history(api_client, auth, limit=2, cursor=first["next_cursor"])
    third = await history(api_client, auth, limit=2, cursor=second["next_cursor"])

    seen = [row["id"] for page in (first, second, third) for row in page["data"]]
    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_a_caller_with_no_grant_cannot_read_the_history(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    auth = uuid4()
    await a_user(db_engine, auth)

    response = await api_client.get(CREDITS, headers=bearer(api_token(auth)))

    assert response.status_code == 404
