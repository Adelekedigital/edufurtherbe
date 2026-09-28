"""Choice questions: a mentor writes the options, single or multiple (#200).

Session Types frontend #12, owner-approved 2026-09-28 (options written by the
mentor; not "options from my offerings"). `multi_choice` is the package's one
choice type — "choose from options" — and `allows_multiple` says whether one or
several may be picked, so single choice needs no second vocabulary value.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type
from tests.integration.test_api_me_intake import as_mentor, url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def choice(*options: str, many: bool = False, **overrides: object) -> dict[str, object]:
    return {
        "question_text": "Which stage are you at?",
        "question_type": "multi_choice",
        "allows_multiple": many,
        "options": [{"text": o} for o in options],
    } | overrides


async def setup(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    mentor, auth_id = await as_mentor(engine, tag)
    return await add_session_type(engine, mentor), bearer(api_token(auth_id))


async def listed(client: httpx.AsyncClient, session_type: UUID, headers: dict[str, str]) -> list:
    return (await client.get(url(session_type), headers=headers)).json()["data"]


async def test_a_single_choice_question_reads_back_with_its_options_in_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, headers = await setup(db_engine, "choice-single")

    created = await api_client.post(
        url(session_type), json=choice("Exploring", "Drafting", "Submitted"), headers=headers
    )
    (question,) = await listed(api_client, session_type, headers)

    assert created.status_code == 201, created.text
    assert question["question_type"] == "multi_choice"
    assert question["allows_multiple"] is False
    assert [o["text"] for o in question["options"]] == ["Exploring", "Drafting", "Submitted"]
    assert all(o["id"] for o in question["options"])


async def test_a_multiple_choice_question_says_so(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, headers = await setup(db_engine, "choice-many")

    await api_client.post(url(session_type), json=choice("CV", "Essay", many=True), headers=headers)
    (question,) = await listed(api_client, session_type, headers)

    assert question["allows_multiple"] is True


async def test_other_questions_carry_no_options(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, headers = await setup(db_engine, "choice-text")

    await api_client.post(
        url(session_type), json={"question_text": "Anything else?"}, headers=headers
    )
    (question,) = await listed(api_client, session_type, headers)

    assert question["options"] == []
    assert question["allows_multiple"] is False


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (choice("Only one"), "fewer than two options"),
        (choice(*[f"Option {n}" for n in range(11)]), "more than ten options"),
        (choice("Same", "same"), "a repeated option, ignoring case"),
        (choice("Fine", "   "), "an empty option"),
        (
            {
                "question_text": "Upload?",
                "question_type": "file_upload",
                "options": [{"text": "a"}, {"text": "b"}],
            },
            "options on a non-choice question",
        ),
        (
            {"question_text": "Say?", "allows_multiple": True},
            "allows_multiple on a non-choice question",
        ),
    ],
)
async def test_a_malformed_choice_question_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, payload: dict, why: str
) -> None:
    session_type, headers = await setup(db_engine, f"choice-bad-{uuid4().hex[:6]}")

    response = await api_client.post(url(session_type), json=payload, headers=headers)

    assert response.status_code == 422, why


async def test_the_atomic_create_takes_choice_questions_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, auth_id = await as_mentor(db_engine, "choice-atomic")
    headers = bearer(api_token(auth_id))

    created = await api_client.post(
        "/api/v1/me/session-types",
        json={"name": "Stage check", "duration_minutes": 30, "questions": [choice("A", "B")]},
        headers=headers,
    )
    (question,) = await listed(api_client, created.json()["id"], headers)

    assert created.status_code == 201, created.text
    assert [o["text"] for o in question["options"]] == ["A", "B"]


async def test_patching_options_keeps_renames_adds_and_removes(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Replace-all: an `id` keeps that option (its text and place may change),
    no `id` adds one, and one left out is removed."""
    session_type, headers = await setup(db_engine, "choice-patch")
    await api_client.post(url(session_type), json=choice("Keep", "Rename", "Drop"), headers=headers)
    (before,) = await listed(api_client, session_type, headers)
    keep, rename, _ = before["options"]

    response = await api_client.patch(
        f"{url(session_type)}/{before['id']}",
        json={
            "options": [
                {"id": rename["id"], "text": "Renamed"},
                {"text": "New"},
                {"id": keep["id"], "text": "Keep"},
            ],
            "allows_multiple": True,
        },
        headers=headers,
    )
    (after,) = await listed(api_client, session_type, headers)

    assert response.status_code == 200, response.text
    assert [o["text"] for o in after["options"]] == ["Renamed", "New", "Keep"]
    assert after["options"][0]["id"] == rename["id"]
    assert after["options"][2]["id"] == keep["id"]
    assert after["allows_multiple"] is True


