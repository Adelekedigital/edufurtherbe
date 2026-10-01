"""Uploading a profile picture or banner, end to end.

Storage is the real adapter over a mock transport, so the status handling and
the retry are exercised rather than stubbed out. The database is real, because
"the object was uploaded" and "the profile points at it" are two different
claims and this endpoint has to make both true.

**Why an upload endpoint at all, rather than a presigned URL.** A presigned PUT
puts the client in direct contact with the bucket, which means nothing can strip
the camera metadata, nothing can resize, and the object name cannot be derived
from the content — the three things this endpoint exists to do.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor
from tests.integration.test_api_admin import make_user as make_user_with_role

from app.api.limits import MAX_BODY_BYTES
from app.domain.images import MAX_UPLOAD_BYTES
from app.infra.storage.supabase import SupabaseStorage
from conftest import PROBLEM_JSON, FakeStorage, api_token, bearer, image_bytes, storage_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest.fixture
def fake_storage(request: pytest.FixtureRequest) -> FakeStorage:
    """Indirect so one test can ask for a bucket that refuses writes."""
    return FakeStorage(upload_fails=getattr(request, "param", False))


@pytest.fixture
def api_storage(fake_storage: FakeStorage) -> SupabaseStorage:
    """Overrides the conftest default of `None` for this module only."""
    return storage_for(fake_storage)


async def make_user(engine: AsyncEngine, auth_id: UUID, email: str) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()


async def stored_urls(engine: AsyncEngine, user_id: UUID) -> tuple[str | None, str | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT avatar_url, banner_url FROM user_profiles WHERE user_id = :u"),
                {"u": user_id},
            )
        ).first()
    return (None, None) if row is None else (row[0], row[1])


def url(user_id: UUID, kind: str) -> str:
    return f"/api/v1/users/{user_id}/{kind}"


def upload(payload: bytes, *, name: str = "photo.jpg", declared: str = "image/jpeg") -> dict:
    return {"file": (name, payload, declared)}


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "column"), [("avatar", 0), ("banner", 1)])
async def test_an_upload_is_stored_and_the_profile_points_at_it(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fake_storage: FakeStorage,
    kind: str,
    column: int,
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"{kind}@example.com")

    response = await api_client.post(
        url(user_id, kind), files=upload(image_bytes("JPEG")), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200, response.text
    returned = response.json()[f"{kind}_url"]
    assert len(fake_storage.uploads) == 1
    assert fake_storage.uploads[0].startswith(f"users/{user_id}/{kind}-")
    assert (await stored_urls(db_engine, user_id))[column] == returned
    assert returned.endswith(fake_storage.uploads[0])


async def test_only_the_column_that_was_uploaded_moves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both columns live on one row and one statement writes it. A statement that
    set the wrong one would still pass every single-column assertion above."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "one@example.com")

    await api_client.post(
        url(user_id, "avatar"),
        files=upload(image_bytes("JPEG")),
        headers=bearer(api_token(auth_id)),
    )

    avatar, banner = await stored_urls(db_engine, user_id)
    assert avatar is not None
    assert banner is None, "uploading an avatar also wrote the banner"


# --------------------------------------------------------------------------
# what the endpoint exists to do
# --------------------------------------------------------------------------


async def test_the_gps_a_phone_wrote_is_not_in_the_stored_object(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """The requirement ADR 0019 left open, asserted on the **bytes that reached
    storage** rather than on the response. A profile is public; the coordinates a
    photo was taken at are not something a user chose to publish."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "gps@example.com")
    source = image_bytes("JPEG", gps=True)
    with Image.open(io.BytesIO(source)) as check:
        assert check.getexif().get_ifd(0x8825), "the fixture carries no GPS to strip"

    await api_client.post(
        url(user_id, "avatar"), files=upload(source), headers=bearer(api_token(auth_id))
    )

    payload, _ = fake_storage.objects[fake_storage.uploads[0]]
    with Image.open(io.BytesIO(payload)) as stored:
        assert not stored.getexif().get_ifd(0x8825)
        assert dict(stored.getexif()) == {}


