"""The owner's read counts each offering's questions (frontend #147).

The session-type list showed each form's size by calling the questions endpoint
once per offering. `question_count` makes it one request, so it must agree with
that endpoint exactly: the same live questions, counted.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body
from tests.integration.test_session_type_windows_approval import own

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def offering_asking(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str, questions: int
) -> tuple[UUID, str, list[str]]:
    _, auth = await as_mentor(engine, tag)
    headers = bearer(api_token(auth))
    type_id = (await client.post(URL, json=body(), headers=headers)).json()["id"]
    ids = [
        (
            await client.post(
                f"{URL}/{type_id}/questions",
                json={"question_text": f"Question {n}?"},
                headers=headers,
            )
        ).json()["id"]
        for n in range(questions)
    ]
    return auth, type_id, ids


async def form_length(client: httpx.AsyncClient, auth: UUID, type_id: str) -> int:
    listed = await client.get(f"{URL}/{type_id}/questions", headers=bearer(api_token(auth)))
    return len(listed.json()["data"])


async def test_every_question_on_the_form_is_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, type_id, _ = await offering_asking(api_client, db_engine, "qcount-three", 3)

    listed = await own(api_client, auth, type_id)
    restored = (
        await api_client.post(f"{URL}/{type_id}/restore", headers=bearer(api_token(auth)))
    ).json()

    assert listed["question_count"] == 3
    assert restored["question_count"] == 3
    assert listed["question_count"] == await form_length(api_client, auth, type_id)


async def test_a_deleted_question_is_not_counted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, type_id, ids = await offering_asking(api_client, db_engine, "qcount-deleted", 3)
    removed = await api_client.delete(
        f"{URL}/{type_id}/questions/{ids[0]}", headers=bearer(api_token(auth))
    )
    assert removed.status_code == 204, removed.text

    listed = await own(api_client, auth, type_id)

    assert listed["question_count"] == 2
    assert listed["question_count"] == await form_length(api_client, auth, type_id)


async def test_an_offering_that_asks_nothing_counts_zero(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, type_id, _ = await offering_asking(api_client, db_engine, "qcount-none", 0)

    assert (await own(api_client, auth, type_id))["question_count"] == 0


async def test_each_offering_counts_only_its_own_questions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth, first, _ = await offering_asking(api_client, db_engine, "qcount-two-types", 2)
    headers = bearer(api_token(auth))
    created = await api_client.post(URL, json=body(name="CV review"), headers=headers)
    second = created.json()["id"]
    await api_client.post(
        f"{URL}/{second}/questions", json={"question_text": "Hi?"}, headers=headers
    )

    assert (await own(api_client, auth, first))["question_count"] == 2
    assert (await own(api_client, auth, second))["question_count"] == 1