async def test_an_option_id_from_another_question_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, headers = await setup(db_engine, "choice-foreign")
    await api_client.post(url(session_type), json=choice("A", "B"), headers=headers)
    await api_client.post(
        url(session_type), json=choice("C", "D", question_text="Other?"), headers=headers
    )
    first, second = await listed(api_client, session_type, headers)

    response = await api_client.patch(
        f"{url(session_type)}/{first['id']}",
        json={"options": [{"id": second["options"][0]["id"], "text": "C"}, {"text": "E"}]},
        headers=headers,
    )

    assert response.status_code == 422
    (unchanged, _) = await listed(api_client, session_type, headers)
    assert [o["text"] for o in unchanged["options"]] == ["A", "B"]


async def test_an_answered_option_cannot_be_removed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Once a mentee has picked an option, removing it would leave an answer
    that points at nothing: a `409`, and the option stays."""
    session_type, headers = await setup(db_engine, "choice-answered")
    await api_client.post(url(session_type), json=choice("Picked", "Other"), headers=headers)
    (question,) = await listed(api_client, session_type, headers)
    picked = question["options"][0]["id"]
    async with db_engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES (:e, 'mentee', 'UTC') RETURNING id"
                ),
                {"e": f"picker-{uuid4().hex[:6]}@example.test"},
            )
        ).scalar_one()
        mentor = (
            await conn.execute(
                text("SELECT mentor_user_id FROM session_types WHERE id = :t"), {"t": session_type}
            )
        ).scalar_one()
        booked = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, session_type_id, starts_at, "
                    "duration_minutes, status) VALUES (:m, :a, :t, now() + interval '3 days', "
                    "45, 'confirmed') RETURNING id"
                ),
                {"m": mentor, "a": mentee, "t": session_type},
            )
        ).scalar_one()
        submission = (
            await conn.execute(
                text(
                    "INSERT INTO intake_submissions (session_id, mentee_id, status) "
                    "VALUES (:s, :a, 'submitted') RETURNING id"
                ),
                {"s": booked, "a": mentee},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO intake_answers (submission_id, question_id, selected_option_id) "
                "VALUES (:s, :q, :o)"
            ),
            {"s": submission, "q": question["id"], "o": picked},
        )

    response = await api_client.patch(
        f"{url(session_type)}/{question['id']}",
        json={"options": [{"text": "Other"}, {"text": "Third"}]},
        headers=headers,
    )

    assert response.status_code == 409
    (unchanged,) = await listed(api_client, session_type, headers)
    assert picked in [o["id"] for o in unchanged["options"]]


async def test_switching_between_choice_and_other_types_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A choice question's answers mean nothing as prose, and the reverse: the
    type change is refused both ways; delete and re-create instead."""
    session_type, headers = await setup(db_engine, "choice-switch")
    await api_client.post(url(session_type), json=choice("A", "B"), headers=headers)
    await api_client.post(url(session_type), json={"question_text": "Prose?"}, headers=headers)
    choice_q, prose_q = await listed(api_client, session_type, headers)

    to_prose = await api_client.patch(
        f"{url(session_type)}/{choice_q['id']}",
        json={"question_type": "free_text"},
        headers=headers,
    )
    to_choice = await api_client.patch(
        f"{url(session_type)}/{prose_q['id']}",
        json={"question_type": "multi_choice", "options": [{"text": "A"}, {"text": "B"}]},
        headers=headers,
    )

    assert to_prose.status_code == 422
    assert to_choice.status_code == 422


async def test_a_non_choice_question_cannot_be_given_options_by_patch(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, headers = await setup(db_engine, "choice-patch-text")
    await api_client.post(url(session_type), json={"question_text": "Prose?"}, headers=headers)
    (question,) = await listed(api_client, session_type, headers)

    response = await api_client.patch(
        f"{url(session_type)}/{question['id']}",
        json={"options": [{"text": "A"}, {"text": "B"}]},
        headers=headers,
    )

    assert response.status_code == 422


async def test_another_mentors_question_options_are_not_found(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    session_type, headers = await setup(db_engine, "choice-mine")
    await api_client.post(url(session_type), json=choice("A", "B"), headers=headers)
    (question,) = await listed(api_client, session_type, headers)
    _, stranger = await setup(db_engine, "choice-stranger")

    response = await api_client.patch(
        f"{url(session_type)}/{question['id']}",
        json={"options": [{"text": "X"}, {"text": "Y"}]},
        headers=stranger,
    )

    assert response.status_code == 404
