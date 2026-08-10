"""Asset re-hosting, against a real database and fake hosts.

The fakes model the two behaviours measured against the real export, because
those are the ones that would have broken the run silently:

- Bubble's file host answers ``HEAD`` with **403** and ``GET`` with **200**
- filenames disagree with their contents

The Storage and source adapters are the real ones, driven over
``httpx.MockTransport``. Hand-written stand-ins would test the stand-ins.
"""

import importlib.util
import io
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID

import httpx
import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db.asset_store import AssetStore
from app.infra.storage.source import AssetSource
from conftest import STORAGE_BUCKET as BUCKET
from conftest import SUPABASE_URL as SUPABASE
from conftest import FakeStorage, image_bytes, storage_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUBBLE = "https://app.edufurther.org/version-test/fileupload"

# Real encoded images. `rehost` decodes and re-encodes every asset now — the same
# `process` the upload endpoint uses — so header bytes followed by zeroes are
# refused, which is the point rather than an obstacle.
JPEG = image_bytes("JPEG")
PNG = image_bytes("PNG")


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migrate_assets", PROJECT_ROOT / "scripts" / "migrate_assets.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBubble:
    """Serves the legacy assets, and refuses HEAD exactly as the real host does."""

    def __init__(self, files: dict[str, bytes], *, refuse: set[str] | None = None) -> None:
        self.files = files
        self.refuse = refuse or set()
        self.gets: list[str] = []
        self.heads: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "HEAD":
            # Measured: the real host answers 403 here while GET returns 200.
            self.heads.append(name)
            return httpx.Response(403)
        self.gets.append(name)
        if name in self.refuse:
            return httpx.Response(401)
        if name not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.files[name])


def source_for(fake: FakeBubble) -> AssetSource:
    return AssetSource(
        client=httpx.Client(transport=httpx.MockTransport(fake.handle)),
        sleep=lambda _: None,
    )


#: A timestamp no clock in this suite can produce, standing in for Bubble's
#: Modified Date. Set at INSERT, because `trg_set_updated_at` is BEFORE UPDATE —
#: seeding it with an UPDATE fires the very trigger under test and overwrites the
#: value, which is exactly how the first version of this file reported a failure
#: that was in the test rather than the code.
MIGRATED_AT = datetime(2023, 12, 7, 18, 36, 46, 179000, tzinfo=UTC)


async def seed(
    engine: AsyncEngine, email: str, *, avatar: str | None = None, banner: str | None = None
) -> UUID:
    async with engine.begin() as connection:
        user_id = (
            await connection.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:email, 'Ada', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"email": email},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO user_profiles (user_id, avatar_url, banner_url, updated_at) "
                "VALUES (:u, :a, :b, :at)"
            ),
            {"u": user_id, "a": avatar, "b": banner, "at": MIGRATED_AT},
        )
    return user_id


async def urls(engine: AsyncEngine, user_id: UUID) -> tuple[str | None, str | None]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT avatar_url, banner_url FROM user_profiles WHERE user_id = :u"),
                {"u": user_id},
            )
        ).first()
    assert row is not None
    return row[0], row[1]


# --------------------------------------------------------------------------
# the two measured behaviours
# --------------------------------------------------------------------------


async def test_the_source_is_never_probed_with_head(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    """``app.edufurther.org`` answers HEAD with 403 and the same GET with 200,
    for twelve of the twenty-one dev assets. Anything built on HEAD would report
    most of the collection missing and look like a Bubble outage."""
    bubble = FakeBubble({"a.jpeg": JPEG})
    storage = FakeStorage()
    await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/a.jpeg")

    await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=False,
    )

    assert bubble.heads == [], "the source was probed with HEAD"
    assert bubble.gets == ["a.jpeg"]


async def test_the_stored_content_type_comes_from_the_bytes(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    """A file named ``.gif`` holding JPEG bytes exists in the real export. Trusting
    the name would publish it with a Content-Type a CDN then caches."""
    bubble = FakeBubble({"lying.gif": JPEG})
    storage = FakeStorage()
    user_id = await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/lying.gif")

    await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=False,
    )

    path = storage.uploads[0]
    assert path.endswith(".jpg"), path
    assert storage.objects[path][1] == "image/jpeg"
    avatar, _ = await urls(db_engine, user_id)
    assert avatar is not None and avatar.endswith(".jpg")


