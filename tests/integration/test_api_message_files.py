"""Files in a chat (decision 302): upload with the intake checks, send with the
id, download by the two people in the chat, and the sweep of unsent uploads.

The access rule is the acceptance criterion: a third user can never download
another pair's file, and an upload is the uploader's alone until it is sent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.message_helpers import ROOT, Person, book, pair, person, scalar, thread

from app.core.config import Settings
from app.core.errors import FileUnavailableError
from app.infra.db.chat_writer import send_message
from app.infra.db.message_file_store import sweep_message_files
from conftest import PDF_BYTES, PROBLEM_JSON, FakeStorage, build_api_app, client_for, docx_bytes
from conftest import storage_for as real_storage_over

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BUCKET = "private-files"
UPLOAD = "/api/v1/me/message-files"
DOCM = "application/vnd.ms-word.document.macroEnabled.main+xml"


@pytest.fixture
def fake() -> FakeStorage:
    return FakeStorage(bucket=BUCKET)


def file_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, supabase_intake_bucket=BUCKET, **overrides)


async def files_app(
    engine: AsyncEngine, fake: FakeStorage, settings: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_api_app(engine, None, settings)
    app.state.intake_storage = real_storage_over(fake)
    async with client_for(app) as client:
        yield client


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine, fake: FakeStorage) -> AsyncIterator[httpx.AsyncClient]:
    async for each in files_app(db_engine, fake, file_settings()):
        yield each


async def upload(
    client: httpx.AsyncClient,
    who: Person,
    payload: bytes = PDF_BYTES,
    name: str = "Essay draft.pdf",
) -> httpx.Response:
    return await client.post(
        UPLOAD, files={"file": (name, payload, "application/octet-stream")}, headers=who.headers
    )


async def uploaded(client: httpx.AsyncClient, who: Person) -> str:
    response = await upload(client, who)
    assert response.status_code == 201, response.text
    return str(response.json()["file_id"])


async def send_file(
    client: httpx.AsyncClient,
    sender: Person,
    to: Person,
    file_id: str,
    *,
    body: str | None = None,
    client_id: UUID | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"client_id": str(client_id or uuid4()), "file_id": file_id}
    if body is not None:
        payload["body"] = body
    return await client.post(f"{ROOT}/{to.id}", json=payload, headers=sender.headers)


async def download(client: httpx.AsyncClient, who: Person, file_id: str) -> httpx.Response:
    return await client.get(f"/api/v1/message-files/{file_id}", headers=who.headers)


# --------------------------------------------------------------------------
# Upload: the intake checks, through the one shared function
# --------------------------------------------------------------------------


async def test_an_upload_is_stored_privately_by_ids_and_described(
    client: httpx.AsyncClient, db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    me = await person(db_engine, "up")

    response = await upload(client, me, name="../../Essay draft.pdf")

    assert response.status_code == 201, response.text
    data = response.json()
    assert data == {
        "file_id": data["file_id"],
        "filename": "Essay draft.pdf",
        "size": len(PDF_BYTES),
        "content_type": "application/pdf",
    }
    assert response.headers["location"] == f"/api/v1/message-files/{data['file_id']}"
    key = await scalar(
        db_engine,
        "SELECT storage_key FROM message_attachments WHERE id = :i AND uploaded_by = :u "
        "AND message_id IS NULL",
        i=data["file_id"],
        u=me.id,
    )
    prefix, uploader, object_name = key.split("/")
    assert (prefix, uploader) == ("messages", str(me.id))
    assert UUID(object_name) != UUID(data["file_id"])
    assert fake.objects[key] == (PDF_BYTES, "application/pdf")


async def test_a_word_document_is_accepted(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    me = await person(db_engine, "up-docx")

    response = await upload(client, me, docx_bytes(), "notes.docx")

    assert response.status_code == 201, response.text
    assert response.json()["content_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@pytest.mark.parametrize(
    "payload",
    [b"just text", docx_bytes(main=DOCM), docx_bytes(parts=()), b"PK\x03\x04 not a zip", b""],
    ids=["text", "docm", "renamed-zip", "broken-zip", "empty"],
)
async def test_anything_but_pdf_or_word_is_a_typed_refusal(
    client: httpx.AsyncClient, db_engine: AsyncEngine, fake: FakeStorage, payload: bytes
) -> None:
    me = await person(db_engine, "up-bad")

    response = await upload(client, me, payload, "cv.pdf")

    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == "/problems/file-type-not-allowed"
    assert [e["pointer"] for e in response.json()["errors"]] == ["/file"]
    assert fake.uploads == []
    assert await scalar(db_engine, "SELECT count(*) FROM message_attachments") == 0


async def test_a_file_over_the_limit_is_a_typed_refusal(
    db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    me = await person(db_engine, "up-big")
    async for each in files_app(
        db_engine, fake, file_settings(intake_file_max_bytes=len(PDF_BYTES))
    ):
        exact = await upload(each, me)
        over = await upload(each, me, PDF_BYTES + b" ")

    assert exact.status_code == 201, exact.text
    assert over.status_code == 422
    assert over.json()["type"] == "/problems/file-too-large"
    assert [e["pointer"] for e in over.json()["errors"]] == ["/file"]
    assert len(fake.uploads) == 1


async def test_the_hourly_limit_is_counted_apart_from_intake(
    db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    me = await person(db_engine, "up-rate")
    async for each in files_app(db_engine, fake, file_settings(intake_uploads_per_hour=2)):
        allowed = [await upload(each, me) for _ in range(2)]
        over = await upload(each, me)
        intake = await each.post(
            "/api/v1/me/intake-files",
            files={"file": ("cv.pdf", PDF_BYTES, "application/pdf")},
            headers=me.headers,
        )

    assert [r.status_code for r in allowed] == [201, 201]
    assert over.status_code == 429
    assert over.json()["type"] == "/problems/rate-limited"
    assert 1 <= int(over.headers["retry-after"]) <= 3600
    # The chat's uploads spent the chat's budget, not intake's.
    assert intake.status_code == 201, intake.text


async def test_unsent_uploads_are_bounded_per_person(
    db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    mentor, me = await pair(db_engine, "up-pending")
    async for each in files_app(db_engine, fake, file_settings(intake_uploads_per_hour=50)):
        # A sent file is not pending: it does not count.
        sent = await send_file(each, me, mentor, await uploaded(each, me))
        held = [await upload(each, me) for _ in range(10)]
        refused = await upload(each, me)

    assert sent.status_code == 201
    assert all(r.status_code == 201 for r in held)
    assert refused.status_code == 409
    assert "not yet sent" in refused.json()["detail"]


async def test_an_unconfigured_bucket_refuses_as_misconfigured(
    db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    me = await person(db_engine, "up-nobucket")
    async for each in files_app(db_engine, fake, Settings(_env_file=None)):
        response = await upload(each, me)

    assert response.status_code == 500
    assert fake.uploads == []


# --------------------------------------------------------------------------
# Sending a file
# --------------------------------------------------------------------------


async def test_a_file_alone_is_sent_and_read_back_by_both(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-file")
    file_id = await uploaded(client, mentee)

    response = await send_file(client, mentee, mentor, file_id)

    assert response.status_code == 201, response.text
    sent = response.json()
    expected = {
        "id": file_id,
        "filename": "Essay draft.pdf",
        "size": len(PDF_BYTES),
        "content_type": "application/pdf",
    }
    assert sent["body"] is None
    assert sent["file"] == expected
    assert (
        await scalar(db_engine, "SELECT message_type FROM messages WHERE id = :i", i=sent["id"])
        == "attachment"
    )
    for reader in (mentor, mentee):
        items = (await thread(client, reader, mentee if reader is mentor else mentor)).json()
        assert items["items"][-1]["file"] == expected
    inbox = (await client.get(ROOT, headers=mentor.headers)).json()["data"][0]["last"]
    assert inbox["kind"] == "file"
    assert inbox["file_name"] == "Essay draft.pdf"
    assert inbox["text"] is None


async def test_text_and_a_file_go_in_one_message(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-both")
    file_id = await uploaded(client, mentor)

    response = await send_file(client, mentor, mentee, file_id, body="  Here it is  ")

    assert response.status_code == 201, response.text
    assert response.json()["body"] == "Here it is"
    assert response.json()["file"]["id"] == file_id
    last = (await client.get(ROOT, headers=mentee.headers)).json()["data"][0]["last"]
    assert (last["kind"], last["text"], last["file_name"]) == (
        "file",
        "Here it is",
        "Essay draft.pdf",
    )


async def test_a_text_message_carries_no_file(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-text")

    response = await client.post(
        f"{ROOT}/{mentor.id}",
        json={"client_id": str(uuid4()), "body": "Hi"},
        headers=mentee.headers,
    )

    assert response.status_code == 201
    assert response.json()["file"] is None
    last = (await client.get(ROOT, headers=mentee.headers)).json()["data"][0]["last"]
    assert (last["kind"], last["file_name"]) == ("text", None)


async def test_a_replayed_client_id_returns_the_message_with_its_file(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-replay")
    file_id = await uploaded(client, mentee)
    client_id = uuid4()

    first = await send_file(client, mentee, mentor, file_id, client_id=client_id)
    again = await send_file(client, mentee, mentor, file_id, client_id=client_id)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 1


async def test_neither_text_nor_a_file_is_refused_at_body(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-empty")
    for payload in ({"client_id": str(uuid4())}, {"client_id": str(uuid4()), "body": "  "}):
        response = await client.post(f"{ROOT}/{mentor.id}", json=payload, headers=mentee.headers)
        assert response.status_code == 422, payload
        assert [e["pointer"] for e in response.json()["errors"]] == ["/body"]
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 0


async def _unavailable(response: httpx.Response, engine: AsyncEngine, messages: int) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["type"] == "/problems/file-unavailable"
    assert [e["pointer"] for e in response.json()["errors"]] == ["/file_id"]
    # The message went back with the file: nothing half-sent.
    assert await scalar(engine, "SELECT count(*) FROM messages") == messages


async def test_somebody_elses_file_is_unavailable(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-theirs")
    theirs = await uploaded(client, mentor)

    response = await send_file(client, mentee, mentor, theirs)

    await _unavailable(response, db_engine, 0)
    assert (
        await scalar(
            db_engine, "SELECT message_id FROM message_attachments WHERE id = :i", i=theirs
        )
        is None
    )


async def test_a_file_already_sent_is_unavailable(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-twice")
    file_id = await uploaded(client, mentee)
    assert (await send_file(client, mentee, mentor, file_id)).status_code == 201

    response = await send_file(client, mentee, mentor, file_id)

    await _unavailable(response, db_engine, 1)


async def test_a_swept_or_unknown_file_is_unavailable(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "send-swept")
    file_id = await uploaded(client, mentee)
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM message_attachments WHERE id = :i"), {"i": file_id})

    swept = await send_file(client, mentee, mentor, file_id)
    unknown = await send_file(client, mentee, mentor, str(uuid4()))

    await _unavailable(swept, db_engine, 0)
    await _unavailable(unknown, db_engine, 0)


async def test_a_file_cannot_be_sent_to_somebody_you_are_not_connected_with(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, mentee = await pair(db_engine, "send-stranger")
    stranger = await person(db_engine, "send-stranger-x")
    file_id = await uploaded(client, mentee)

    response = await send_file(client, mentee, stranger, file_id)

    assert response.status_code == 403
    assert (
        await scalar(
            db_engine, "SELECT message_id FROM message_attachments WHERE id = :i", i=file_id
        )
        is None
    )


async def test_two_sends_racing_for_one_file_attach_it_once(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The race held open: the first send has attached the file and not
    committed when the second tries. The second waits on the row, then finds
    it sent and is refused, so one file never lands in two messages."""
    mentor, mentee = await pair(db_engine, "send-race")
    file_id = UUID(await uploaded(client, mentee))
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)

    async with factory() as first, factory() as second:
        won, _ = await send_message(
            first,
            mentee.id,
            mentor.id,
            client_id=uuid4(),
            body=None,
            file_id=file_id,
            unused_hours=24,
            now=now,
        )
        racing = asyncio.create_task(
            send_message(
                second,
                mentee.id,
                mentor.id,
                client_id=uuid4(),
                body=None,
                file_id=file_id,
                unused_hours=24,
                now=now,
            )
        )
        await asyncio.sleep(1)
        assert not racing.done(), "the second send did not wait for the first"
        await first.commit()
        with pytest.raises(FileUnavailableError):
            await racing
        await second.rollback()

    assert won["file_id"] == file_id
    assert await scalar(db_engine, "SELECT count(*) FROM messages") == 1
    assert (
        await scalar(
            db_engine, "SELECT message_id FROM message_attachments WHERE id = :i", i=file_id
        )
        == won["id"]
    )