async def test_a_large_photo_is_resized_before_it_is_stored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "big@example.com")

    await api_client.post(
        url(user_id, "avatar"),
        files=upload(image_bytes("JPEG", (4000, 3000))),
        headers=bearer(api_token(auth_id)),
    )

    payload, content_type = fake_storage.objects[fake_storage.uploads[0]]
    with Image.open(io.BytesIO(payload)) as stored:
        assert max(stored.size) == 512
    assert content_type == "image/jpeg"


async def test_the_declared_content_type_is_ignored_in_both_directions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """A client can claim anything; magic bytes are a fact. Both directions,
    because a check that read the declared type would pass one of them."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "liar@example.com")
    headers = bearer(api_token(auth_id))

    honest_bytes_dishonest_label = await api_client.post(
        url(user_id, "avatar"),
        files=upload(image_bytes("JPEG"), name="x.gif", declared="image/gif"),
        headers=headers,
    )
    dishonest_bytes_honest_label = await api_client.post(
        url(user_id, "avatar"),
        files=upload(image_bytes("GIF"), name="x.jpg", declared="image/jpeg"),
        headers=headers,
    )

    assert honest_bytes_dishonest_label.status_code == 200, "a real JPEG was refused for its label"
    assert dishonest_bytes_honest_label.status_code == 422, "a GIF passed as a JPEG"
    assert fake_storage.objects[fake_storage.uploads[0]][1] == "image/jpeg"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "what"),
    [
        (b"", "an empty file"),
        (b"just some text, not an image", "a text file"),
        (image_bytes("GIF"), "a GIF"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "an SVG"),
    ],
)
async def test_a_refused_upload_writes_nothing_anywhere(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fake_storage: FakeStorage,
    payload: bytes,
    what: str,
) -> None:
    """The refusal and its two consequences in one assertion: no object, and no
    column. A refusal that still uploaded would leave the bucket growing."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"{len(payload)}@example.com")

    response = await api_client.post(
        url(user_id, "avatar"), files=upload(payload), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 422, f"{what} was accepted: {response.text}"
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert fake_storage.uploads == []
    assert (await stored_urls(db_engine, user_id))[0] is None


async def test_a_body_over_the_cap_is_refused_without_being_decoded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """A **valid** image over the cap, and that is what makes this a test of the
    cap. A mutation found the first version: it sent six megabytes of zeroes
    behind a JFIF header, which the decoder refuses anyway — so removing the
    size limit entirely left the test green. Padding a real JPEG past the marker
    keeps it decodable, and then only the limit can refuse it."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "huge@example.com")
    small = image_bytes("JPEG")
    oversize = small + b"\x00" * (MAX_UPLOAD_BYTES + 1 - len(small))
    assert len(oversize) == MAX_UPLOAD_BYTES + 1

    response = await api_client.post(
        url(user_id, "avatar"), files=upload(oversize), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 422, response.text
    assert fake_storage.uploads == []


async def test_a_body_past_the_request_ceiling_never_reaches_the_route(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """The middleware, not the endpoint — **413 rather than 422**, and that is
    the observable difference between the two limits.

    A check inside the endpoint cannot produce this: by the time a dependency
    runs, the form has been parsed and spooled to disk. The status is what
    proves which one answered.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "absurd@example.com")
    small = image_bytes("JPEG")

    response = await api_client.post(
        url(user_id, "avatar"),
        files=upload(small + b"\x00" * (MAX_BODY_BYTES + 1 - len(small))),
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 413, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert fake_storage.uploads == []


async def test_the_request_ceiling_leaves_room_for_an_image_at_the_image_limit(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A 5 MB file arrives inside a larger request — multipart boundary, part
    headers, filename. If the request ceiling were set to the image limit, a file
    **at** the documented size would be refused for bytes the user never sent."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "edge@example.com")
    # Padded to *exactly* the cap rather than encoded up to it — a decoder
    # ignores bytes after the end-of-image marker, so this is a real image at a
    # size no compression ratio can move. Encoding to a target size depends on
    # the JPEG quality tables and would drift with Pillow.
    small = image_bytes("JPEG")
    payload = small + b"\x00" * (MAX_UPLOAD_BYTES - len(small))
    assert len(payload) == MAX_UPLOAD_BYTES
    assert MAX_UPLOAD_BYTES < MAX_BODY_BYTES, "the request ceiling is below the image limit"

    response = await api_client.post(
        url(user_id, "avatar"), files=upload(payload), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["avatar", "banner"])
async def test_an_upload_without_a_token_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage, kind: str
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"anon-{kind}@example.com")

    response = await api_client.post(url(user_id, kind), files=upload(image_bytes("JPEG")))

    assert response.status_code == 401, response.text
    assert fake_storage.uploads == []


@pytest.mark.parametrize("kind", ["avatar", "banner"])
async def test_one_user_cannot_upload_onto_another(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage, kind: str
) -> None:
    """404 rather than 403, the house rule: "this exists but is not yours" is the
    fact worth withholding."""
    victim_auth, attacker_auth = uuid4(), uuid4()
    victim = await make_user(db_engine, victim_auth, f"victim-{kind}@example.com")
    await make_user(db_engine, attacker_auth, f"attacker-{kind}@example.com")

    response = await api_client.post(
        url(victim, kind),
        files=upload(image_bytes("JPEG")),
        headers=bearer(api_token(attacker_auth)),
    )

    assert response.status_code == 404, response.text
    assert fake_storage.uploads == [], "an object was written for a refused request"
    assert await stored_urls(db_engine, victim) == (None, None)


# --------------------------------------------------------------------------
# replacement
# --------------------------------------------------------------------------


async def test_replacing_an_image_deletes_the_one_it_replaced(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """Paths are per-user and content-addressed, so nothing else can be pointing
    at the object being removed."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "replace@example.com")
    headers = bearer(api_token(auth_id))

    first = await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("JPEG", (300, 300))), headers=headers
    )
    second = await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("PNG", (301, 301))), headers=headers
    )

    old_path, new_path = fake_storage.uploads
    assert first.json()["avatar_url"] != second.json()["avatar_url"]
    assert fake_storage.deletes == [old_path]
    assert old_path not in fake_storage.objects
    assert new_path in fake_storage.objects
    assert (await stored_urls(db_engine, user_id))[0] == second.json()["avatar_url"]


async def test_uploading_the_same_image_twice_deletes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """Content-addressed, so the second upload lands on the same path. Deleting
    "the previous one" there would delete the image the profile now points at —
    the failure mode that makes an unconditional cleanup unsafe."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "same@example.com")
    headers = bearer(api_token(auth_id))
    payload = image_bytes("JPEG", (300, 300))

    await api_client.post(url(user_id, "avatar"), files=upload(payload), headers=headers)
    await api_client.post(url(user_id, "avatar"), files=upload(payload), headers=headers)

    assert fake_storage.deletes == [], "the image the profile points at was deleted"
    path = fake_storage.uploads[0]
    assert fake_storage.uploads == [path, path]
    assert path in fake_storage.objects
    assert (await stored_urls(db_engine, user_id))[0].endswith(path)


async def test_an_avatar_and_a_banner_do_not_delete_each_other(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """Two kinds, one row. A cleanup that read the wrong column would remove the
    image the other endpoint had just stored."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "both@example.com")
    headers = bearer(api_token(auth_id))

    await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("JPEG", (300, 300))), headers=headers
    )
    await api_client.post(
        url(user_id, "banner"), files=upload(image_bytes("JPEG", (600, 400))), headers=headers
    )

    assert fake_storage.deletes == []
    avatar, banner = await stored_urls(db_engine, user_id)
    assert avatar is not None and banner is not None and avatar != banner
    assert len(fake_storage.objects) == 2


@pytest.mark.parametrize("fake_storage", [True], indirect=True)
async def test_a_storage_failure_leaves_the_previous_image_in_place(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """The order the endpoint works in — upload, then repoint — is what makes
    this true. Clearing the column first would leave a profile with no image
    because a bucket had a bad minute."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "down@example.com")
    headers = bearer(api_token(auth_id))
    fake_storage.upload_fails = False
    good = await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("JPEG", (300, 300))), headers=headers
    )
    assert good.status_code == 200, good.text

    fake_storage.upload_fails = True
    response = await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("PNG", (301, 301))), headers=headers
    )

    assert response.status_code >= 500, response.text
    assert fake_storage.deletes == [], "the old image was deleted for a failed replacement"
    assert (await stored_urls(db_engine, user_id))[0] == good.json()["avatar_url"]


