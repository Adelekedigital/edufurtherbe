"""A mentee uploads a file, answers a `file_upload` question with it, and the
three people who may read it can download it.

Owner decisions, 2026-09-27: PDF and Word only, decided from the bytes; 5 MB by
default, configurable; readable by the uploader, the session's mentor and
admins, and nobody else (404); retention configurable, empty meaning forever;
uploaded to the API and downloaded through it, never a signed URL.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_bookable_offering, a_mentee, body, first_slot, key
from tests.integration.test_booking_answers import (  # noqa: F401 - enforcing_client is a fixture
    add_question,
    enforcing_client,
)

from app.core.config import Settings
from app.domain.enums import IntakeFileType
from app.infra.db.engine import create_session_factory
from app.infra.db.intake_file_store import sweep_intake_files
from conftest import (
    PDF_BYTES,
    PROBLEM_JSON,
    FakeStorage,
    api_token,
    bearer,
    build_api_app,
    client_for,
    docx_bytes,
    storage_for,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

BUCKET = "intake-files"
UPLOAD = "/api/v1/me/intake-files"


@pytest.fixture
def intake_fake() -> FakeStorage:
    return FakeStorage(bucket=BUCKET)


def intake_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, supabase_intake_bucket=BUCKET, **overrides)


async def intake_client(
    engine: AsyncEngine, fake: FakeStorage, settings: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_api_app(engine, None, settings)
    app.state.intake_storage = storage_for(fake)
    async with client_for(app) as client:
        yield client


@pytest_asyncio.fixture
async def files_client(
    db_engine: AsyncEngine, intake_fake: FakeStorage
) -> AsyncIterator[httpx.AsyncClient]:
    async for client in intake_client(db_engine, intake_fake, intake_settings()):
        yield client


def pdf(name: str = "My CV.pdf", payload: bytes = PDF_BYTES) -> dict[str, Any]:
    return {"file": (name, payload, "application/octet-stream")}


async def upload(
    client: httpx.AsyncClient, headers: dict[str, str], **kwargs: Any
) -> httpx.Response:
    return await client.post(UPLOAD, files=pdf(**kwargs), headers=headers)


async def file_row(engine: AsyncEngine, file_id: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT * FROM intake_files WHERE id = :i"), {"i": file_id})
        ).first()
        return dict(row._mapping) if row else None


async def signed_in(engine: AsyncEngine, user: UUID) -> dict[str, str]:
    """Headers for an existing user, giving them an identity if they had none."""
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": user}
        )
    return bearer(api_token(auth_id))


async def an_admin(engine: AsyncEngine, tag: str) -> dict[str, str]:
    user, headers = await a_mentee(engine, f"admin-{tag}")
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'limited_access')"),
            {"u": user},
        )
    return headers


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


async def test_a_pdf_is_stored_privately_and_described(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    mentee, headers = await a_mentee(db_engine, "file-pdf")

    response = await upload(files_client, headers, name="../../My CV.pdf")

    assert response.status_code == 201, response.text
    data = response.json()
    assert response.headers["location"] == f"/api/v1/intake-files/{data['file_id']}"
    assert data == {
        "file_id": data["file_id"],
        "filename": "My CV.pdf",
        "size": len(PDF_BYTES),
        "content_type": "application/pdf",
    }
    row = await file_row(db_engine, data["file_id"])
    assert row is not None
    assert row["uploader_id"] == mentee
    assert row["session_id"] is None
    # Ids only: the uploader's, then a random object name — never the filename.
    uploader_part, object_part = row["storage_key"].split("/")
    assert uploader_part == str(mentee)
    assert UUID(object_part)
    assert intake_fake.objects[row["storage_key"]] == (PDF_BYTES, "application/pdf")


async def test_a_word_document_is_accepted_by_its_content(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "file-docx")

    response = await upload(files_client, headers, name="essay.pdf", payload=docx_bytes())

    assert response.status_code == 201, response.text
    assert response.json()["content_type"] == IntakeFileType.DOCX.value
    assert response.json()["filename"] == "essay.pdf.docx"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"just some text", id="text"),
        pytest.param(
            docx_bytes(main="application/vnd.ms-word.document.macroEnabled.main+xml"), id="docm"
        ),
    ],
)
async def test_anything_but_pdf_or_word_is_refused(
    files_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    intake_fake: FakeStorage,
    payload: bytes,
) -> None:
    _, headers = await a_mentee(db_engine, f"file-bad-{len(payload)}")

    response = await upload(files_client, headers, name="cv.pdf", payload=payload)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    assert [e["pointer"] for e in response.json()["errors"]] == ["/file"]
    assert intake_fake.uploads == []


async def test_a_file_over_the_configured_limit_is_refused(
    db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    _, headers = await a_mentee(db_engine, "file-big")
    settings = intake_settings(intake_file_max_bytes=len(PDF_BYTES))
    async for client in intake_client(db_engine, intake_fake, settings):
        exact = await upload(client, headers)
        over = await upload(client, headers, payload=PDF_BYTES + b" ")

    assert exact.status_code == 201, exact.text
    assert over.status_code == 422
    assert [e["pointer"] for e in over.json()["errors"]] == ["/file"]
    assert len(intake_fake.uploads) == 1


async def test_an_unconfigured_bucket_refuses_as_misconfigured(
    db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    _, headers = await a_mentee(db_engine, "file-nobucket")
    async for client in intake_client(db_engine, intake_fake, Settings(_env_file=None)):
        response = await upload(client, headers)

    assert response.status_code == 500
    assert response.json()["detail"] == "The service is misconfigured."
    assert intake_fake.uploads == []


async def test_an_upload_needs_a_token(files_client: httpx.AsyncClient) -> None:
    response = await files_client.post(UPLOAD, files=pdf())
    assert response.status_code == 401


async def test_pending_uploads_are_bounded_per_person(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    from app.domain.intake_files import MAX_PENDING_UPLOADS

    _, headers = await a_mentee(db_engine, "file-many")
    for _ in range(MAX_PENDING_UPLOADS):
        assert (await upload(files_client, headers)).status_code == 201

    response = await upload(files_client, headers)

    assert response.status_code == 409, response.text


# --------------------------------------------------------------------------
# Answering with it
# --------------------------------------------------------------------------


async def booking_with_file(
    engine: AsyncEngine,
    tag: str,
    *,
    required: bool = True,
) -> tuple[UUID, UUID, UUID, dict[str, str]]:
    """A mentor whose offering asks for a CV, and a mentee holding one upload."""
    mentor, session_type = await a_bookable_offering(engine, tag)
    question, _ = await add_question(
        engine, session_type, "Your CV", question_type="file_upload", required=required
    )
    _, headers = await a_mentee(engine, f"{tag}-mentee")
    return mentor, session_type, question, headers


async def book(
    client: httpx.AsyncClient,
    mentor: UUID,
    session_type: UUID,
    headers: dict[str, str],
    answers: list[dict[str, Any]],
) -> httpx.Response:
    payload = body(session_type, await first_slot(client, mentor, session_type))
    return await client.post(
        "/api/v1/sessions", json=payload | {"answers": answers}, headers=headers | key()
    )


async def test_a_file_answers_the_question_and_is_linked_to_the_booking(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type, question, headers = await booking_with_file(db_engine, "file-answer")
    file_id = (await upload(files_client, headers)).json()["file_id"]

    response = await book(
        files_client,
        mentor,
        session_type,
        headers,
        [{"question_id": str(question), "file_id": file_id}],
    )

    assert response.status_code == 201, response.text
    row = await file_row(db_engine, file_id)
    assert row is not None
    assert str(row["session_id"]) == response.json()["id"]
    async with db_engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT file_storage_key FROM intake_answers WHERE question_id = :q"),
                {"q": question},
            )
        ).scalar_one()
    assert stored == row["storage_key"]


async def test_a_required_file_question_blocks_a_booking_without_one(
    enforcing_client: httpx.AsyncClient,  # noqa: F811 - the imported fixture
    db_engine: AsyncEngine,
) -> None:
    """Where required answers are enforced (#283), a file question is one."""
    mentor, session_type, _, headers = await booking_with_file(db_engine, "file-required")

    response = await book(enforcing_client, mentor, session_type, headers, [])

    assert response.status_code == 422, response.text
    assert [e["pointer"] for e in response.json()["errors"]] == ["/answers"]


async def test_somebody_elses_file_is_refused(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type, question, headers = await booking_with_file(db_engine, "file-theirs")
    _, other = await a_mentee(db_engine, "file-theirs-other")
    theirs = (await upload(files_client, other)).json()["file_id"]

    response = await book(
        files_client,
        mentor,
        session_type,
        headers,
        [{"question_id": str(question), "file_id": theirs}],
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/answers/0/file_id"
    assert (await file_row(db_engine, theirs) or {})["session_id"] is None


async def test_a_file_already_used_cannot_answer_a_second_booking(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, session_type, question, headers = await booking_with_file(
        db_engine, "file-reuse", required=False
    )
    file_id = (await upload(files_client, headers)).json()["file_id"]
    answer = [{"question_id": str(question), "file_id": file_id}]
    first = await book(files_client, mentor, session_type, headers, answer)
    assert first.status_code == 201, first.text

    second = await book(files_client, mentor, session_type, headers, answer)

    assert second.status_code == 422
    assert second.json()["errors"][0]["pointer"] == "/answers/0/file_id"


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


async def a_linked_file(
    client: httpx.AsyncClient, engine: AsyncEngine, tag: str
) -> tuple[str, UUID, dict[str, str]]:
    mentor, session_type, question, headers = await booking_with_file(engine, tag)
    file_id = (await upload(client, headers, name="CV;final.pdf")).json()["file_id"]
    booked = await book(
        client, mentor, session_type, headers, [{"question_id": str(question), "file_id": file_id}]
    )
    assert booked.status_code == 201, booked.text
    return file_id, mentor, headers


async def test_the_three_readers_download_it_as_an_attachment(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    file_id, mentor, mentee_headers = await a_linked_file(files_client, db_engine, "dl-ok")
    readers = {
        "uploader": mentee_headers,
        "mentor": await signed_in(db_engine, mentor),
        "admin": await an_admin(db_engine, "dl-ok"),
    }

    for who, headers in readers.items():
        response = await files_client.get(f"/api/v1/intake-files/{file_id}", headers=headers)
        assert response.status_code == 200, (who, response.text)
        assert response.content == PDF_BYTES
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == (
            "attachment; filename=\"CV_final.pdf\"; filename*=UTF-8''CV%3Bfinal.pdf"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "private, no-store"


async def test_anyone_else_gets_404(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    file_id, _, _ = await a_linked_file(files_client, db_engine, "dl-other")
    _, stranger = await a_mentee(db_engine, "dl-other-stranger")
    other_mentor, _ = await a_bookable_offering(db_engine, "dl-other-mentor")

    for headers in (stranger, await signed_in(db_engine, other_mentor)):
        response = await files_client.get(f"/api/v1/intake-files/{file_id}", headers=headers)
        assert response.status_code == 404
    assert (
        await files_client.get(f"/api/v1/intake-files/{uuid4()}", headers=stranger)
    ).status_code == 404
    assert (await files_client.get(f"/api/v1/intake-files/{file_id}")).status_code == 401


async def test_a_mentor_cannot_read_an_upload_not_yet_booked_with_them(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, _, headers = await booking_with_file(db_engine, "dl-unlinked")
    file_id = (await upload(files_client, headers)).json()["file_id"]

    as_mentor = await files_client.get(
        f"/api/v1/intake-files/{file_id}", headers=await signed_in(db_engine, mentor)
    )
    as_uploader = await files_client.get(f"/api/v1/intake-files/{file_id}", headers=headers)

    assert as_mentor.status_code == 404
    assert as_uploader.status_code == 200


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


async def age(engine: AsyncEngine, file_id: str, **delta: float) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE intake_files SET created_at = now() - CAST(:d AS interval) WHERE id = :i"),
            {"d": dt.timedelta(**delta), "i": file_id},
        )


async def sweep(
    engine: AsyncEngine, fake: FakeStorage, *, retention_days: int | None, dry_run: bool = False
) -> dict[str, int]:
    async with create_session_factory(engine)() as session:
        return await sweep_intake_files(
            session,
            storage_for(fake),
            now=dt.datetime.now(dt.UTC),
            retention_days=retention_days,
            unused_hours=24,
            dry_run=dry_run,
        )


async def test_an_abandoned_upload_is_deleted_and_a_fresh_one_kept(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    _, headers = await a_mentee(db_engine, "sweep-unused")
    old = (await upload(files_client, headers)).json()["file_id"]
    fresh = (await upload(files_client, headers)).json()["file_id"]
    await age(db_engine, old, hours=25)
    old_key = (await file_row(db_engine, old) or {})["storage_key"]

    dry = await sweep(db_engine, intake_fake, retention_days=None, dry_run=True)
    assert dry["unused"] == 1
    assert await file_row(db_engine, old) is not None
    assert old_key in intake_fake.objects

    counts = await sweep(db_engine, intake_fake, retention_days=None)

    assert counts["unused"] == 1
    assert await file_row(db_engine, old) is None
    assert old_key not in intake_fake.objects
    assert await file_row(db_engine, fresh) is not None


async def test_retention_removes_the_object_and_keeps_the_answer(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    file_id, _, headers = await a_linked_file(files_client, db_engine, "sweep-kept")
    await age(db_engine, file_id, days=31)
    key_ = (await file_row(db_engine, file_id) or {})["storage_key"]

    kept = await sweep(db_engine, intake_fake, retention_days=None)
    assert kept["expired"] == 0
    assert key_ in intake_fake.objects
    not_yet = await sweep(db_engine, intake_fake, retention_days=32)
    assert not_yet["expired"] == 0

    counts = await sweep(db_engine, intake_fake, retention_days=30)

    assert counts["expired"] == 1
    assert key_ not in intake_fake.objects
    row = await file_row(db_engine, file_id)
    assert row is not None
    assert row["deleted_at"] is not None
    assert row["purged_at"] is not None
    async with db_engine.connect() as conn:
        answers = (
            await conn.execute(
                text("SELECT count(*) FROM intake_answers WHERE file_storage_key = :k"),
                {"k": key_},
            )
        ).scalar_one()
    assert answers == 1
    gone = await files_client.get(f"/api/v1/intake-files/{file_id}", headers=headers)
    assert gone.status_code == 404
    assert (await sweep(db_engine, intake_fake, retention_days=30))["expired"] == 0
