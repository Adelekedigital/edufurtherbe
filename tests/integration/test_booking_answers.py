"""A mentee sees an offering's intake questions and answers them when booking.

Booking answers resumed on 2026-09-28 (owner): the questions ride on the public
`SessionTypeRead`, and `POST /sessions` takes `answers[]` for text and choice
questions, validated against the offering's live form and saved in the booking's
own transaction. File answers are the next PR; a required file question is not
enforced until then (settled decision #207).

**A required question is enforced only where `require_intake_answers` is on** —
off by default until the frontend's questions step ships (#283). The tests that
need it on build their own app with `enforcing_client`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import build_api_app
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, first_slot, key

from app.core.config import Settings
from app.infra.storage.supabase import SupabaseStorage

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def add_question(
    engine: AsyncEngine,
    session_type: UUID,
    question_text: str,
    *,
    question_type: str = "free_text",
    required: bool = False,
    order: int = 0,
    allows_multiple: bool = False,
    options: tuple[str, ...] = (),
    deleted: bool = False,
) -> tuple[UUID, list[UUID]]:
    async with engine.begin() as conn:
        question = (
            await conn.execute(
                text(
                    "INSERT INTO session_type_questions (session_type_id, question_text, "
                    " question_type, is_required, display_order, allows_multiple, deleted_at) "
                    "VALUES (:t, :q, :k, :r, :o, :m, CASE WHEN :d THEN now() END) RETURNING id"
                ),
                {
                    "t": session_type,
                    "q": question_text,
                    "k": question_type,
                    "r": required,
                    "o": order,
                    "m": allows_multiple,
                    "d": deleted,
                },
            )
        ).scalar_one()
        option_ids = []
        for position, option_text in enumerate(options):
            option_ids.append(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO session_type_question_options "
                            "(question_id, option_text, sort_order) VALUES (:q, :t, :p) "
                            "RETURNING id"
                        ),
                        {"q": question, "t": option_text, "p": position},
                    )
                ).scalar_one()
            )
    return question, option_ids


async def stored_answers(engine: AsyncEngine, mentor: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT a.question_id, a.answer_text, a.selected_option_id, s.status, "
                "       s.mentee_id, s.submitted_at IS NOT NULL AS submitted "
                "FROM intake_answers a "
                "JOIN intake_submissions s ON s.id = a.submission_id "
                "JOIN sessions x ON x.id = s.session_id WHERE x.mentor_id = :m "
                "ORDER BY a.question_id, a.selected_option_id"
            ),
            {"m": mentor},
        )
        return [dict(row._mapping) for row in rows]


async def submissions(engine: AsyncEngine, mentor: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM intake_submissions s "
                        "JOIN sessions x ON x.id = s.session_id WHERE x.mentor_id = :m"
                    ),
                    {"m": mentor},
                )
            ).scalar_one()
        )


async def book(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tag: str,
    mentor: UUID,
    session_type: UUID,
    answers: list[dict[str, Any]] | None,
) -> httpx.Response:
    _, headers = await a_mentee(engine, tag)
    payload = body(session_type, await first_slot(client, mentor, session_type))
    if answers is not None:
        payload["answers"] = answers
    return await client.post("/api/v1/sessions", json=payload, headers=headers | key())


# --------------------------------------------------------------------------
# The questions, on the public read
# --------------------------------------------------------------------------


async def test_the_public_session_types_carry_the_live_form(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "form-public")
    second, _ = await add_question(db_engine, session_type, "Which programme?", order=1)
    first, options = await add_question(
        db_engine,
        session_type,
        "Which stage?",
        question_type="multi_choice",
        required=True,
        options=("Drafting", "Submitted"),
    )
    await add_question(db_engine, session_type, "Retired?", deleted=True)

    (offering,) = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert [q["id"] for q in offering["questions"]] == [str(first), str(second)]
    choice = offering["questions"][0]
    assert choice["question_type"] == "multi_choice"
    assert choice["is_required"] is True
    assert choice["allows_multiple"] is False
    assert [o["id"] for o in choice["options"]] == [str(o) for o in options]


async def test_the_profile_session_types_carry_the_form_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "form-profile")
    await add_question(db_engine, session_type, "Which programme?")

    profile = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()

    (offering,) = profile["session_types"]
    assert [q["question_text"] for q in offering["questions"]] == ["Which programme?"]


async def test_an_offering_with_no_form_has_no_questions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _ = await a_bookable_offering(db_engine, "form-none")

    (offering,) = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert offering["questions"] == []


# --------------------------------------------------------------------------
# Answers, on booking
# --------------------------------------------------------------------------


async def test_text_and_choice_answers_are_saved_with_the_booking(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ans-ok")
    prose, _ = await add_question(db_engine, session_type, "Which programme?", required=True)
    single, (drafting, _submitted) = await add_question(
        db_engine,
        session_type,
        "Which stage?",
        question_type="multi_choice",
        options=("Drafting", "Submitted"),
    )
    multi, (ielts, toefl, _gre) = await add_question(
        db_engine,
        session_type,
        "Which tests?",
        question_type="multi_choice",
        allows_multiple=True,
        options=("IELTS", "TOEFL", "GRE"),
    )

    response = await book(
        api_client,
        db_engine,
        "ans-ok",
        mentor,
        session_type,
        [
            {"question_id": str(prose), "text": "  MSc Public Policy  "},
            {"question_id": str(single), "option_ids": [str(drafting)]},
            {"question_id": str(multi), "option_ids": [str(ielts), str(toefl)]},
        ],
    )

    assert response.status_code == 201, response.text
    stored = await stored_answers(db_engine, mentor)
    assert await submissions(db_engine, mentor) == 1
    assert {row["status"] for row in stored} == {"submitted"}
    assert all(row["submitted"] for row in stored)
    by_question: dict[UUID, list[dict[str, Any]]] = {}
    for row in stored:
        by_question.setdefault(row["question_id"], []).append(row)
    assert [r["answer_text"] for r in by_question[prose]] == ["MSc Public Policy"]
    assert [r["selected_option_id"] for r in by_question[single]] == [drafting]
    assert {r["selected_option_id"] for r in by_question[multi]} == {ielts, toefl}


async def test_no_answers_writes_no_submission(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ans-none")
    await add_question(db_engine, session_type, "Anything else?")

    response = await book(api_client, db_engine, "ans-none", mentor, session_type, None)

    assert response.status_code == 201
    assert await submissions(db_engine, mentor) == 0


@pytest_asyncio.fixture
async def enforcing_client(
    db_engine: AsyncEngine, api_storage: SupabaseStorage | None
) -> AsyncIterator[httpx.AsyncClient]:
    """`api_client`, on an app where required questions are enforced."""
    app = build_api_app(
        db_engine, api_storage, Settings(_env_file=None, require_intake_answers=True)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_a_missing_required_answer_books_while_enforcement_is_off(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The default, and what the live frontend relies on** (#283): it sends no
    answers yet, so a required question must not refuse the booking."""
    mentor, session_type = await a_bookable_offering(db_engine, "ans-off")
    await add_question(db_engine, session_type, "Which programme?", required=True)

    response = await book(api_client, db_engine, "ans-off", mentor, session_type, None)

    assert response.status_code == 201, response.text
    assert await submissions(db_engine, mentor) == 0


