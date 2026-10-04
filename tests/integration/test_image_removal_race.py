"""An image removal racing an upload of the same bytes must not delete the live object (#320).

Image objects are content-addressed, so the same file uploaded again lands on
the same path. A removal clears the column, commits, then deletes the object;
an upload of identical bytes in between rewrites that object and points the
profile back at it. Without a guard, the removal's clean-up then deletes the
object the profile now points at.
"""

from __future__ import annotations

import asyncio
import threading
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.assets import AssetKind
from app.infra.db.asset_store import clear_image, release_image, store_image
from app.infra.storage.supabase import SupabaseStorage
from conftest import FakeStorage, image_bytes, storage_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

KINDS = [AssetKind.AVATAR, AssetKind.BANNER]


async def make_user(engine: AsyncEngine) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"race-{uuid4().hex[:8]}@example.com", "a": uuid4()},
            )
        ).scalar_one()


async def stored_url(engine: AsyncEngine, user_id: UUID, kind: AssetKind) -> str | None:
    column = "avatar_url" if kind is AssetKind.AVATAR else "banner_url"
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(f"SELECT {column} FROM user_profiles WHERE user_id = :u"),  # noqa: S608
                {"u": user_id},
            )
        ).scalar_one_or_none()


async def upload(
    engine: AsyncEngine, storage: SupabaseStorage, user_id: UUID, kind: AssetKind, payload: bytes
) -> str:
    async with AsyncSession(engine) as session:
        url, _ = await store_image(session, storage, kind, user_id, payload)
        await session.commit()
    return url


def object_exists(fake: FakeStorage, url: str) -> bool:
    return any(url.endswith(path) for path in fake.objects)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_reupload_between_removal_and_cleanup_keeps_the_live_object(
    db_engine: AsyncEngine, kind: AssetKind
) -> None:
    """Removal commits, the same bytes are uploaded again, then the clean-up runs."""
    fake = FakeStorage()
    storage = storage_for(fake)
    payload = image_bytes("JPEG", (300, 300))
    user_id = await make_user(db_engine)
    original = await upload(db_engine, storage, user_id, kind, payload)

    async with AsyncSession(db_engine) as removal:
        previous = await clear_image(removal, user_id, kind)
        await removal.commit()
        again = await upload(db_engine, storage, user_id, kind, payload)
        dropped = await release_image(removal, storage, user_id, kind, previous)

    assert again == original == previous
    assert dropped is False
    assert await stored_url(db_engine, user_id, kind) == original
    assert object_exists(fake, original), "the clean-up deleted the object the profile points at"


class HeldDelete:
    """Holds the storage DELETE open on its thread, and notes any upload write."""

    def __init__(self, fake: FakeStorage) -> None:
        self.deleting, self.release, self.uploading = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        real = fake.handle

        def handle(request):  # type: ignore[no-untyped-def]
            if request.method == "DELETE":
                self.deleting.set()
                self.release.wait(timeout=30)
            elif request.method in ("POST", "PUT") and "/object/" in request.url.path:
                self.uploading.set()
            return real(request)

        fake.handle = handle  # type: ignore[method-assign]
        self.storage = storage_for(fake)


async def wait_until_blocked(engine: AsyncEngine, held: HeldDelete) -> None:
    """Return once the upload is waiting on the image lock; fail if it writes first.

    Deterministic, not a sleep: the proof the upload is held back is a session
    waiting on an advisory lock, and an upload write while the delete is still
    open is the regression itself.
    """
    async with engine.connect() as conn:
        for _ in range(600):
            if held.uploading.is_set():
                pytest.fail("the upload wrote its object while the delete was still in flight")
            waiting = await conn.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            )
            if waiting:
                return
            await asyncio.sleep(0.05)
    pytest.fail("the upload never waited on the image lock")


