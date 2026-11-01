"""What a purge leaves of a person in invite links and referrals (decision 305, #470).

A purged account keeps its `invite_links` row, which referrals made through it
point at, but none of the handles it carried: the current code becomes a random
one and the retired codes go, so a handle (often a real name) is neither stored
nor reserved. An invite the purged person arrived through keeps its row and
loses the address it was sent to.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_account_purge import DAY, FakeAdmin, FakeStorage, deleted, purge
from tests.integration.test_api_invite_links import execute, pick, scalar, user_id_of, users

from app.domain.referrals import CODE_LENGTH

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def link_code_of(engine: AsyncEngine, user_id: UUID) -> str:
    return str(await scalar(engine, "SELECT code FROM invite_links WHERE user_id = :u", u=user_id))


async def retired_of(engine: AsyncEngine, user_id: UUID) -> list[str]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.code FROM invite_link_retired_codes r "
                "JOIN invite_links l ON l.id = r.invite_link_id "
                "WHERE l.user_id = :u ORDER BY r.code"
            ),
            {"u": user_id},
        )
        return [row.code for row in rows]


async def purged(engine: AsyncEngine, user_id: UUID) -> None:
    await deleted(engine, user_id, purge_in=-DAY)
    counts = await purge(engine, FakeAdmin(), FakeStorage())
    assert counts == {"due": 1, "purged": 1, "failed": 0}


async def test_a_purged_owner_s_handles_are_dropped_and_free_to_pick(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, stranger, another = await users(db_engine, 3)
    await pick(api_client, owner, "ada-before")
    await pick(api_client, owner, "ada-now")
    owner_id = await user_id_of(db_engine, owner)
    assert await retired_of(db_engine, owner_id) == ["ada-before"]

    await purged(db_engine, owner_id)

    code = await link_code_of(db_engine, owner_id)
    # Random and opaque, as long as a single-use code: not the name, not a handle.
    assert code not in {"ada-before", "ada-now"}
    assert len(code) >= CODE_LENGTH
    assert await retired_of(db_engine, owner_id) == []
    # The current handle is free at once, and so is one retired inside the hold.
    now = await pick(api_client, stranger, "ada-now")
    assert now.status_code == 200
    assert now.json() == {"code": "ada-now", "active": True}
    before = await pick(api_client, another, "ada-before")
    assert before.status_code == 200
    assert before.json() == {"code": "ada-before", "active": True}


async def test_other_people_s_links_and_held_handles_are_untouched(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    owner, bystander, stranger = await users(db_engine, 3)
    await pick(api_client, owner, "purged-handle")
    await pick(api_client, bystander, "kept-before")
    await pick(api_client, bystander, "kept-now")
    bystander_id = await user_id_of(db_engine, bystander)

    await purged(db_engine, await user_id_of(db_engine, owner))

    assert await link_code_of(db_engine, bystander_id) == "kept-now"
    assert await retired_of(db_engine, bystander_id) == ["kept-before"]
    assert (await pick(api_client, stranger, "kept-before")).status_code == 409


async def test_a_purged_invitee_s_address_leaves_their_referral(
    db_engine: AsyncEngine,
) -> None:
    """The invite stays (the referrer's count and unlock rest on it); the
    address it was sent to, which is the purged person's, goes."""
    referrer, invitee, other_invitee = await users(db_engine, 3)
    referrer_id = await user_id_of(db_engine, referrer)
    invitee_id = await user_id_of(db_engine, invitee)
    other_id = await user_id_of(db_engine, other_invitee)
    await execute(
        db_engine,
        "INSERT INTO referrals (referrer_id, code, invitee_email, invitee_user_id, signed_up_at) "
        "VALUES (:r, 'purge-claimed', 'invitee@example.test', :i, now()), "
        "(:r, 'purge-open', 'someone@example.test', NULL, NULL), "
        "(:r, 'purge-other', 'other@example.test', :o, now())",
        r=referrer_id,
        i=invitee_id,
        o=other_id,
    )

    await purged(db_engine, invitee_id)

    claimed = await scalar(
        db_engine,
        "SELECT invitee_email FROM referrals WHERE code = 'purge-claimed' AND invitee_user_id = :i",
        i=invitee_id,
    )
    assert claimed is None
    other = await scalar(db_engine, "SELECT invitee_email FROM referrals WHERE code = 'purge-open'")
    assert other == "someone@example.test"
    # Another arrival's invite keeps its address: only the purged person's goes.
    kept = await scalar(
        db_engine,
        "SELECT invitee_email FROM referrals WHERE code = 'purge-other' AND invitee_user_id = :o",
        o=other_id,
    )
    assert kept == "other@example.test"