# --------------------------------------------------------------------------
# Download: the uploader before sending, then the two people in the chat
# --------------------------------------------------------------------------


async def test_the_uploader_alone_reads_an_unsent_file(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "dl-unsent")
    file_id = await uploaded(client, mentee)

    mine = await download(client, mentee, file_id)
    recipient = await download(client, mentor, file_id)

    assert mine.status_code == 200, mine.text
    assert mine.content == PDF_BYTES
    assert mine.headers["content-type"] == "application/pdf"
    assert mine.headers["content-disposition"] == (
        "attachment; filename=\"Essay draft.pdf\"; filename*=UTF-8''Essay%20draft.pdf"
    )
    assert mine.headers["x-content-type-options"] == "nosniff"
    assert recipient.status_code == 404


async def test_a_sent_file_is_read_by_both_and_never_by_a_third(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "dl-sent")
    file_id = await uploaded(client, mentee)
    assert (await send_file(client, mentee, mentor, file_id)).status_code == 201
    stranger = await person(db_engine, "dl-stranger")
    # Connected to the mentor through a booking of their own: still not in this chat.
    other_mentee = await person(db_engine, "dl-other-mentee")
    await book(db_engine, mentor, other_mentee, starts_in=dt.timedelta(days=5))
    # Connected to the mentee as their mentor elsewhere: still not in this chat.
    other_mentor = await person(db_engine, "dl-other-mentor", mentor="listed")
    await book(db_engine, other_mentor, mentee, starts_in=dt.timedelta(days=7))

    readers = [await download(client, who, file_id) for who in (mentee, mentor)]
    others = [
        await download(client, who, file_id) for who in (stranger, other_mentee, other_mentor)
    ]

    assert [r.status_code for r in readers] == [200, 200]
    assert all(r.content == PDF_BYTES for r in readers)
    assert [r.status_code for r in others] == [404, 404, 404]
    assert all(r.json()["type"] == "about:blank" for r in others)


