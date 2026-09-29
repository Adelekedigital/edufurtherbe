"""The delete refusal says why and how many; questions reorder in one request.

Session Types frontend #4 and #8 (owner go-ahead 2026-09-28).

#4: a delete refused because sessions are still live on the offering was a
`409` typed `/problems/session-type-has-bookings`. Round 4 (#218) replaced the
refusal with a scheduled deletion — see
`test_session_type_featured_and_scheduled_deletion.py`.

#8: `PUT .../questions/order` takes the whole id list and renumbers the form in
one transaction, instead of one `PATCH` per moved question.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type
from tests.integration.test_api_me_session_type_delete import URL, as_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_other_conflicts_carry_no_booked_count(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The member belongs to this refusal only — a name clash stays untyped."""
    _, auth = await as_mentor(db_engine, "typed-other")
    headers = bearer(api_token(auth))
    body = {"name": "Same", "duration_minutes": 45}
    await api_client.post(URL, json=body, headers=headers)

    clash = await api_client.post(URL, json=body, headers=headers)

    assert clash.status_code == 409
    assert "booked_count" not in clash.json()


# --------------------------------------------------------------------------
# #8 — reorder
# --------------------------------------------------------------------------


def order_url(session_type: object) -> str:
    return f"{URL}/{session_type}/questions/order"


async def form(client: httpx.AsyncClient, session_type: object, auth: UUID) -> list[str]:
    rows = (
        await client.get(f"{URL}/{session_type}/questions", headers=bearer(api_token(auth)))
    ).json()["data"]
    return [row["id"] for row in rows]


async def a_form(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str, n: int = 3
) -> tuple[UUID, UUID, list[str]]:
    mentor, auth = await as_mentor(engine, tag)
    session_type = await add_session_type(engine, mentor, name=f"Form {tag}")
    for index in range(n):
        await client.post(
            f"{URL}/{session_type}/questions",
            # Spaced, so a renumber to 0, 1, 2 is visible rather than a no-op.
            json={"question_text": f"Q{index}?", "display_order": index * 10},
            headers=bearer(api_token(auth)),
        )
    return session_type, auth, await form(client, session_type, auth)


async def test_the_form_takes_the_order_given(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, auth, ids = await a_form(api_client, db_engine, "reorder")
    wanted = [ids[2], ids[0], ids[1]]

    response = await api_client.put(
        order_url(session_type), json={"question_ids": wanted}, headers=bearer(api_token(auth))
    )

    assert response.status_code == 204
    assert await form(api_client, session_type, auth) == wanted


async def test_the_order_is_renumbered_from_zero(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, auth, ids = await a_form(api_client, db_engine, "renumber")

    await api_client.put(
        order_url(session_type),
        json={"question_ids": list(reversed(ids))},
        headers=bearer(api_token(auth)),
    )

    async with db_engine.begin() as conn:
        orders = (
            await conn.execute(
                text(
                    "SELECT display_order FROM session_type_questions "
                    "WHERE session_type_id = :t AND deleted_at IS NULL ORDER BY display_order"
                ),
                {"t": session_type},
            )
        ).scalars()
        assert list(orders) == [0, 1, 2]


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate"])
async def test_anything_but_the_exact_form_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, change: str
) -> None:
    session_type, auth, ids = await a_form(api_client, db_engine, f"bad-{change}")
    sent = {
        "missing": ids[:2],
        "extra": [*ids, str(uuid4())],
        "duplicate": [ids[0], ids[0], ids[1], ids[2]],
    }[change]

    response = await api_client.put(
        order_url(session_type), json={"question_ids": sent}, headers=bearer(api_token(auth))
    )

    assert response.status_code == 422
    assert await form(api_client, session_type, auth) == ids


async def test_another_offerings_questions_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, auth, ids = await a_form(api_client, db_engine, "cross-a")
    _, _, theirs = await a_form(api_client, db_engine, "cross-b")

    response = await api_client.put(
        order_url(session_type),
        json={"question_ids": [*ids[:2], theirs[0]]},
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422


async def test_another_mentors_offering_is_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Scoped in the query: the offering is found through its owner or not at all."""
    session_type, _, ids = await a_form(api_client, db_engine, "owner-a")
    _, stranger = await as_mentor(db_engine, "owner-b")

    response = await api_client.put(
        order_url(session_type),
        json={"question_ids": list(reversed(ids))},
        headers=bearer(api_token(stranger)),
    )

    assert response.status_code == 404