# --------------------------------------------------------------------------
# the migration
# --------------------------------------------------------------------------


async def test_a_bubble_image_is_rehosted_and_the_row_repointed(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    bubble = FakeBubble({"a.jpeg": JPEG, "b.png": PNG})
    storage = FakeStorage()
    user_id = await seed(
        db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/a.jpeg", banner=f"{BUBBLE}/f2/b.png"
    )

    report = await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=2,
        dry_run=False,
    )

    assert report.copied == 2
    avatar, banner = await urls(db_engine, user_id)
    for url in (avatar, banner):
        assert url is not None and url.startswith(f"{SUPABASE}/storage/v1/object/public/{BUCKET}/")
    assert f"users/{user_id}/avatar-" in str(avatar)
    assert f"users/{user_id}/banner-" in str(banner)


async def test_no_bubble_url_survives(db_engine: AsyncEngine, script: ModuleType) -> None:
    """The acceptance criterion, asserted by a query — and deliberately **not**
    with ``LIKE '%bubble%'``, which would miss the eleven dev assets served from
    the custom domain."""
    bubble = FakeBubble({"a.jpeg": JPEG})
    storage = FakeStorage()
    await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/a.jpeg")

    await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=False,
    )

    async with db_engine.connect() as connection:
        foreign = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM user_profiles "
                    "WHERE (avatar_url IS NOT NULL AND avatar_url NOT LIKE :ours) "
                    "   OR (banner_url IS NOT NULL AND banner_url NOT LIKE :ours)"
                ),
                {"ours": f"{SUPABASE}/%"},
            )
        ).scalar_one()
    assert foreign == 0


async def test_a_second_run_costs_nothing(db_engine: AsyncEngine, script: ModuleType) -> None:
    """Re-running is how this catches up between now and the freeze, so it has to
    be free once the work is done."""
    bubble = FakeBubble({"a.jpeg": JPEG})
    storage = FakeStorage()
    user_id = await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/a.jpeg")
    store, source, sink = AssetStore(db_engine), source_for(bubble), storage_for(storage)

    await script.migrate(store, source, sink, workers=1, dry_run=False)
    first = await urls(db_engine, user_id)
    bubble.gets.clear()
    storage.uploads.clear()

    report = await script.migrate(store, source, sink, workers=1, dry_run=False)

    assert bubble.gets == [], "a re-run fetched from the source"
    assert storage.uploads == [], "a re-run uploaded"
    assert report.skipped == 1
    assert await urls(db_engine, user_id) == first


async def test_an_asset_the_source_refuses_is_reported_not_fatal(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    """One dev asset answers 401 with an empty body. That user keeps a null image;
    it is not a reason to stop the other 1,199."""
    bubble = FakeBubble({"good.jpeg": JPEG}, refuse={"gone.jpeg"})
    storage = FakeStorage()
    refused = await seed(db_engine, "gone@example.com", avatar=f"{BUBBLE}/f1/gone.jpeg")
    fine = await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f2/good.jpeg")

    report = await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=False,
    )

    assert report.absent == 1
    assert report.copied == 1
    assert (await urls(db_engine, refused))[0] == f"{BUBBLE}/f1/gone.jpeg"
    assert (await urls(db_engine, fine))[0] is not None


async def test_profile_timestamps_are_not_rewritten(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    """``user_profiles.updated_at`` carries Bubble's Modified Date. Moving a file
    is not a modification of the user's data."""
    bubble = FakeBubble({"a.jpeg": JPEG})
    storage = FakeStorage()
    user_id = await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/a.jpeg")

    await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=False,
    )

    async with db_engine.connect() as connection:
        stamp = (
            await connection.execute(
                text("SELECT updated_at FROM user_profiles WHERE user_id = :u"), {"u": user_id}
            )
        ).scalar_one()
    assert stamp.year == 2023, stamp