async def test_a_missing_required_answer_is_refused_by_name(
    enforcing_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ans-missing")
    required, _ = await add_question(db_engine, session_type, "Which programme?", required=True)

    response = await book(enforcing_client, db_engine, "ans-missing", mentor, session_type, [])

    assert response.status_code == 422
    problem = response.json()
    assert str(required) in problem["detail"]
    assert any(e["pointer"] == "/answers" for e in problem["errors"])
    assert await submissions(db_engine, mentor) == 0


@pytest.mark.parametrize(
    ("case", "pointer"),
    [
        ("unknown_question", "/answers/0/question_id"),
        ("duplicate_question", "/answers/1/question_id"),
        ("text_for_choice", "/answers/0"),
        ("options_for_text", "/answers/0"),
        ("two_for_single", "/answers/0/option_ids"),
        ("foreign_option", "/answers/0/option_ids"),
        ("option_twice", "/answers/0/option_ids"),
        ("both_forms", "/answers/0"),
        ("file_question", "/answers/0"),
    ],
)
async def test_a_wrong_answer_is_refused_with_its_pointer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, case: str, pointer: str
) -> None:
    tag = f"ans-{case.replace('_', '-')}"
    mentor, session_type = await a_bookable_offering(db_engine, tag)
    prose, _ = await add_question(db_engine, session_type, "Which programme?")
    single, (a, b) = await add_question(
        db_engine, session_type, "Stage?", question_type="multi_choice", options=("A", "B")
    )
    upload, _ = await add_question(db_engine, session_type, "CV?", question_type="file_upload")
    several, (c, _d) = await add_question(
        db_engine,
        session_type,
        "Tests taken?",
        question_type="multi_choice",
        allows_multiple=True,
        options=("C", "D"),
    )
    # An option of a question on another offering: never valid here.
    _, other_type = await a_bookable_offering(db_engine, f"{tag}-other")
    _, (foreign, _x) = await add_question(
        db_engine, other_type, "Other?", question_type="multi_choice", options=("X", "Y")
    )
    answers = {
        "unknown_question": [{"question_id": str(uuid4()), "text": "x"}],
        "duplicate_question": [
            {"question_id": str(prose), "text": "x"},
            {"question_id": str(prose), "text": "y"},
        ],
        "text_for_choice": [{"question_id": str(single), "text": "A"}],
        "options_for_text": [{"question_id": str(prose), "option_ids": [str(a)]}],
        "two_for_single": [{"question_id": str(single), "option_ids": [str(a), str(b)]}],
        "foreign_option": [{"question_id": str(single), "option_ids": [str(foreign)]}],
        "option_twice": [{"question_id": str(several), "option_ids": [str(c), str(c)]}],
        "both_forms": [{"question_id": str(single), "text": "A", "option_ids": [str(a)]}],
        "file_question": [{"question_id": str(upload), "text": "my cv"}],
    }[case]

    response = await book(api_client, db_engine, tag, mentor, session_type, answers)

    assert response.status_code == 422, response.text
    assert pointer in [e["pointer"] for e in response.json()["errors"]]
    assert await submissions(db_engine, mentor) == 0


