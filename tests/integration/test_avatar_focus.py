"""The avatar focal point on the read side, and the backfill for older avatars.

Detection on upload is tested with the upload endpoint (`test_api_uploads.py`).
Here: the point reaches the discovery card and the public profile, and the
backfill fills avatars stored before focal points existed — without ever
overwriting a mentor's choice, a newer photo, or guessing about a picture it
could not read.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import make_bookable_mentor

from app.infra.db.avatar_focus_store import backfill_avatar_focus, record_focus
from conftest import FakeStorage, storage_for

pytestmark = [pytest.mark.db, pytest.mark.anyio]

FACE = (Path(__file__).parents[1] / "fixtures" / "faces" / "nasa-portrait.jpg").read_bytes()


async def set_profile(engine: AsyncEngine, user: UUID, **columns: object) -> None:
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    updates = ", ".join(f"{name} = excluded.{name}" for name in columns)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO user_profiles (user_id, {names}) VALUES (:u, {params}) "  # noqa: S608
                f"ON CONFLICT (user_id) DO UPDATE SET {updates}"
            ),
            {"u": user, **columns},
        )


async def focus_of(engine: AsyncEngine, user: UUID) -> tuple[object, object, object]:
    async with engine.connect() as conn:
        return tuple(
            (
                await conn.execute(
                    text(
                        "SELECT avatar_focus_x, avatar_focus_y, avatar_focus_source "
                        "FROM user_profiles WHERE user_id = :u"
                    ),
                    {"u": user},
                )
            ).one()
        )  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Read side
# --------------------------------------------------------------------------


async def test_the_card_and_the_profile_carry_the_focal_point(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "focus-read")
    await set_profile(
        db_engine,
        mentor,
        avatar_url="https://cdn.example/a.jpg",
        avatar_focus_x=0.52,
        avatar_focus_y=0.35,
        avatar_focus_source="detected",
    )

    card = (await api_client.get("/api/v1/mentors")).json()["data"][0]
    profile = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()

    assert card["avatar_focus"] == pytest.approx({"x": 0.52, "y": 0.35})
    assert profile["avatar_focus"] == pytest.approx({"x": 0.52, "y": 0.35})


async def test_no_point_is_null_so_the_client_uses_its_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "focus-none")
    await set_profile(db_engine, mentor, avatar_focus_source="detected")

    card = (await api_client.get("/api/v1/mentors")).json()["data"][0]

    assert card["avatar_focus"] is None


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


async def run_backfill(engine: AsyncEngine, fake: FakeStorage) -> dict[str, int]:
    async with AsyncSession(engine) as session:
        return await backfill_avatar_focus(session, storage_for(fake))


def stored(fake: FakeStorage, path: str, payload: bytes) -> str:
    fake.objects[path] = (payload, "image/jpeg")
    return storage_for(fake).public_url(path)


async def test_an_old_avatar_gets_its_focal_point(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "focus-backfill")
    fake = FakeStorage()
    await set_profile(db_engine, mentor, avatar_url=stored(fake, f"users/{mentor}/a.jpg", FACE))

    counts = await run_backfill(db_engine, fake)

    assert counts["focused"] == 1
    x, y, source = await focus_of(db_engine, mentor)
    assert source == "detected"
    assert 0.45 < x < 0.6  # type: ignore[operator]
    assert 0.28 < y < 0.42  # type: ignore[operator]


async def test_a_chosen_focus_is_never_overwritten(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "focus-chosen")
    fake = FakeStorage()
    await set_profile(
        db_engine,
        mentor,
        avatar_url=stored(fake, f"users/{mentor}/a.jpg", FACE),
        avatar_focus_x=0.1,
        avatar_focus_y=0.9,
        avatar_focus_source="chosen",
    )

    await run_backfill(db_engine, fake)

    assert await focus_of(db_engine, mentor) == (Decimal("0.1"), Decimal("0.9"), "chosen")


async def test_an_avatar_that_is_not_ours_is_skipped_not_guessed(db_engine: AsyncEngine) -> None:
    """A legacy link: nobody looked at it, so it must stay unprocessed rather
    than be recorded as having no face."""
    mentor = await make_bookable_mentor(db_engine, "focus-foreign")
    await set_profile(db_engine, mentor, avatar_url="https://legacy.example/old.jpg")

    counts = await run_backfill(db_engine, FakeStorage())

    assert counts["skipped"] == 1
    assert await focus_of(db_engine, mentor) == (None, None, None)


async def test_an_unreadable_avatar_is_left_for_a_rerun(db_engine: AsyncEngine) -> None:
    mentor = await make_bookable_mentor(db_engine, "focus-missing")
    fake = FakeStorage()
    await set_profile(
        db_engine, mentor, avatar_url=storage_for(fake).public_url(f"users/{mentor}/gone.jpg")
    )

    counts = await run_backfill(db_engine, fake)

    assert counts["unreadable"] == 1
    assert await focus_of(db_engine, mentor) == (None, None, None)


async def test_a_point_for_an_old_photo_is_not_written_onto_a_new_one(
    db_engine: AsyncEngine,
) -> None:
    """The mentor uploaded a new photo while the backfill was looking at the old."""
    mentor = await make_bookable_mentor(db_engine, "focus-raced")
    await set_profile(db_engine, mentor, avatar_url="https://cdn.example/new.jpg")

    async with AsyncSession(db_engine) as session:
        written = await record_focus(session, mentor, "https://cdn.example/old.jpg", (0.5, 0.5))
        await session.commit()

    assert written is False
    assert await focus_of(db_engine, mentor) == (None, None, None)


async def test_a_choice_made_while_the_backfill_ran_is_kept(db_engine: AsyncEngine) -> None:
    """The query skips chosen rows, so this is the write-side guard for a mentor
    who chose a crop after the backfill read the row."""
    mentor = await make_bookable_mentor(db_engine, "focus-chosen-race")
    await set_profile(
        db_engine,
        mentor,
        avatar_url="https://cdn.example/a.jpg",
        avatar_focus_x=0.1,
        avatar_focus_y=0.9,
        avatar_focus_source="chosen",
    )

    async with AsyncSession(db_engine) as session:
        written = await record_focus(session, mentor, "https://cdn.example/a.jpg", (0.5, 0.5))
        await session.commit()

    assert written is False
    assert await focus_of(db_engine, mentor) == (Decimal("0.1"), Decimal("0.9"), "chosen")
