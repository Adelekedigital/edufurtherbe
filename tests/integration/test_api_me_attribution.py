"""`POST /me/acquisition` (PRD 30 PR 2, decision 329).

What carries weight: acquisition is recorded **once** and never for a migrated
account, untrusted values are coerced rather than refused, and an invite is the
one `resolve_code` finds.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import token_for

from conftest import add_user, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ACQUISITION = "/api/v1/me/acquisition"
CONSENT = "/api/v1/me/consents/analytics"


async def a_user(engine: AsyncEngine, **columns: Any) -> UUID:
    async with engine.begin() as conn:
        return await add_user(conn, f"{uuid4()}@example.test", **columns)


async def signed_in(engine: AsyncEngine, **columns: Any) -> tuple[UUID, dict[str, str]]:
    user = await a_user(engine, **columns)
    return user, bearer(await token_for(engine, user))


async def stored(engine: AsyncEngine, user: UUID) -> dict[str, Any]:
    async with engine.begin() as conn:
        row = await conn.execute(
            text("SELECT signup_source, signup_channel, acquired_at FROM users WHERE id = :u"),
            {"u": user},
        )
        return dict(row.one()._mapping)


async def an_invite(engine: AsyncEngine, referrer: UUID) -> str:
    code = f"acq-{uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO referrals (referrer_id, code) VALUES (:r, :c)"),
            {"r": referrer, "c": code},
        )
    return code


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


async def test_the_first_call_records_and_a_later_one_changes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, token = await signed_in(db_engine)

    first = await api_client.post(
        ACQUISITION, json={"source": "campaign", "channel": "linkedin"}, headers=token
    )
    recorded = await stored(db_engine, user)
    later = await api_client.post(
        ACQUISITION, json={"source": "referral_site", "channel": "google"}, headers=token
    )

    assert first.status_code == 201, first.text
    assert first.json() == {"source": "campaign", "channel": "linkedin"}
    assert later.status_code == 200
    assert later.json() == {"source": "campaign", "channel": "linkedin"}
    assert await stored(db_engine, user) == recorded
    assert recorded["acquired_at"] is not None


async def test_a_direct_arrival_is_recorded_once_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, token = await signed_in(db_engine)

    first = await api_client.post(ACQUISITION, json={}, headers=token)
    later = await api_client.post(ACQUISITION, json={"source": "campaign"}, headers=token)

    assert first.status_code == 201
    assert later.status_code == 200
    assert later.json() == {"source": "direct", "channel": None}
    assert (await stored(db_engine, user))["signup_source"] == "direct"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"source": "migrated", "channel": "tiktok"}, {"source": "direct", "channel": "other"}),
        ({"source": "spam" * 100, "channel": 7}, {"source": "direct", "channel": "other"}),
        ({"source": ["x"], "channel": None}, {"source": "direct", "channel": None}),
        (
            {"source": "Campaign ", "channel": "WhatsApp"},
            {"source": "campaign", "channel": "whatsapp"},
        ),
    ],
)
async def test_untrusted_values_are_coerced_never_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, Any], expected: Any
) -> None:
    user, token = await signed_in(db_engine)

    response = await api_client.post(ACQUISITION, json=body, headers=token)

    assert response.status_code == 201, response.text
    assert response.json() == expected
    assert (await stored(db_engine, user))["signup_source"] == expected["source"]


async def test_a_migrated_account_is_never_changed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    user, token = await signed_in(db_engine, legacy_bubble_id=f"bubble-{uuid4()}")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET signup_source = 'migrated' WHERE id = :u"), {"u": user}
        )

    response = await api_client.post(
        ACQUISITION, json={"source": "campaign", "channel": "google"}, headers=token
    )

    assert response.status_code == 200
    assert response.json() == {"source": "migrated", "channel": None}
    assert (await stored(db_engine, user))["acquired_at"] is None


async def test_a_real_invite_code_records_invite_and_the_code_is_not_kept(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    referrer = await a_user(db_engine)
    code = await an_invite(db_engine, referrer)
    user, token = await signed_in(db_engine)

    response = await api_client.post(
        ACQUISITION, json={"source": "campaign", "referral_code": code}, headers=token
    )

    assert response.status_code == 201
    assert response.json()["source"] == "invite"
    async with db_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT row_to_json(u)::text FROM users u WHERE id = :u"), {"u": user}
            )
        ).scalar_one()
        claimed = await conn.scalar(
            text("SELECT invitee_user_id FROM referrals WHERE code = :c"), {"c": code}
        )
    assert code not in row
    assert claimed is None, "recording an arrival is not claiming the invite"


@pytest.mark.parametrize("which", ["unknown", "own", "deleted owner"])
async def test_a_code_naming_no_live_invite_of_somebody_else_is_not_an_invite(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, which: str
) -> None:
    user, token = await signed_in(db_engine)
    if which == "unknown":
        code = "no-such-code"
    elif which == "own":
        code = await an_invite(db_engine, user)
    else:
        gone = await a_user(db_engine)
        code = await an_invite(db_engine, gone)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": gone}
            )

    response = await api_client.post(
        ACQUISITION, json={"source": "campaign", "referral_code": code}, headers=token
    )

    assert response.status_code == 201
    assert response.json()["source"] == "campaign"


async def test_acquisition_needs_a_sign_in(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.post(ACQUISITION, json={})).status_code == 401