async def test_a_retired_question_cannot_be_answered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ans-retired")
    retired, _ = await add_question(db_engine, session_type, "Old?", deleted=True)

    response = await book(
        api_client,
        db_engine,
        "ans-retired",
        mentor,
        session_type,
        [{"question_id": str(retired), "text": "x"}],
    )

    assert response.status_code == 422


async def test_a_required_file_question_does_not_block_booking_yet(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """File answers are the next PR; until then a required file question must
    not make the offering unbookable (#207)."""
    mentor, session_type = await a_bookable_offering(db_engine, "ans-file-required")
    await add_question(
        db_engine, session_type, "Upload your CV", question_type="file_upload", required=True
    )

    response = await book(api_client, db_engine, "ans-file-required", mentor, session_type, [])

    assert response.status_code == 201


async def test_answers_are_part_of_the_idempotency_fingerprint(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A retry with different answers under the same key is a different request."""
    mentor, session_type = await a_bookable_offering(db_engine, "ans-idem")
    prose, _ = await add_question(db_engine, session_type, "Which programme?")
    _, headers = await a_mentee(db_engine, "ans-idem")
    payload = body(session_type, await first_slot(api_client, mentor, session_type))
    same_key = headers | key("answers-key")

    first = await api_client.post(
        "/api/v1/sessions",
        json=payload | {"answers": [{"question_id": str(prose), "text": "One"}]},
        headers=same_key,
    )
    second = await api_client.post(
        "/api/v1/sessions",
        json=payload | {"answers": [{"question_id": str(prose), "text": "Two"}]},
        headers=same_key,
    )

    assert first.status_code == 201
    assert second.status_code == 422