@pytest.mark.parametrize("kind", KINDS)
async def test_an_upload_racing_the_cleanup_waits_and_lands_after_it(
    db_engine: AsyncEngine, kind: AssetKind
) -> None:
    """The clean-up has checked and is mid-delete; the upload must not slip in between.

    The delete is held open on a thread. An upload of the same bytes started then
    must wait on the image lock, so its object is written after the delete
    rather than before it.
    """
    fake = FakeStorage()
    payload = image_bytes("JPEG", (300, 300))
    user_id = await make_user(db_engine)
    original = await upload(db_engine, storage_for(fake), user_id, kind, payload)
    held = HeldDelete(fake)

    async with AsyncSession(db_engine) as removal:
        previous = await clear_image(removal, user_id, kind)
        await removal.commit()
        cleanup = asyncio.create_task(release_image(removal, held.storage, user_id, kind, previous))
        await asyncio.to_thread(held.deleting.wait, 30)
        racing = asyncio.create_task(upload(db_engine, held.storage, user_id, kind, payload))
        await wait_until_blocked(db_engine, held)
        held.release.set()
        await cleanup
        again = await racing

    assert again == original
    assert await stored_url(db_engine, user_id, kind) == original
    assert object_exists(fake, original), "the upload landed before the delete and was lost"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_cancelled_cleanup_holds_the_lock_until_its_delete_finishes(
    db_engine: AsyncEngine, kind: AssetKind
) -> None:
    """A request cancelled mid-delete must not free the lock while the delete still runs.

    The clean-up owns its session, as a request does, so cancelling it unwinds
    that session and its transaction. An identical upload started then must
    still wait until the in-flight delete has finished.
    """
    fake = FakeStorage()
    payload = image_bytes("JPEG", (300, 300))
    user_id = await make_user(db_engine)
    original = await upload(db_engine, storage_for(fake), user_id, kind, payload)
    held = HeldDelete(fake)

    async with AsyncSession(db_engine) as removal:
        previous = await clear_image(removal, user_id, kind)
        await removal.commit()

    async def request_cleanup() -> None:
        async with AsyncSession(db_engine) as session:
            await release_image(session, held.storage, user_id, kind, previous)

    cleanup = asyncio.create_task(request_cleanup())
    await asyncio.to_thread(held.deleting.wait, 30)
    cleanup.cancel()
    racing = asyncio.create_task(upload(db_engine, held.storage, user_id, kind, payload))
    await wait_until_blocked(db_engine, held)
    held.release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    again = await racing

    assert again == original
    assert object_exists(fake, original), "the cancelled delete removed the re-uploaded object"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deleted_account_does_not_keep_the_object(
    db_engine: AsyncEngine, kind: AssetKind
) -> None:
    """Re-uploaded, then the account deleted: only a live profile keeps an object."""
    fake = FakeStorage()
    storage = storage_for(fake)
    payload = image_bytes("JPEG", (300, 300))
    user_id = await make_user(db_engine)
    original = await upload(db_engine, storage, user_id, kind, payload)

    async with AsyncSession(db_engine) as removal:
        previous = await clear_image(removal, user_id, kind)
        await removal.commit()
        await upload(db_engine, storage, user_id, kind, payload)
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": user_id}
            )
        dropped = await release_image(removal, storage, user_id, kind, previous)

    assert dropped is True
    assert not object_exists(fake, original)


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unreferenced_image_is_still_deleted(
    db_engine: AsyncEngine, kind: AssetKind
) -> None:
    """The guard skips only a live object; a plain removal still cleans up."""
    fake = FakeStorage()
    storage = storage_for(fake)
    user_id = await make_user(db_engine)
    original = await upload(db_engine, storage, user_id, kind, image_bytes("JPEG", (300, 300)))

    async with AsyncSession(db_engine) as removal:
        previous = await clear_image(removal, user_id, kind)
        await removal.commit()
        dropped = await release_image(removal, storage, user_id, kind, previous)

    assert dropped is True
    assert await stored_url(db_engine, user_id, kind) is None
    assert not object_exists(fake, original)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_replaced_image_is_deleted_when_a_different_one_takes_its_place(
    db_engine: AsyncEngine, kind: AssetKind
) -> None:
    """Remove, then upload a different picture: the old object goes, the new one stays."""
    fake = FakeStorage()
    storage = storage_for(fake)
    user_id = await make_user(db_engine)
    original = await upload(db_engine, storage, user_id, kind, image_bytes("JPEG", (300, 300)))

    async with AsyncSession(db_engine) as removal:
        previous = await clear_image(removal, user_id, kind)
        await removal.commit()
        replacement = await upload(
            db_engine, storage, user_id, kind, image_bytes("PNG", (320, 200))
        )
        dropped = await release_image(removal, storage, user_id, kind, previous)

    assert replacement != original
    assert dropped is True
    assert not object_exists(fake, original)
    assert object_exists(fake, replacement)
    assert await stored_url(db_engine, user_id, kind) == replacement
