"""Creating a session type with its questions at once, and safely twice.

Session Types frontend #1 and #2 (owner-approved 2026-09-28). The create wizard
collects the type and its intake questions together: `questions[]` (at most
`MAX_QUESTIONS`) on `POST /me/session-types` lands in the same transaction as the
type, so a failure leaves no half-built form. And `Idempotency-Key` — optional
here, unlike booking — makes a double-click on "Publish" one type, not two.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_me_session_type_writes import URL, as_mentor, body

from app.domain.intake import MAX_QUESTIONS
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def question(n: int, **overrides: object) -> dict[str, object]:
    return {"question_text": f"Question {n}?", "is_required": n == 0, "display_order": n} | (
        overrides
    )


async def counts(engine: AsyncEngine, mentor: UUID) -> tuple[int, int]:
    async with engine.begin() as conn:
        types = (
            await conn.execute(
                text("SELECT count(*) FROM session_types WHERE mentor_user_id = :u"), {"u": mentor}
            )
        ).scalar_one()
        questions = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM session_type_questions q "
                    "JOIN session_types t ON t.id = q.session_type_id WHERE t.mentor_user_id = :u"
                ),
                {"u": mentor},
            )
        ).scalar_one()
    return int(types), int(questions)


async def test_questions_are_created_with_the_type(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "atomic-ok")

    response = await api_client.post(
        URL, json=body(questions=[question(0), question(1)]), headers=bearer(api_token(auth))
    )

    assert response.status_code == 201
    created = response.json()
    assert len(created["question_ids"]) == 2
    listed = (
        await api_client.get(f"{URL}/{created['id']}/questions", headers=bearer(api_token(auth)))
    ).json()["data"]
    assert [q["question_text"] for q in listed] == ["Question 0?", "Question 1?"]
    assert await counts(db_engine, mentor) == (1, 2)


async def test_no_questions_is_still_a_plain_create(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "atomic-none")

    response = await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))

    assert response.status_code == 201
    assert response.json()["question_ids"] == []


async def test_a_bad_question_refuses_the_whole_create(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One invalid question and nothing is written — not the type without it."""
    mentor, auth = await as_mentor(db_engine, "atomic-bad")

    response = await api_client.post(
        URL,
        json=body(questions=[question(0), question(1, question_type="multi_choice")]),
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422
    assert await counts(db_engine, mentor) == (0, 0)


async def test_more_than_the_limit_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "atomic-many")

    response = await api_client.post(
        URL,
        json=body(questions=[question(n) for n in range(MAX_QUESTIONS + 1)]),
        headers=bearer(api_token(auth)),
    )

    assert response.status_code == 422
    assert await counts(db_engine, mentor) == (0, 0)


async def test_a_retried_create_with_the_same_key_makes_one_type(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "idem-create")
    headers = bearer(api_token(auth)) | {"Idempotency-Key": "publish-1"}
    payload = body(questions=[question(0)])

    first = await api_client.post(URL, json=payload, headers=headers)
    second = await api_client.post(URL, json=payload, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert (
        "Idempotent-Replayed" not in first.headers or first.headers["Idempotent-Replayed"] != "true"
    )
    assert await counts(db_engine, mentor) == (1, 1)


async def test_the_same_key_with_a_different_body_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth = await as_mentor(db_engine, "idem-mismatch")
    headers = bearer(api_token(auth)) | {"Idempotency-Key": "publish-2"}
    await api_client.post(URL, json=body(name="One"), headers=headers)

    response = await api_client.post(URL, json=body(name="Two"), headers=headers)

    assert response.status_code == 422


async def test_without_a_key_two_creates_are_two_requests(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Optional, so nothing changes for a client that sends none: the second
    identical create is a name clash, exactly as before."""
    _, auth = await as_mentor(db_engine, "idem-none")
    headers = bearer(api_token(auth))

    await api_client.post(URL, json=body(), headers=headers)
    second = await api_client.post(URL, json=body(), headers=headers)

    assert second.status_code == 409


async def test_a_retried_question_with_the_same_key_is_added_once(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await as_mentor(db_engine, "idem-question")
    created = (await api_client.post(URL, json=body(), headers=bearer(api_token(auth)))).json()
    headers = bearer(api_token(auth)) | {"Idempotency-Key": "question-1"}
    path = f"{URL}/{created['id']}/questions"

    first = await api_client.post(path, json=question(0), headers=headers)
    second = await api_client.post(path, json=question(0), headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert await counts(db_engine, mentor) == (1, 1)


async def test_a_question_key_is_not_replayed_onto_another_type(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same key and body sent to a different offering is a different request."""
    _, auth = await as_mentor(db_engine, "idem-scope")
    headers = bearer(api_token(auth))
    one = (await api_client.post(URL, json=body(name="One"), headers=headers)).json()["id"]
    two = (await api_client.post(URL, json=body(name="Two"), headers=headers)).json()["id"]
    keyed = headers | {"Idempotency-Key": "question-shared"}

    await api_client.post(f"{URL}/{one}/questions", json=question(0), headers=keyed)
    response = await api_client.post(f"{URL}/{two}/questions", json=question(0), headers=keyed)

    assert response.status_code == 422


async def test_one_mentors_key_is_not_another_mentors(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth_a = await as_mentor(db_engine, "idem-a")
    mentor_b, auth_b = await as_mentor(db_engine, "idem-b")
    key = {"Idempotency-Key": f"shared-{uuid4().hex[:6]}"}

    await api_client.post(URL, json=body(), headers=bearer(api_token(auth_a)) | key)
    response = await api_client.post(URL, json=body(), headers=bearer(api_token(auth_b)) | key)

    assert response.status_code == 201
    assert "Idempotent-Replayed" not in response.headers
    assert (await counts(db_engine, mentor_b))[0] == 1
