"""An answer keeps the words it answered (#350).

A mentor may reword a question, or an option, after a booking. The answer and
its preview still show the wording the mentee saw; an answer recorded before
that copy was kept falls back to the current wording.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_session_answers import answers, booked_with_answers
from tests.integration.test_intake_files import BUCKET, intake_client, intake_settings, signed_in

from conftest import FakeStorage

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest_asyncio.fixture
async def files_client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async for client in intake_client(db_engine, FakeStorage(bucket=BUCKET), intake_settings()):
        yield client


async def reword(engine: AsyncEngine, booking: dict[str, Any]) -> None:
    """The mentor rewrites the first question and the IELTS option after the booking."""
    prose, _choice, _cv = booking["questions"]
    ielts, _toefl = booking["options"]
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_type_questions SET question_text = :t WHERE id = :q"),
            {"t": "Which degree are you applying for?", "q": prose},
        )
        await conn.execute(
            text("UPDATE session_type_question_options SET option_text = :t WHERE id = :o"),
            {"t": "IELTS Academic", "o": ielts},
        )


async def read(client: httpx.AsyncClient, engine: AsyncEngine, booking: dict[str, Any]) -> Any:
    headers = await signed_in(engine, booking["mentor"])
    listed = await answers(client, booking["session_id"], headers)
    assert listed.status_code == 200, listed.text
    session = await client.get(f"/api/v1/sessions/{booking['session_id']}", headers=headers)
    assert session.status_code == 200, session.text
    return listed.json()["data"], session.json()["answers_preview"]


async def test_a_reworded_question_and_option_still_read_as_answered(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "aw-kept")
    await reword(db_engine, booking)

    data, preview = await read(files_client, db_engine, booking)

    text_answer, choice_answer, _file = data
    assert text_answer["question_text"] == "Which programme?"
    assert [o["text"] for o in choice_answer["options"]] == ["IELTS", "TOEFL"]
    assert preview["first"]["question_text"] == "Which programme?"


async def test_an_answer_from_before_the_copy_reads_the_current_wording(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "aw-legacy")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE intake_answers SET question_text = NULL, option_text = NULL "
                "WHERE submission_id IN "
                "(SELECT id FROM intake_submissions WHERE session_id = :s)"
            ),
            {"s": booking["session_id"]},
        )
    await reword(db_engine, booking)

    data, preview = await read(files_client, db_engine, booking)

    text_answer, choice_answer, _file = data
    assert text_answer["question_text"] == "Which degree are you applying for?"
    assert [o["text"] for o in choice_answer["options"]] == ["IELTS Academic", "TOEFL"]
    assert preview["first"]["question_text"] == "Which degree are you applying for?"


async def test_booking_writes_the_wording_onto_every_row(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "aw-rows")

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT a.question_text, a.option_text, a.selected_option_id IS NOT NULL "
                    "FROM intake_answers a JOIN intake_submissions s ON s.id = a.submission_id "
                    "WHERE s.session_id = :s ORDER BY a.question_text, a.option_text"
                ),
                {"s": booking["session_id"]},
            )
        ).all()

    assert sorted((q, o) for q, o, _ in rows) == sorted(
        [
            ("Which programme?", None),
            ("Which tests?", "IELTS"),
            ("Which tests?", "TOEFL"),
            ("Your CV", None),
        ]
    )
    assert all((o is not None) == is_choice for _, o, is_choice in rows)
