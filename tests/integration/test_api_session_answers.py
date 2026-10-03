"""The mentor reads what the mentee answered when booking (#269).

`GET /sessions/{id}/answers` is read by the session's mentee, its mentor and
admins, the same three who may download the file behind a file answer
(decision 210). Everyone else gets the 404 a missing session gets.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, first_slot, key
from tests.integration.test_booking_answers import add_question
from tests.integration.test_intake_files import (
    BUCKET,
    an_admin,
    intake_client,
    intake_settings,
    signed_in,
    upload,
)

from conftest import FakeStorage

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest_asyncio.fixture
async def files_client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """An app whose intake bucket is an in-memory fake, so uploads work."""
    async for client in intake_client(db_engine, FakeStorage(bucket=BUCKET), intake_settings()):
        yield client


async def booked_with_answers(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str
) -> dict[str, Any]:
    """A booking that answered a text, a choice and a file question."""
    mentor, session_type = await a_bookable_offering(engine, tag)
    prose, _ = await add_question(engine, session_type, "Which programme?", order=0)
    choice, (ielts, toefl, _gre) = await add_question(
        engine,
        session_type,
        "Which tests?",
        question_type="multi_choice",
        allows_multiple=True,
        options=("IELTS", "TOEFL", "GRE"),
        order=1,
    )
    cv, _ = await add_question(
        engine, session_type, "Your CV", question_type="file_upload", order=2
    )
    mentee, headers = await a_mentee(engine, f"{tag}-mentee")
    file_id = (await upload(client, headers, name="My CV.pdf")).json()["file_id"]
    payload = body(session_type, await first_slot(client, mentor, session_type))
    payload["answers"] = [
        {"question_id": str(cv), "file_id": file_id},
        {"question_id": str(choice), "option_ids": [str(toefl), str(ielts)]},
        {"question_id": str(prose), "text": "MSc Public Policy"},
    ]
    booked = await client.post("/api/v1/sessions", json=payload, headers=headers | key())
    assert booked.status_code == 201, booked.text
    return {
        "session_id": booked.json()["id"],
        "mentor": mentor,
        "mentee": mentee,
        "mentee_headers": headers,
        "questions": (prose, choice, cv),
        "options": (ielts, toefl),
        "file_id": file_id,
    }


async def answers(client: httpx.AsyncClient, session_id: str, headers: dict[str, str]) -> Any:
    return await client.get(f"/api/v1/sessions/{session_id}/answers", headers=headers)


async def test_the_mentor_reads_every_answer_in_form_order(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-order")
    prose, choice, cv = booking["questions"]
    ielts, toefl = booking["options"]

    response = await answers(
        files_client, booking["session_id"], await signed_in(db_engine, booking["mentor"])
    )

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["next_cursor"] is None
    assert [a["question_id"] for a in page["data"]] == [str(prose), str(choice), str(cv)]
    text_answer, choice_answer, file_answer = page["data"]
    assert text_answer == {
        "question_id": str(prose),
        "question_text": "Which programme?",
        "question_type": "free_text",
        "retired": False,
        "text": "MSc Public Policy",
        "options": [],
        "file": None,
    }
    # The form's order, not the order the mentee picked them in.
    assert choice_answer["options"] == [
        {"id": str(ielts), "text": "IELTS"},
        {"id": str(toefl), "text": "TOEFL"},
    ]
    assert choice_answer["text"] is None and choice_answer["file"] is None
    assert file_answer["question_type"] == "file_upload"
    assert file_answer["file"] == {
        "id": booking["file_id"],
        "filename": "My CV.pdf",
        "content_type": "application/pdf",
        "size": file_answer["file"]["size"],
        "available": True,
    }
    assert file_answer["file"]["size"] > 0


async def test_the_mentee_and_an_admin_read_them_too(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-three")

    for headers in (booking["mentee_headers"], await an_admin(db_engine, "sa-three")):
        response = await answers(files_client, booking["session_id"], headers)
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]) == 3


async def test_the_file_answer_downloads_for_the_mentor(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-dl")
    mentor = await signed_in(db_engine, booking["mentor"])
    file_id = (await answers(files_client, booking["session_id"], mentor)).json()["data"][2][
        "file"
    ]["id"]

    download = await files_client.get(f"/api/v1/intake-files/{file_id}", headers=mentor)

    assert download.status_code == 200


async def test_anyone_else_gets_the_404_of_a_missing_session(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-404")
    other_mentor, _ = await a_bookable_offering(db_engine, "sa-404-other")
    _, stranger = await a_mentee(db_engine, "sa-404-stranger")

    for headers in (await signed_in(db_engine, other_mentor), stranger):
        response = await answers(files_client, booking["session_id"], headers)
        assert response.status_code == 404, response.text
        assert booking["file_id"] not in response.text
        # The file's own reader rule (decision 210) refuses the same people, so
        # the answers and the file behind one never disagree about who may read.
        download = await files_client.get(
            f"/api/v1/intake-files/{booking['file_id']}", headers=headers
        )
        assert download.status_code == 404

    missing = await answers(files_client, str(uuid4()), booking["mentee_headers"])
    assert missing.status_code == 404


async def test_the_type_follows_the_answer_when_the_question_is_switched(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-switch")
    prose, _, cv = booking["questions"]
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_questions SET question_type = CASE id "
                "WHEN :p THEN 'file_upload' ELSE 'free_text' END WHERE id IN (:p, :c)"
            ),
            {"p": prose, "c": cv},
        )

    data = (await answers(files_client, booking["session_id"], booking["mentee_headers"])).json()[
        "data"
    ]

    assert (data[0]["question_type"], data[0]["text"]) == ("free_text", "MSc Public Policy")
    assert data[2]["question_type"] == "file_upload"
    assert data[2]["file"]["id"] == booking["file_id"]


async def test_a_booking_with_no_form_reads_as_an_empty_list(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type = await a_bookable_offering(db_engine, "sa-empty")
    _, headers = await a_mentee(db_engine, "sa-empty-mentee")
    payload = body(session_type, await first_slot(files_client, mentor, session_type))
    booked = await files_client.post("/api/v1/sessions", json=payload, headers=headers | key())
    assert booked.status_code == 201, booked.text

    response = await answers(files_client, booked.json()["id"], headers)

    assert response.status_code == 200
    assert response.json() == {"data": [], "next_cursor": None}


async def test_a_retired_question_and_a_removed_file_still_read(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-retired")
    prose, _, _ = booking["questions"]
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE session_type_questions SET deleted_at = now() WHERE id = :q"), {"q": prose}
        )
        await conn.execute(
            text("UPDATE intake_files SET deleted_at = now() WHERE id = :f"),
            {"f": booking["file_id"]},
        )

    data = (await answers(files_client, booking["session_id"], booking["mentee_headers"])).json()[
        "data"
    ]

    assert data[0]["retired"] is True
    assert data[0]["text"] == "MSc Public Policy"
    assert data[2]["file"]["available"] is False


async def test_reading_answers_logs_none_of_their_content(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    booking = await booked_with_answers(files_client, db_engine, "sa-logs")
    mentor = await signed_in(db_engine, booking["mentor"])

    with caplog.at_level(logging.DEBUG):
        response = await answers(files_client, booking["session_id"], mentor)

    assert response.status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("MSc Public Policy", "My CV.pdf", "Which programme?"):
        assert secret not in logged