async def test_first_write_creates_the_profile_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The pin between `replace_url` and `upsert_profile`.**

    Both create `user_profiles` on first write, in two statements neither can
    share — one inserts the caller's fields, the other one image column. Non-
    negotiable #8 allows a second copy only if a test fails when they diverge,
    and this is it: each entry point is driven against a user who has no row,
    and each must produce one.
    """
    photo_auth, bio_auth = uuid4(), uuid4()
    by_photo = await make_user(db_engine, photo_auth, "photo-first@example.com")
    by_bio = await make_user(db_engine, bio_auth, "bio-first@example.com")

    uploaded = await api_client.post(
        url(by_photo, "avatar"),
        files=upload(image_bytes("JPEG")),
        headers=bearer(api_token(photo_auth)),
    )
    patched = await api_client.patch(
        f"/api/v1/users/{by_bio}/profile",
        json={"about_me": "hello"},
        headers=bearer(api_token(bio_auth)),
    )

    assert uploaded.status_code == 200, uploaded.text
    assert patched.status_code in {200, 204}, patched.text
    assert (await stored_urls(db_engine, by_photo))[0] is not None, (
        "an upload answered 200 for a user whose profile row was never created"
    )
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM user_profiles WHERE user_id = ANY(:ids)"),
            {"ids": [by_photo, by_bio]},
        )
    assert rows.scalar_one() == 2


async def test_the_request_ceiling_applies_to_every_route_not_just_uploads(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Middleware, so it holds for the JSON routes too. A ceiling that only
    covered the endpoint it was written for would leave every other body
    unbounded — which is the failure the endpoint-level check already had."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "json@example.com")

    response = await api_client.patch(
        f"/api/v1/users/{user_id}/profile",
        content=b'{"about_me": "' + b"a" * (MAX_BODY_BYTES + 1) + b'"}',
        headers={**bearer(api_token(auth_id)), "content-type": "application/json"},
    )

    assert response.status_code == 413, response.text


async def test_a_body_with_no_declared_length_is_still_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A chunked request carries no `Content-Length`, so the declared check has
    nothing to compare — and the first version of this let it through in full.

    **The failure it left open is worse here than on an upload.** This is JSON,
    and `request.body()` accumulates JSON in memory, so an unbounded chunked
    `PATCH` costs the worker's RAM rather than its disk.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "chunked@example.com")

    async def chunks():
        yield b'{"about_me": "'
        for _ in range((MAX_BODY_BYTES // (64 * 1024)) + 1):
            yield b"a" * (64 * 1024)
        yield b'"}'

    response = await api_client.patch(
        f"/api/v1/users/{user_id}/profile",
        content=chunks(),
        headers={**bearer(api_token(auth_id)), "content-type": "application/json"},
    )

    assert "content-length" not in response.request.headers, "httpx declared a length"
    assert response.status_code == 413, response.text


async def test_a_small_streamed_body_still_works(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Streamed bodies are **limited, not banned**. Requiring a length on every
    route was the easy fix and would refuse every chunked client — so this is
    the test that stops the limit from being tightened into a ban."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "small-stream@example.com")

    async def chunks():
        yield b'{"about_me": "streamed"}'

    response = await api_client.patch(
        f"/api/v1/users/{user_id}/profile",
        content=chunks(),
        headers={**bearer(api_token(auth_id)), "content-type": "application/json"},
    )

    assert "content-length" not in response.request.headers
    assert response.status_code in {200, 204}, response.text
    async with db_engine.connect() as conn:
        about = await conn.execute(
            text("SELECT about_me FROM user_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert about.scalar_one() == "streamed", "the streamed body was refused or truncated"


async def test_a_chunked_upload_over_the_ceiling_stores_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """The upload half of the same hole: no declared length, so nothing compared,
    and the multipart parser would spool every byte to disk before the endpoint
    got a say."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "chunked-upload@example.com")
    boundary = b"----probe"
    head = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="big.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n"
    )

    async def chunks():
        yield head
        for _ in range((MAX_BODY_BYTES // (64 * 1024)) + 1):
            yield b"\x00" * (64 * 1024)
        yield b"\r\n--" + boundary + b"--\r\n"

    response = await api_client.post(
        url(user_id, "avatar"),
        content=chunks(),
        headers={
            **bearer(api_token(auth_id)),
            "content-type": f"multipart/form-data; boundary={boundary.decode()}",
        },
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413, response.text
    assert fake_storage.uploads == []
    assert (await stored_urls(db_engine, user_id))[0] is None


# --------------------------------------------------------------------------
# the avatar's focal point (#180)
# --------------------------------------------------------------------------

FACE = (Path(__file__).parents[1] / "fixtures" / "faces" / "nasa-portrait.jpg").read_bytes()


async def focus_of(engine: AsyncEngine, user_id: UUID) -> tuple[object, object, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT avatar_focus_x, avatar_focus_y, avatar_focus_source "
                    "FROM user_profiles WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).one()
    return row[0], row[1], row[2]


async def choose_focus(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, avatar_focus_x, avatar_focus_y, "
                " avatar_focus_source) VALUES (:u, 0.1, 0.9, 'chosen') "
                "ON CONFLICT (user_id) DO UPDATE SET avatar_focus_x = 0.1, "
                " avatar_focus_y = 0.9, avatar_focus_source = 'chosen'"
            ),
            {"u": user_id},
        )


async def test_an_avatar_with_a_face_is_stored_with_its_focal_point(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Found in the re-encoded image that is served, as fractions of it — the
    portrait's face sits a little right of centre, in the upper third."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "face@example.com")

    response = await api_client.post(
        url(user_id, "avatar"), files=upload(FACE), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200, response.text
    x, y, source = await focus_of(db_engine, user_id)
    assert source == "detected"
    assert 0.45 < x < 0.6  # type: ignore[operator]
    assert 0.28 < y < 0.42  # type: ignore[operator]


async def test_an_avatar_without_a_face_is_looked_at_and_has_no_point(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`detected` with no point: the backfill must not look at it again."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "noface@example.com")

    response = await api_client.post(
        url(user_id, "avatar"),
        files=upload(image_bytes("JPEG")),
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 200, response.text
    assert await focus_of(db_engine, user_id) == (None, None, "detected")


async def test_a_new_avatar_replaces_a_chosen_focus(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A chosen crop was a crop of the *old* picture."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "rechosen@example.com")
    await choose_focus(db_engine, user_id)

    await api_client.post(
        url(user_id, "avatar"), files=upload(FACE), headers=bearer(api_token(auth_id))
    )

    _, _, source = await focus_of(db_engine, user_id)
    assert source == "detected"


async def test_a_banner_upload_leaves_the_avatar_focus_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "banner-focus@example.com")
    await choose_focus(db_engine, user_id)

    await api_client.post(
        url(user_id, "banner"), files=upload(FACE), headers=bearer(api_token(auth_id))
    )

    assert await focus_of(db_engine, user_id) == (Decimal("0.1"), Decimal("0.9"), "chosen")


async def test_a_detector_failure_still_stores_the_avatar_and_leaves_it_unprocessed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort by contract: the photo is saved, and "unknown" is left for the
    backfill — never recorded as "no face", a claim nobody checked."""
    from app.infra.images import faces

    def broken(_payload: bytes) -> None:
        raise ValueError("detector blew up")

    monkeypatch.setattr(faces, "detect_faces", broken)
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "detector-down@example.com")

    response = await api_client.post(
        url(user_id, "avatar"), files=upload(FACE), headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200, response.text
    assert (await stored_urls(db_engine, user_id))[0] is not None
    assert await focus_of(db_engine, user_id) == (None, None, None)


async def test_the_same_photo_again_keeps_a_chosen_focus(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Object paths are content hashes, so the same file is the same URL — the
    picture did not change, and neither should the mentor's crop of it."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "same-photo@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)
    await choose_focus(db_engine, user_id)

    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)

    assert await focus_of(db_engine, user_id) == (Decimal("0.1"), Decimal("0.9"), "chosen")


async def test_the_upload_response_carries_the_new_focal_point(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """So the client can crop the photo it just uploaded without refetching."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "upload-focus@example.com")

    body = (
        await api_client.post(
            url(user_id, "avatar"), files=upload(FACE), headers=bearer(api_token(auth_id))
        )
    ).json()

    assert 0.45 < body["avatar_focus"]["x"] < 0.6
    assert 0.28 < body["avatar_focus"]["y"] < 0.42


async def test_the_upload_response_reports_what_is_stored_not_what_was_detected(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same photo again keeps a chosen crop, so that is what comes back."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "upload-focus-kept@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)
    await choose_focus(db_engine, user_id)

    body = (
        await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)
    ).json()

    assert body["avatar_focus"] == pytest.approx({"x": 0.1, "y": 0.9})


# --------------------------------------------------------------------------
# removing the banner
# --------------------------------------------------------------------------


async def test_removing_the_banner_clears_it_and_deletes_the_object(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """The design's "Remove image": the cover falls back to its colour and art."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "unbanner@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("JPEG", (300, 300))), headers=headers
    )
    await api_client.post(
        url(user_id, "banner"), files=upload(image_bytes("JPEG", (600, 400))), headers=headers
    )
    avatar_path, banner_path = fake_storage.uploads

    response = await api_client.delete(url(user_id, "banner"), headers=headers)

    assert response.status_code == 204, response.text
    avatar, banner = await stored_urls(db_engine, user_id)
    assert banner is None
    assert avatar is not None and avatar.endswith(avatar_path), "the avatar was touched"
    assert fake_storage.deletes == [banner_path]
    assert avatar_path in fake_storage.objects


async def test_removing_a_banner_that_is_not_there_is_a_quiet_204(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """Idempotent: a double tap, or a user with no profile row yet, is not an error."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "nobanner@example.com")

    response = await api_client.delete(url(user_id, "banner"), headers=bearer(api_token(auth_id)))

    assert response.status_code == 204, response.text
    assert fake_storage.deletes == []
    assert await stored_urls(db_engine, user_id) == (None, None)


async def test_one_user_cannot_remove_another_users_banner(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    victim_auth, attacker_auth = uuid4(), uuid4()
    victim = await make_user(db_engine, victim_auth, "victim-unbanner@example.com")
    await make_user(db_engine, attacker_auth, "attacker-unbanner@example.com")
    await api_client.post(
        url(victim, "banner"),
        files=upload(image_bytes("JPEG", (600, 400))),
        headers=bearer(api_token(victim_auth)),
    )

    anonymous = await api_client.delete(url(victim, "banner"))
    response = await api_client.delete(
        url(victim, "banner"), headers=bearer(api_token(attacker_auth))
    )

    assert anonymous.status_code == 401, anonymous.text
    assert response.status_code == 404, response.text
    assert fake_storage.deletes == []
    assert (await stored_urls(db_engine, victim))[1] is not None


async def test_an_admin_cannot_remove_someone_elses_banner(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """Owner only, as the upload is: an admin role is no licence to edit a profile."""
    owner_auth, admin_auth = uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner-unbanner@example.com")
    await make_user_with_role(
        db_engine, admin_auth, "admin-unbanner@example.com", role="super_admin"
    )
    await api_client.post(
        url(owner, "banner"),
        files=upload(image_bytes("JPEG", (600, 400))),
        headers=bearer(api_token(owner_auth)),
    )

    response = await api_client.delete(url(owner, "banner"), headers=bearer(api_token(admin_auth)))

    assert response.status_code == 404, response.text
    assert fake_storage.deletes == []
    assert (await stored_urls(db_engine, owner))[1] is not None


async def test_a_legacy_banner_is_cleared_but_nothing_is_deleted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """A URL this bucket did not build names no object here to delete."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "legacy-unbanner@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, banner_url) "
                "VALUES (:u, 'https://legacy.bubble.io/banner.jpg')"
            ),
            {"u": user_id},
        )

    response = await api_client.delete(url(user_id, "banner"), headers=bearer(api_token(auth_id)))

    assert response.status_code == 204, response.text
    assert (await stored_urls(db_engine, user_id))[1] is None
    assert fake_storage.deletes == []


@pytest.mark.parametrize("failure", ["status", "transport"])
async def test_a_cleanup_that_fails_after_the_commit_is_still_a_204(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage, failure: str
) -> None:
    """The banner is already gone when the delete runs; a bucket that errors or
    never answers leaves an orphan, and must not turn a done change into a 500."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"{failure}-unbanner@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(
        url(user_id, "banner"), files=upload(image_bytes("JPEG", (600, 400))), headers=headers
    )
    fake_storage.delete_fails = failure

    response = await api_client.delete(url(user_id, "banner"), headers=headers)

    assert response.status_code == 204, response.text
    assert (await stored_urls(db_engine, user_id))[1] is None
    assert len(fake_storage.deletes) >= 1


# --------------------------------------------------------------------------
# removing the photo (FE #98): the avatar's twin of the banner removal
# --------------------------------------------------------------------------


async def auth_of(engine: AsyncEngine, user_id: UUID) -> UUID:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": user_id})
        ).scalar_one()


async def test_removing_the_photo_clears_it_its_focus_and_the_object(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """The banner is a different column and a different object: both survive."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "unavatar@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)
    await api_client.post(
        url(user_id, "banner"), files=upload(image_bytes("JPEG", (600, 400))), headers=headers
    )
    avatar_path, banner_path = fake_storage.uploads
    assert (await focus_of(db_engine, user_id))[2] == "detected"

    response = await api_client.delete(url(user_id, "avatar"), headers=headers)

    assert response.status_code == 204, response.text
    avatar, banner = await stored_urls(db_engine, user_id)
    assert avatar is None
    assert banner is not None and banner.endswith(banner_path), "the banner was touched"
    assert await focus_of(db_engine, user_id) == (None, None, None)
    assert fake_storage.deletes == [avatar_path]
    assert banner_path in fake_storage.objects


async def test_removing_a_photo_that_is_not_there_is_a_quiet_204(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "noavatar@example.com")

    response = await api_client.delete(url(user_id, "avatar"), headers=bearer(api_token(auth_id)))

    assert response.status_code == 204, response.text
    assert fake_storage.deletes == []
    assert await stored_urls(db_engine, user_id) == (None, None)


async def test_only_the_owner_may_remove_the_photo(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """404, not 403, for another user and for an admin alike; 401 with no token."""
    owner_auth, other_auth, admin_auth = uuid4(), uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner-unavatar@example.com")
    await make_user(db_engine, other_auth, "other-unavatar@example.com")
    await make_user_with_role(
        db_engine, admin_auth, "admin-unavatar@example.com", role="super_admin"
    )
    await api_client.post(
        url(owner, "avatar"), files=upload(FACE), headers=bearer(api_token(owner_auth))
    )

    anonymous = await api_client.delete(url(owner, "avatar"))
    other = await api_client.delete(url(owner, "avatar"), headers=bearer(api_token(other_auth)))
    admin = await api_client.delete(url(owner, "avatar"), headers=bearer(api_token(admin_auth)))

    assert anonymous.status_code == 401, anonymous.text
    assert other.status_code == 404, other.text
    assert admin.status_code == 404, admin.text
    assert fake_storage.deletes == []
    assert (await stored_urls(db_engine, owner))[0] is not None
    assert (await focus_of(db_engine, owner))[2] == "detected"


async def test_a_legacy_photo_is_cleared_but_nothing_is_deleted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "legacy-unavatar@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, avatar_url, avatar_focus_x, "
                " avatar_focus_y, avatar_focus_source) "
                "VALUES (:u, 'https://legacy.bubble.io/me.jpg', 0.5, 0.4, 'detected')"
            ),
            {"u": user_id},
        )

    response = await api_client.delete(url(user_id, "avatar"), headers=bearer(api_token(auth_id)))

    assert response.status_code == 204, response.text
    assert (await stored_urls(db_engine, user_id))[0] is None
    assert await focus_of(db_engine, user_id) == (None, None, None)
    assert fake_storage.deletes == []


@pytest.mark.parametrize("failure", ["status", "transport"])
async def test_a_photo_cleanup_that_fails_after_the_commit_is_still_a_204(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage, failure: str
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"{failure}-unavatar@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)
    fake_storage.delete_fails = failure

    response = await api_client.delete(url(user_id, "avatar"), headers=headers)

    assert response.status_code == 204, response.text
    assert (await stored_urls(db_engine, user_id))[0] is None
    assert len(fake_storage.deletes) >= 1


async def test_a_new_photo_after_removal_gets_its_own_detected_focus(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A chosen crop of the removed photo must not carry over to the next one."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "rephoto@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(
        url(user_id, "avatar"), files=upload(image_bytes("JPEG", (300, 300))), headers=headers
    )
    await choose_focus(db_engine, user_id)

    await api_client.delete(url(user_id, "avatar"), headers=headers)
    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)

    x, y, source = await focus_of(db_engine, user_id)
    assert source == "detected"
    assert (x, y) != (0.1, 0.9)


async def test_me_reads_no_photo_after_removal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "me-unavatar@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "avatar"), files=upload(FACE), headers=headers)

    await api_client.delete(url(user_id, "avatar"), headers=headers)

    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    assert me["profile"]["avatar_url"] is None
    assert me["profile"]["avatar_focus"] is None


async def test_the_public_profile_reads_no_photo_after_removal(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "unavatar-read")
    headers = bearer(api_token(await auth_of(db_engine, mentor)))
    await api_client.post(url(mentor, "avatar"), files=upload(FACE), headers=headers)
    before = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()
    assert before["avatar_url"] is not None and before["avatar_focus"] is not None

    await api_client.delete(url(mentor, "avatar"), headers=headers)

    public = (await api_client.get(f"/api/v1/mentors/{mentor}")).json()
    assert public["avatar_url"] is None
    assert public["avatar_focus"] is None


#: A legacy (non-bucket) image of each kind, as fixed SQL rather than a built string.
LEGACY_IMAGE = {
    "avatar": "INSERT INTO user_profiles (user_id, avatar_url) VALUES (:u, :url)",
    "banner": "INSERT INTO user_profiles (user_id, banner_url) VALUES (:u, :url)",
}


@pytest.mark.parametrize("kind", ["avatar", "banner"])
async def test_removal_needs_no_storage_when_there_is_nothing_to_delete(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A deployment without storage settings still answers the idempotent 204:
    the URL is cleared and committed first, and a legacy URL names nothing in a
    bucket this deployment could ever have written to."""
    from app.api.deps import profile
    from app.core.errors import ConfigurationError

    def unconfigured() -> object:
        raise ConfigurationError("Supabase storage is not configured")

    api_client._transport.app.state.storage = None  # type: ignore[attr-defined]
    monkeypatch.setattr(profile, "get_storage", unconfigured)
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, f"nostorage-{kind}@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(LEGACY_IMAGE[kind]),
            {"u": user_id, "url": f"https://legacy.bubble.io/{kind}.jpg"},
        )
    headers = bearer(api_token(auth_id))

    removed = await api_client.delete(url(user_id, kind), headers=headers)
    again = await api_client.delete(url(user_id, kind), headers=headers)

    assert removed.status_code == 204, removed.text
    assert again.status_code == 204, again.text
    column = 0 if kind == "avatar" else 1
    assert (await stored_urls(db_engine, user_id))[column] is None


@pytest.mark.parametrize("kind", ["avatar", "banner"])
async def test_an_owner_deleted_after_auth_has_nothing_cleared(
    db_engine: AsyncEngine, kind: str
) -> None:
    """`clear_image` scopes its read and write to the LIVE owner in SQL, so an
    account soft-deleted between `OwnerDep` and the write is refused (404), and
    its profile keeps its image."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.errors import NotFoundError
    from app.domain.assets import AssetKind
    from app.infra.db.asset_store import clear_image

    user_id = await make_user(db_engine, uuid4(), f"gone-{kind}@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(LEGACY_IMAGE[kind]),
            {"u": user_id, "url": f"https://legacy.bubble.io/{kind}.jpg"},
        )
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": user_id}
        )

    async with AsyncSession(db_engine) as session:
        with pytest.raises(NotFoundError):
            await clear_image(session, user_id, AssetKind(kind))
        await session.rollback()

    column = 0 if kind == "avatar" else 1
    assert (await stored_urls(db_engine, user_id))[column] is not None