async def test_a_file_whose_message_was_deleted_or_missing_is_404(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, mentee = await pair(db_engine, "dl-deleted")
    file_id = await uploaded(client, mentee)
    sent = await send_file(client, mentee, mentor, file_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE messages SET deleted_at = now() WHERE id = :i"), {"i": sent.json()["id"]}
        )

    assert (await download(client, mentor, file_id)).status_code == 404
    assert (await download(client, mentee, file_id)).status_code == 404
    assert (await download(client, mentee, str(uuid4()))).status_code == 404
    reread = (await thread(client, mentor, mentee)).json()["items"][-1]
    assert (reread["deleted"], reread["file"]) == (True, None)
    last = (await client.get(ROOT, headers=mentor.headers)).json()["data"][0]["last"]
    assert (last["deleted"], last["file_name"], last["text"]) == (True, None, None)


async def test_a_participant_no_longer_connected_cannot_read_it(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """M0 decides a download as it decides the thread: a chat whose only
    booking stops connecting (an imported chat, or a status corrected later)
    is unreadable to both, file included."""
    mentor, mentee = await pair(db_engine, "dl-unlinked")
    file_id = await uploaded(client, mentee)
    assert (await send_file(client, mentee, mentor, file_id)).status_code == 201
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET status = 'withdrawn' WHERE mentee_id = :u"), {"u": mentee.id}
        )

    assert (await thread(client, mentee, mentor)).status_code == 404
    assert (await download(client, mentee, file_id)).status_code == 404
    assert (await download(client, mentor, file_id)).status_code == 404


async def test_a_download_needs_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/api/v1/message-files/{uuid4()}")).status_code == 401
    assert (await client.post(UPLOAD, files={"file": ("a.pdf", PDF_BYTES)})).status_code == 401


# --------------------------------------------------------------------------
# The sweep of uploads never sent
# --------------------------------------------------------------------------


async def test_the_sweep_removes_old_unsent_uploads_and_keeps_the_rest(
    client: httpx.AsyncClient, db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    mentor, mentee = await pair(db_engine, "sweep")
    old_unsent = await uploaded(client, mentee)
    old_sent = await uploaded(client, mentee)
    fresh = await uploaded(client, mentee)
    assert (await send_file(client, mentee, mentor, old_sent)).status_code == 201
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE message_attachments SET created_at = now() - interval '25 hours' "
                "WHERE id IN (:a, :b)"
            ),
            {"a": old_unsent, "b": old_sent},
        )
    keys = {
        file_id: await scalar(
            db_engine, "SELECT storage_key FROM message_attachments WHERE id = :i", i=file_id
        )
        for file_id in (old_unsent, old_sent, fresh)
    }
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    storage = real_storage_over(fake)
    now = dt.datetime.now(dt.UTC)

    async with factory() as session:
        dry = await sweep_message_files(session, storage, now=now, unused_hours=24, dry_run=True)
    assert dry == {"message_files_swept": 1, "message_files_failed": 0}
    assert len(fake.objects) == 3

    async with factory() as session:
        counts = await sweep_message_files(
            session, storage, now=now, unused_hours=24, dry_run=False
        )

    assert counts == {"message_files_swept": 1, "message_files_failed": 0}
    assert set(fake.objects) == {keys[old_sent], keys[fresh]}
    remaining = await scalar(db_engine, "SELECT array_agg(id::text) FROM message_attachments")
    assert sorted(remaining) == sorted([old_sent, fresh])
    assert (await send_file(client, mentee, mentor, old_unsent)).status_code == 422


