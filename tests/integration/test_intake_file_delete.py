"""A mentee deletes an upload they did not use (frontend fixes, item 7).

Without this, a file uploaded for a booking that never happened counted toward
the pending cap until the daily sweep expired it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_mentee
from tests.integration.test_intake_files import (
    a_linked_file,
    file_row,
    intake_client,
    intake_settings,
    upload,
)

from app.domain.intake_files import MAX_PENDING_UPLOADS
from app.infra.db.engine import create_session_factory
from app.infra.db.intake_file_store import sweep_intake_files
from conftest import FakeStorage, storage_for

pytestmark = [pytest.mark.db, pytest.mark.anyio]

BUCKET = "intake-files"


@pytest_asyncio.fixture
async def files_client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async for client in intake_client(
        db_engine, FakeStorage(bucket=BUCKET), intake_settings(intake_uploads_per_hour=100)
    ):
        yield client


def url(file_id: str) -> str:
    return f"/api/v1/me/intake-files/{file_id}"


async def test_an_unused_upload_is_deleted(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "del-own")
    file_id = (await upload(files_client, headers)).json()["file_id"]

    deleted = await files_client.delete(url(file_id), headers=headers)

    assert deleted.status_code == 204, deleted.text
    row = await file_row(db_engine, file_id)
    assert row is not None and row["deleted_at"] is not None
    assert (
        await files_client.get(f"/api/v1/intake-files/{file_id}", headers=headers)
    ).status_code == 404


async def test_a_deleted_upload_frees_its_place_under_the_cap(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "del-cap")
    ids = [
        (await upload(files_client, headers)).json()["file_id"] for _ in range(MAX_PENDING_UPLOADS)
    ]
    assert (await upload(files_client, headers)).status_code == 409

    deleted = await files_client.delete(url(ids[0]), headers=headers)

    assert deleted.status_code == 204, deleted.text
    assert (await upload(files_client, headers)).status_code == 201
    for kept in ids[1:]:
        row = await file_row(db_engine, kept)
        assert row is not None and row["deleted_at"] is None


async def test_somebody_elses_upload_is_404_and_stays(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, owner = await a_mentee(db_engine, "del-owner")
    _, stranger = await a_mentee(db_engine, "del-stranger")
    file_id = (await upload(files_client, owner)).json()["file_id"]

    deleted = await files_client.delete(url(file_id), headers=stranger)

    assert deleted.status_code == 404, deleted.text
    row = await file_row(db_engine, file_id)
    assert row is not None and row["deleted_at"] is None


async def test_a_file_answering_a_booking_cannot_be_deleted(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The mentor relies on it; it leaves with the session's retention."""
    file_id, _, headers = await a_linked_file(files_client, db_engine, "del-linked")

    deleted = await files_client.delete(url(file_id), headers=headers)

    assert deleted.status_code == 409, deleted.text
    row = await file_row(db_engine, file_id)
    assert row is not None and row["deleted_at"] is None


async def test_deleting_twice_is_404(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "del-twice")
    file_id = (await upload(files_client, headers)).json()["file_id"]
    await files_client.delete(url(file_id), headers=headers)

    again = await files_client.delete(url(file_id), headers=headers)

    assert again.status_code == 404, again.text


async def test_a_deleted_upload_still_counts_toward_the_hourly_rate(
    db_engine: AsyncEngine,
) -> None:
    """Deleting is not a way round the upload rate."""
    async for client in intake_client(
        db_engine, FakeStorage(bucket=BUCKET), intake_settings(intake_uploads_per_hour=2)
    ):
        _, headers = await a_mentee(db_engine, "del-rate")
        for _ in range(2):
            file_id = (await upload(client, headers)).json()["file_id"]
            assert (await client.delete(url(file_id), headers=headers)).status_code == 204

        assert (await upload(client, headers)).status_code == 429


async def test_somebody_elses_file_on_a_booking_is_404_not_409(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A stranger cannot learn which files answer a booking."""
    file_id, _, _ = await a_linked_file(files_client, db_engine, "del-linked-stranger")
    _, stranger = await a_mentee(db_engine, "del-linked-stranger-other")

    deleted = await files_client.delete(url(file_id), headers=stranger)

    assert deleted.status_code == 404, deleted.text
    row = await file_row(db_engine, file_id)
    assert row is not None and row["deleted_at"] is None


async def test_a_deleted_upload_keeps_its_row_through_a_sweep_within_the_hour(
    files_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """So the hourly rate still counts it."""
    _, headers = await a_mentee(db_engine, "del-sweep")
    file_id = (await upload(files_client, headers)).json()["file_id"]
    assert (await files_client.delete(url(file_id), headers=headers)).status_code == 204

    async with create_session_factory(db_engine)() as db:
        await sweep_intake_files(
            db,
            storage_for(FakeStorage(bucket=BUCKET)),
            now=dt.datetime.now(dt.UTC),
            retention_days=None,
            unused_hours=24,
            dry_run=False,
        )

    assert await file_row(db_engine, file_id) is not None