async def test_a_soft_deleted_user_is_not_migrated(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    bubble = FakeBubble({"a.jpeg": JPEG, "b.jpeg": JPEG})
    storage = FakeStorage()
    gone = await seed(db_engine, "gone@example.com", avatar=f"{BUBBLE}/f1/a.jpeg")
    await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f2/b.jpeg")
    async with db_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": gone}
        )

    report = await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=False,
    )

    # Both halves: the live user proves the run was working when it skipped the
    # deleted one.
    assert bubble.gets == ["b.jpeg"]
    assert report.copied == 1


async def test_a_dry_run_touches_neither_host_nor_database(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    bubble = FakeBubble({"a.jpeg": JPEG})
    storage = FakeStorage()
    user_id = await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/a.jpeg")

    report = await script.migrate(
        AssetStore(db_engine),
        source_for(bubble),
        storage_for(storage),
        workers=1,
        dry_run=True,
    )

    assert report.copied == 1, "the dry run planned nothing"
    assert bubble.gets == []
    assert storage.uploads == []
    assert (await urls(db_engine, user_id))[0] == f"{BUBBLE}/f1/a.jpeg"


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------


def test_a_missing_bucket_is_refused_before_any_work() -> None:
    """1,200 objects failing one at a time on a missing bucket is 1,200 identical
    lines and no headline."""
    from app.infra.storage.supabase import StorageError

    with pytest.raises(StorageError) as raised:
        storage_for(FakeStorage(bucket_exists=False)).ensure_bucket()

    # The *actionable* message, not merely some error. A 404 also satisfies the
    # generic `>= 400` branch, so asserting only the exception type left the
    # specific one deletable with the test still green.
    message = str(raised.value)
    assert BUCKET in message
    assert "PUBLIC" in message and "dashboard" in message, message


def test_the_storage_host_is_derived_not_restated() -> None:
    """``is_ours`` compares against this, so writing the host down a second time
    would be a second representation of one fact."""
    assert storage_for(FakeStorage()).host == "project.supabase.co"


async def test_a_migrated_photo_reaches_storage_without_its_gps(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    """**One rule, both entry points.** ADR 0019 left camera metadata open, and
    the migration is the path that carries most of it — a legacy avatar is a
    phone photo somebody uploaded to Bubble years ago, coordinates included.

    Asserted on the bytes that reached the bucket, and paired with a check that
    the fixture had coordinates to lose: without that half, this passes against
    an image that never carried any.
    """
    source = image_bytes("JPEG", gps=True)
    with Image.open(io.BytesIO(source)) as before:
        assert before.getexif().get_ifd(0x8825), "the fixture carries no GPS to strip"

    bubble = FakeBubble({"holiday.jpeg": source})
    storage = FakeStorage()
    await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/holiday.jpeg")

    await script.migrate(
        AssetStore(db_engine), source_for(bubble), storage_for(storage), workers=1, dry_run=False
    )

    payload, _ = storage.objects[storage.uploads[0]]
    with Image.open(io.BytesIO(payload)) as after:
        assert not after.getexif().get_ifd(0x8825), "GPS survived the migration"
        assert dict(after.getexif()) == {}


async def test_an_asset_the_decoder_will_not_read_is_reported_not_counted(
    db_engine: AsyncEngine, script: ModuleType
) -> None:
    """A refusal must not land in `absent`, which means "there was no image".
    Somebody has to look at these, and a count that hides them is why."""
    bubble = FakeBubble({"broken.jpeg": b"\xff\xd8\xff" + b"\x00" * 64})
    storage = FakeStorage()
    await seed(db_engine, "ada@example.com", avatar=f"{BUBBLE}/f1/broken.jpeg")

    report = await script.migrate(
        AssetStore(db_engine), source_for(bubble), storage_for(storage), workers=1, dry_run=False
    )

    assert report.copied == 0
    assert report.absent == 0, "an unreadable image was counted as having none"
    assert len(report.failed) == 1
    assert storage.uploads == []