async def test_a_failed_object_delete_keeps_the_row_for_the_next_run(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, mentee = await pair(db_engine, "sweep-fail")
    file_id = await uploaded(client, mentee)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_attachments SET created_at = now() - interval '2 days'")
        )
    failing = FakeStorage(bucket=BUCKET, delete_fails="status")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        counts = await sweep_message_files(
            session,
            real_storage_over(failing),
            now=dt.datetime.now(dt.UTC),
            unused_hours=24,
            dry_run=False,
        )

    assert counts == {"message_files_swept": 0, "message_files_failed": 1}
    assert (
        await scalar(db_engine, "SELECT count(*) FROM message_attachments WHERE id = :i", i=file_id)
        == 1
    )


async def test_the_daily_retention_job_sweeps_message_files_too(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fake: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep rides `sweep-intake-files`: the same job, bucket and hours."""
    from app.infra.db.engine import create_session_factory
    from app.infra.jobs import runner

    class Borrowed:
        """The test engine, lent to the job without letting it dispose it."""

        def __getattr__(self, name: str) -> Any:
            return getattr(db_engine, name)

        async def dispose(self) -> None:
            return None

    _, mentee = await pair(db_engine, "sweep-job")
    file_id = await uploaded(client, mentee)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_attachments SET created_at = now() - interval '25 hours'")
        )
    monkeypatch.setattr(runner, "create_database_engine", lambda _s: Borrowed())
    monkeypatch.setattr(
        runner, "create_session_factory", lambda _e: create_session_factory(db_engine)
    )
    monkeypatch.setattr(runner, "intake_storage_for", lambda _s, _c: real_storage_over(fake))

    counts = await runner.RuntimeJobs(settings=file_settings())._sweep_intake_files(dry_run=False)

    assert counts["message_files_swept"] == 1
    assert (
        await scalar(db_engine, "SELECT count(*) FROM message_attachments WHERE id = :i", i=file_id)
        == 0
    )
    assert fake.objects == {}


async def test_an_upload_past_its_hours_neither_sends_nor_counts_before_the_sweep(
    db_engine: AsyncEngine, fake: FakeStorage
) -> None:
    """The cap, the attach and the sweep read one rule: an upload past
    `INTAKE_FILE_UNUSED_HOURS` is done with, whether or not the sweep ran."""
    mentor, me = await pair(db_engine, "expired")
    async for each in files_app(db_engine, fake, file_settings(intake_uploads_per_hour=50)):
        stale = [await uploaded(each, me) for _ in range(10)]
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE message_attachments SET created_at = now() - interval '25 hours'")
            )
        fresh = await upload(each, me)
        sent = await send_file(each, me, mentor, stale[0])

    assert fresh.status_code == 201, fresh.text
    await _unavailable(sent, db_engine, 0)
