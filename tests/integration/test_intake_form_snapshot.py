"""The form as it stood at booking, unanswered questions included.

A mentor reading a booking's answers could not tell "they skipped this" from
"I never asked this": only answers were stored, never which questions were on
the form. The design draws every question, with "No answer" under the blanks
(owner, 2026-10-10). So booking keeps a copy of the form beside the answers, and
the answers read returns every question in the order asked, each saying whether
it was answered. A booking made before the copy existed reads as it always did.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_bookable_offering
from tests.integration.test_booking_answers import add_question, book
from tests.integration.test_intake_files import signed_in

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def read_answers(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: Any, session_id: str
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/sessions/{session_id}/answers", headers=await signed_in(engine, mentor)
    )
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


async def a_three_question_form(engine: AsyncEngine, tag: str) -> dict[str, Any]:
    mentor, session_type = await a_bookable_offering(engine, tag)
    goals, _ = await add_question(engine, session_type, "Your goals?", required=True, order=0)
    deadline, _ = await add_question(engine, session_type, "Your deadline?", order=1)
    extra, _ = await add_question(engine, session_type, "Anything else?", order=2)
    return {
        "mentor": mentor,
        "session_type": session_type,
        "goals": goals,
        "deadline": deadline,
        "extra": extra,
    }


async def test_a_skipped_question_is_read_back_marked_unanswered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Every question, in the order asked**, so a blank reads as "No answer"
    rather than as a question that was never there."""
    form = await a_three_question_form(db_engine, "snap-skip")
    booked = await book(
        api_client,
        db_engine,
        "snap-skip",
        form["mentor"],
        form["session_type"],
        [
            {"question_id": str(form["goals"]), "text": "An MSc"},
            {"question_id": str(form["extra"]), "text": "No"},
        ],
    )
    assert booked.status_code == 201, booked.text

    answers = await read_answers(api_client, db_engine, form["mentor"], booked.json()["id"])

    assert [(a["question_text"], a["answered"], a["required"]) for a in answers] == [
        ("Your goals?", True, True),
        ("Your deadline?", False, False),
        ("Anything else?", True, False),
    ]
    skipped = answers[1]
    assert skipped["question_id"] == str(form["deadline"])
    assert skipped["question_type"] == "free_text"
    assert skipped["text"] is None and skipped["options"] == [] and skipped["file"] is None


async def test_a_form_left_wholly_blank_is_still_recorded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**An all-optional form skipped entirely** wrote nothing before, so the
    mentor saw no form at all. Now every question reads back unanswered."""
    mentor, session_type = await a_bookable_offering(db_engine, "snap-blank")
    await add_question(db_engine, session_type, "Anything else?")

    booked = await book(api_client, db_engine, "snap-blank", mentor, session_type, None)
    assert booked.status_code == 201, booked.text

    answers = await read_answers(api_client, db_engine, mentor, booked.json()["id"])

    assert [(a["question_text"], a["answered"]) for a in answers] == [("Anything else?", False)]


async def test_the_form_reads_as_it_was_asked_not_as_it_is_now(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**A question added later was never asked**, and a skipped question
    reworded later keeps the words it was asked with."""
    form = await a_three_question_form(db_engine, "snap-later")
    booked = await book(
        api_client,
        db_engine,
        "snap-later",
        form["mentor"],
        form["session_type"],
        [{"question_id": str(form["goals"]), "text": "An MSc"}],
    )
    assert booked.status_code == 201, booked.text
    await add_question(db_engine, form["session_type"], "Asked only later?", order=3)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_type_questions SET question_text = 'Reworded' WHERE id = :q"),
            {"q": form["deadline"]},
        )

    answers = await read_answers(api_client, db_engine, form["mentor"], booked.json()["id"])

    assert [a["question_text"] for a in answers] == [
        "Your goals?",
        "Your deadline?",
        "Anything else?",
    ]


async def test_a_booking_from_before_the_copy_reads_as_it_always_did(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Answered-only, and no guessing**, for bookings made before the form was
    kept: merging in today's form could show questions that were never asked.
    `required` is unknown there, so null."""
    form = await a_three_question_form(db_engine, "snap-legacy")
    booked = await book(
        api_client,
        db_engine,
        "snap-legacy",
        form["mentor"],
        form["session_type"],
        [{"question_id": str(form["goals"]), "text": "An MSc"}],
    )
    assert booked.status_code == 201, booked.text
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM intake_form_questions WHERE submission_id IN "
                "(SELECT id FROM intake_submissions WHERE session_id = :s)"
            ),
            {"s": booked.json()["id"]},
        )

    answers = await read_answers(api_client, db_engine, form["mentor"], booked.json()["id"])

    assert [(a["question_text"], a["answered"], a["required"]) for a in answers] == [
        ("Your goals?", True, None)
    ]


async def test_the_preview_still_counts_answers_only(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The card's preview is "2 answers, the first of them", not the form."""
    form = await a_three_question_form(db_engine, "snap-preview")
    booked = await book(
        api_client,
        db_engine,
        "snap-preview",
        form["mentor"],
        form["session_type"],
        [{"question_id": str(form["extra"]), "text": "No"}],
    )
    assert booked.status_code == 201, booked.text

    session = await api_client.get(
        f"/api/v1/sessions/{booked.json()['id']}",
        headers=await signed_in(db_engine, form["mentor"]),
    )

    assert session.json()["answers_preview"]["count"] == 1
    assert session.json()["answers_preview"]["first"]["question_text"] == "Anything else?"
