"""Every session row carries a preview of its intake answers.

A list of bookings shows what each mentee wants to cover, so the preview rides
on ``SessionRead``: how many answers, and the first. It is read through the same
rows and fold as ``GET /sessions/{id}/answers``, and these tests pin that the
two agree.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, first_slot, key
from tests.integration.test_api_session_answers import answers, booked_with_answers, files_client
from tests.integration.test_booking_answers import add_question
from tests.integration.test_intake_files import signed_in, upload

pytestmark = [pytest.mark.db, pytest.mark.anyio]

__all__ = ["files_client"]


async def detail(client: httpx.AsyncClient, session_id: str, headers: dict[str, str]) -> Any:
    response = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def listed(
    client: httpx.AsyncClient, user_id: object, session_id: str, headers: dict[str, str]
) -> Any:
    response = await client.get(f"/api/v1/users/{user_id}/sessions", headers=headers)
    assert response.status_code == 200, response.text
    (row,) = [row for row in response.json()["data"] if row["id"] == session_id]
    return row


async def booked_first(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str, *, kind: str
) -> dict[str, Any]:
    """A booking whose first question (in form order) is a ``kind`` answer."""
    mentor, session_type = await a_bookable_offering(engine, tag)
    mentee, headers = await a_mentee(engine, f"{tag}-mentee")
    payload = body(session_type, await first_slot(client, mentor, session_type))
    if kind == "choice":
        question, (ielts, toefl) = await add_question(
            engine,
            session_type,
            "Which tests?",
            question_type="multi_choice",
            allows_multiple=True,
            options=("IELTS", "TOEFL"),
            order=0,
        )
        payload["answers"] = [
            {"question_id": str(question), "option_ids": [str(toefl), str(ielts)]}
        ]
    else:
        question, _ = await add_question(
            engine, session_type, "Your CV", question_type="file_upload", order=0
        )
        file_id = (await upload(client, headers, name="My CV.pdf")).json()["file_id"]
        payload["answers"] = [{"question_id": str(question), "file_id": file_id}]
    booked = await client.post("/api/v1/sessions", json=payload, headers=headers | key())
    assert booked.status_code == 201, booked.text
    return {"session_id": booked.json()["id"], "mentor": mentor, "mentee": mentee}


async def test_the_preview_counts_the_answers_and_shows_the_first_text_answer(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "ap-text")
    headers = await signed_in(db_engine, booking["mentor"])

    row = await detail(files_client, booking["session_id"], headers)

    assert row["answers_preview"] == {
        "count": 3,
        "first": {"question_text": "Which programme?", "text": "MSc Public Policy"},
    }


async def test_a_first_choice_answer_reads_as_its_options_in_form_order(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_first(files_client, db_engine, "ap-choice", kind="choice")

    row = await detail(
        files_client, booking["session_id"], await signed_in(db_engine, booking["mentor"])
    )

    assert row["answers_preview"] == {
        "count": 1,
        "first": {"question_text": "Which tests?", "text": "IELTS, TOEFL"},
    }


async def test_a_first_file_answer_reads_as_its_filename(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_first(files_client, db_engine, "ap-file", kind="file")

    row = await detail(
        files_client, booking["session_id"], await signed_in(db_engine, booking["mentor"])
    )

    assert row["answers_preview"]["first"] == {"question_text": "Your CV", "text": "My CV.pdf"}


async def test_a_session_with_no_answers_has_no_preview(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "ap-none")
    mentee, headers = await a_mentee(db_engine, "ap-none-mentee")
    payload = body(session_type, await first_slot(files_client, mentor, session_type))
    booked = await files_client.post("/api/v1/sessions", json=payload, headers=headers | key())
    assert booked.status_code == 201, booked.text

    assert booked.json()["answers_preview"] is None
    row = await listed(files_client, mentee, booked.json()["id"], headers)
    assert row["answers_preview"] is None


async def test_the_list_carries_the_same_preview_as_the_detail(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "ap-list")
    headers = booking["mentee_headers"]

    row = await listed(files_client, booking["mentee"], booking["session_id"], headers)

    assert (
        row["answers_preview"]
        == (await detail(files_client, booking["session_id"], headers))["answers_preview"]
    )
    assert row["answers_preview"]["count"] == 3


async def test_the_preview_agrees_with_the_answers_list(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Rule 8: the preview is a summary of the list, never a second definition."""
    booking = await booked_with_answers(files_client, db_engine, "ap-agree")
    headers = await signed_in(db_engine, booking["mentor"])

    preview = (await detail(files_client, booking["session_id"], headers))["answers_preview"]
    full = (await answers(files_client, booking["session_id"], headers)).json()["data"]

    assert preview["count"] == len(full)
    assert preview["first"]["question_text"] == full[0]["question_text"]
    assert preview["first"]["text"] == full[0]["text"]
