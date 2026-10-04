"""Intake uploads are rate-limited per person (#281).

The cap on unused uploads bounds storage but not rate: a mentee can upload,
book, and upload again. `INTAKE_UPLOADS_PER_HOUR` bounds that, and the next
upload is a `429` saying when to retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_api_booking import a_mentee
from tests.integration.test_intake_files import intake_client, intake_settings, upload

from conftest import PROBLEM_JSON, FakeStorage

pytestmark = [pytest.mark.db, pytest.mark.anyio]

LIMIT = 2


@pytest_asyncio.fixture
async def limited_client(
    db_engine: AsyncEngine, intake_fake: FakeStorage
) -> AsyncIterator[httpx.AsyncClient]:
    settings = intake_settings(intake_uploads_per_hour=LIMIT)
    async for client in intake_client(db_engine, intake_fake, settings):
        yield client


@pytest.fixture
def intake_fake() -> FakeStorage:
    return FakeStorage(bucket="intake-files")


async def age_uploads(engine: AsyncEngine, uploader: UUID, minutes: int) -> None:
    """Move every upload by `uploader` `minutes` into the past."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE intake_files SET created_at = now() - make_interval(mins => :m) "
                "WHERE uploader_id = :u"
            ),
            {"m": minutes, "u": uploader},
        )


async def test_the_upload_past_the_hourly_limit_is_a_429_with_retry_after(
    limited_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, headers = await a_mentee(db_engine, "rate-over")
    for _ in range(LIMIT):
        assert (await upload(limited_client, headers)).status_code == 201

    response = await upload(limited_client, headers)

    assert response.status_code == 429, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == "/problems/rate-limited"
    retry = int(response.headers["retry-after"])
    assert 1 <= retry <= 3600


async def test_uploads_up_to_the_limit_are_all_accepted(
    db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    _, headers = await a_mentee(db_engine, "rate-setting")
    async for client in intake_client(
        db_engine, intake_fake, intake_settings(intake_uploads_per_hour=3)
    ):
        statuses = [(await upload(client, headers)).status_code for _ in range(4)]

    assert statuses == [201, 201, 201, 429]


async def test_an_upload_is_allowed_again_once_the_hour_has_passed(
    limited_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentee, headers = await a_mentee(db_engine, "rate-window")
    for _ in range(LIMIT):
        assert (await upload(limited_client, headers)).status_code == 201
    await age_uploads(db_engine, mentee, minutes=61)

    response = await upload(limited_client, headers)

    assert response.status_code == 201, response.text


async def test_retry_after_counts_down_to_when_the_oldest_upload_leaves_the_hour(
    limited_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentee, headers = await a_mentee(db_engine, "rate-retry")
    for _ in range(LIMIT):
        assert (await upload(limited_client, headers)).status_code == 201
    await age_uploads(db_engine, mentee, minutes=59)

    response = await upload(limited_client, headers)

    assert response.status_code == 429, response.text
    assert 1 <= int(response.headers["retry-after"]) <= 61


async def test_one_persons_limit_does_not_touch_anothers(
    limited_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, first = await a_mentee(db_engine, "rate-first")
    _, second = await a_mentee(db_engine, "rate-second")
    for _ in range(LIMIT):
        assert (await upload(limited_client, first)).status_code == 201
    assert (await upload(limited_client, first)).status_code == 429

    response = await upload(limited_client, second)

    assert response.status_code == 201, response.text


async def test_over_the_limit_retry_after_waits_for_enough_uploads_to_age_out(
    db_engine: AsyncEngine, intake_fake: FakeStorage
) -> None:
    """Three uploads against a limit of two (a lowered setting) free a slot only
    when the *second* oldest leaves the hour, not the oldest."""
    mentee, headers = await a_mentee(db_engine, "rate-over-limit")
    async for client in intake_client(
        db_engine, intake_fake, intake_settings(intake_uploads_per_hour=3)
    ):
        for _ in range(3):
            assert (await upload(client, headers)).status_code == 201
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE intake_files SET created_at = now() - make_interval(mins => m.age) "
                "FROM (SELECT id, (ARRAY[55, 10, 0])[row_number() OVER (ORDER BY created_at, id)] "
                "AS age FROM intake_files WHERE uploader_id = :u) AS m "
                "WHERE intake_files.id = m.id"
            ),
            {"u": mentee},
        )

    async for client in intake_client(
        db_engine, intake_fake, intake_settings(intake_uploads_per_hour=2)
    ):
        response = await upload(client, headers)

    assert response.status_code == 429, response.text
    # The 10-minute-old upload frees the slot in about 50 minutes; the oldest
    # (55 minutes) would wrongly have said about 5.
    assert 2900 <= int(response.headers["retry-after"]) <= 3001
