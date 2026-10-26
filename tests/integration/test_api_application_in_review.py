"""A mentor application in review locks nothing on the profile (owner, 2026-10-26;
decision 296, superseding 273): the reviewer sees the profile as it is now.

Every write the freeze refused now saves while the application is in review.
Only the help-area guard holds: an application in review keeps one, as an
approved mentor does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.application_factories import make_applicant, run_sql
from tests.integration.factories import token_for, until_blocked

from app.infra.storage.supabase import SupabaseStorage
from conftest import FakeStorage, bearer, image_bytes, storage_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def api_storage(fake_storage: FakeStorage) -> SupabaseStorage:
    """A bucket for this module, so an upload can complete."""
    return storage_for(fake_storage)


async def submitted(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    """A complete application, submitted and pending."""
    mentor = await make_applicant(engine, tag)
    await run_sql(
        engine, "UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u", {"u": mentor}
    )
    return mentor, bearer(await token_for(engine, mentor))


async def one(engine: AsyncEngine, sql: str, mentor: UUID) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), {"u": mentor})).scalar_one()


async def _offering(engine: AsyncEngine, slug: str) -> UUID:
    async with engine.begin() as conn:
        found = await conn.execute(
            text("SELECT id FROM service_offerings WHERE slug = :s"), {"s": slug}
        )
        return UUID(str(found.scalar_one()))


async def _country(engine: AsyncEngine, code: str) -> UUID:
    async with engine.begin() as conn:
        found = await conn.execute(text("SELECT id FROM countries WHERE code = :c"), {"c": code})
        return UUID(str(found.scalar_one()))


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------


async def test_education_can_be_added_changed_and_removed_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "rev-edu")
    entry = await one(db_engine, "SELECT id FROM education_entries WHERE user_id = :u", mentor)
    base = f"/api/v1/users/{mentor}/education"

    added = await api_client.post(base, headers=auth, json={"school_name_raw": "Lagos"})
    changed = await api_client.patch(f"{base}/{entry}", headers=auth, json={"study_course": "Law"})
    removed = await api_client.delete(f"{base}/{entry}", headers=auth)

    assert (added.status_code, changed.status_code, removed.status_code) == (201, 200, 204)
    live = "SELECT count(*) FROM education_entries WHERE user_id = :u AND deleted_at IS NULL"
    assert await one(db_engine, live, mentor) == 1


# --------------------------------------------------------------------------
# Profile and mentor profile fields
# --------------------------------------------------------------------------

ABOUT = "SELECT about_me FROM user_profiles WHERE user_id = :u"
LINKEDIN = "SELECT social_linkedin FROM user_profiles WHERE user_id = :u"


@pytest.mark.parametrize(
    ("body", "read", "stored"),
    [
        ({"headline": "New"}, "SELECT headline FROM mentor_profiles WHERE user_id = :u", "New"),
        (
            {"primary_study_program": "Law"},
            "SELECT primary_study_program FROM mentor_profiles WHERE user_id = :u",
            "Law",
        ),
    ],
    ids=["headline", "programme"],
)
async def test_a_mentor_profile_field_saves_while_in_review(
    request: pytest.FixtureRequest,
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    body: dict[str, str],
    read: str,
    stored: str,
) -> None:
    mentor, auth = await submitted(db_engine, f"rev-mp-{request.node.callspec.id}")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile", headers=auth, json=body
    )

    assert response.status_code == 200, response.text
    assert await one(db_engine, read, mentor) == stored


async def test_help_areas_and_study_country_change_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "rev-mp-ids")
    other = await _offering(db_engine, "document-preparation")
    country = await _country(db_engine, "US")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={"offering_ids": [str(other)], "primary_study_country_id": str(country)},
    )

    assert response.status_code == 200, response.text
    held = "SELECT service_offering_id FROM mentor_service_offerings WHERE mentor_user_id = :u"
    assert await one(db_engine, held, mentor) == other
    study = "SELECT primary_study_country_id FROM mentor_profiles WHERE user_id = :u"
    assert await one(db_engine, study, mentor) == country


async def test_clearing_every_help_area_in_review_is_a_422_and_writes_nothing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Approval does not re-check the application, so the help area submit
    asked for is kept, as an approved mentor keeps theirs (decision 292)."""
    mentor, auth = await submitted(db_engine, "rev-mp-clear")
    held = await _offering(db_engine, "test-preparation")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={"offering_ids": [], "headline": "Changed"},
    )

    assert response.status_code == 422, response.text
    assert "/offering_ids" in response.text
    sql = "SELECT service_offering_id FROM mentor_service_offerings WHERE mentor_user_id = :u"
    assert await one(db_engine, sql, mentor) == held
    headline = "SELECT headline FROM mentor_profiles WHERE user_id = :u"
    assert await one(db_engine, headline, mentor) != "Changed"


async def test_a_clear_racing_a_submit_waits_for_it_and_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The race the lock order closes.** A submit holds the mentor row; a
    clear started meanwhile waits on it, then sees the application in review,
    instead of reading "draft" and landing after submit's help-area check."""
    mentor, auth = await submitted(db_engine, "rev-mp-race")
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET submitted_at = NULL WHERE user_id = :u",
        {"u": mentor},
    )
    async with db_engine.connect() as submit:
        transaction = await submit.begin()
        await submit.execute(
            text("SELECT 1 FROM mentor_profiles WHERE user_id = :u FOR UPDATE"), {"u": mentor}
        )
        clear = asyncio.create_task(
            api_client.patch(
                f"/api/v1/users/{mentor}/mentor-profile", headers=auth, json={"offering_ids": []}
            )
        )
        await until_blocked(db_engine)
        await submit.execute(
            text("UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )
        await transaction.commit()

    response = await clear
    assert response.status_code == 422, response.text
    assert "/offering_ids" in response.text


async def test_a_draft_may_still_clear_its_help_areas(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "rev-mp-draft")
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET submitted_at = NULL WHERE user_id = :u",
        {"u": mentor},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile", headers=auth, json={"offering_ids": []}
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("body", "read", "stored"),
    [
        ({"about_me": "A new bio"}, ABOUT, "A new bio"),
        ({"social_linkedin": None}, LINKEDIN, None),
        (
            {"social_linkedin": "ada-lovelace"},
            LINKEDIN,
            "https://www.linkedin.com/in/ada-lovelace",
        ),
        (
            {"cover_color": "mint"},
            "SELECT cover_color FROM user_profiles WHERE user_id = :u",
            "mint",
        ),
        ({"cover_art": "icons"}, "SELECT cover_art FROM user_profiles WHERE user_id = :u", "icons"),
    ],
    ids=["about", "linkedin-cleared", "linkedin-corrected", "cover-color", "cover-art"],
)
async def test_a_profile_field_saves_while_in_review(
    request: pytest.FixtureRequest,
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    body: dict[str, str | None],
    read: str,
    stored: str | None,
) -> None:
    """LinkedIn included: the in-review rule that it could be corrected but not
    removed went with the freeze."""
    mentor, auth = await submitted(db_engine, f"rev-prof-{request.node.callspec.id}")

    response = await api_client.patch(f"/api/v1/users/{mentor}/profile", headers=auth, json=body)

    assert response.status_code == 204, response.text
    assert await one(db_engine, read, mentor) == stored


async def test_a_redrafted_bio_saves_with_its_provenance_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "rev-redraft")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile",
        headers=auth,
        json={
            "about_me": "A drafted bio",
            "about_me_drafted": True,
            "about_me_prompt_version": "bio_draft-v1",
        },
    )

    assert response.status_code == 204, response.text
    sql = "SELECT about_me_prompt_version FROM user_profiles WHERE user_id = :u"
    assert await one(db_engine, sql, mentor) == "bio_draft-v1"


async def test_languages_change_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An omitted proficiency still keeps what is stored; a sent one replaces it."""
    mentor, auth = await submitted(db_engine, "rev-lang")
    english = str(await _language(db_engine, "English"))
    await run_sql(
        db_engine,
        "INSERT INTO user_languages (user_id, language_id, proficiency, is_primary) "
        "VALUES (:u, :l, 'native', true)",
        {"u": mentor, "l": english},
    )
    url = f"/api/v1/users/{mentor}/languages"
    proficiency = "SELECT proficiency FROM user_languages WHERE user_id = :u"

    kept = await api_client.put(url, headers=auth, json={"languages": [{"language_id": english}]})
    assert kept.status_code == 204, kept.text
    assert await one(db_engine, proficiency, mentor) == "native"

    changed = await api_client.put(
        url, headers=auth, json={"languages": [{"language_id": english, "proficiency": "fluent"}]}
    )
    assert changed.status_code == 204, changed.text
    assert await one(db_engine, proficiency, mentor) == "fluent"


# --------------------------------------------------------------------------
# The settings page's writes (Mentor Settings PRD PR 2)
# --------------------------------------------------------------------------

Write = Callable[[httpx.AsyncClient, AsyncEngine, UUID, dict[str, str]], Awaitable[httpx.Response]]


def users(mentor: UUID, path: str) -> str:
    return f"/api/v1/users/{mentor}/{path}"


def a_photo() -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("photo.jpg", image_bytes("JPEG"), "image/jpeg")}


async def stored_images(engine: AsyncEngine, mentor: UUID) -> tuple[str | None, str | None]:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT avatar_url, banner_url FROM user_profiles WHERE user_id = :u"),
            {"u": mentor},
        )
        avatar, banner = row.one()
        return avatar, banner


async def _language(engine: AsyncEngine, name: str) -> UUID:
    async with engine.begin() as conn:
        found = await conn.execute(
            text("SELECT id FROM languages WHERE display_name = :n"), {"n": name}
        )
        return UUID(str(found.scalar_one()))


async def _award(engine: AsyncEngine, mentor: UUID) -> UUID:
    async with engine.begin() as conn:
        found = await conn.execute(
            text(
                "INSERT INTO user_awards (user_id, institution, title) "
                "VALUES (:u, 'FCDO', 'Chevening') RETURNING id"
            ),
            {"u": mentor},
        )
        return UUID(str(found.scalar_one()))


async def _upload_avatar(
    client: httpx.AsyncClient, _: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    return await client.post(users(mentor, "avatar"), headers=auth, files=a_photo())


async def _remove_avatar(
    client: httpx.AsyncClient, _: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    return await client.delete(users(mentor, "avatar"), headers=auth)


async def _upload_banner(
    client: httpx.AsyncClient, _: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    return await client.post(users(mentor, "banner"), headers=auth, files=a_photo())


async def _remove_banner(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    await run_sql(
        engine,
        "UPDATE user_profiles SET banner_url = 'https://cdn.example.test/b.png' WHERE user_id = :u",
        {"u": mentor},
    )
    return await client.delete(users(mentor, "banner"), headers=auth)


def _profile(body: dict[str, object]) -> Write:
    async def write(
        client: httpx.AsyncClient, _: AsyncEngine, mentor: UUID, auth: dict[str, str]
    ) -> httpx.Response:
        return await client.patch(users(mentor, "profile"), headers=auth, json=body)

    return write


async def _origin(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    body = {"origin_country_id": str(await _country(engine, "US"))}
    return await client.patch(users(mentor, "profile"), headers=auth, json=body)


async def _languages(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    body = {"languages": [{"language_id": str(await _language(engine, "English"))}]}
    return await client.put(users(mentor, "languages"), headers=auth, json=body)


async def _guide_countries(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    body = {"guide_country_ids": [str(await _country(engine, "US"))]}
    return await client.patch(users(mentor, "mentor-profile"), headers=auth, json=body)


async def _add_award(
    client: httpx.AsyncClient, _: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    body = {"title": "Commonwealth", "institution": "CSC"}
    return await client.post(users(mentor, "awards"), headers=auth, json=body)


async def _change_award(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    award = await _award(engine, mentor)
    return await client.patch(users(mentor, f"awards/{award}"), headers=auth, json={"year": 2020})


async def _remove_award(
    client: httpx.AsyncClient, engine: AsyncEngine, mentor: UUID, auth: dict[str, str]
) -> httpx.Response:
    award = await _award(engine, mentor)
    return await client.delete(users(mentor, f"awards/{award}"), headers=auth)


#: Every write the freeze refused from the settings page.
SETTINGS_WRITES: dict[str, Write] = {
    "avatar-upload": _upload_avatar,
    "avatar-removal": _remove_avatar,
    "avatar-focus": _profile({"avatar_focus": {"x": 0.1, "y": 0.9}}),
    "banner-upload": _upload_banner,
    "banner-removal": _remove_banner,
    "origin-country": _origin,
    "languages": _languages,
    "guide-countries": _guide_countries,
    "award-added": _add_award,
    "award-changed": _change_award,
    "award-removed": _remove_award,
}


@pytest.mark.parametrize("write", SETTINGS_WRITES)
async def test_each_settings_write_saves_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, write: str
) -> None:
    mentor, auth = await submitted(db_engine, f"rev-set-{write}")

    response = await SETTINGS_WRITES[write](api_client, db_engine, mentor, auth)

    assert response.is_success, response.text


@pytest.mark.parametrize("kind", ["avatar", "banner"])
async def test_an_upload_in_review_is_what_the_profile_shows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage, kind: str
) -> None:
    mentor, auth = await submitted(db_engine, f"rev-bytes-{kind}")

    response = await api_client.post(users(mentor, kind), headers=auth, files=a_photo())

    assert response.status_code == 200, response.text
    avatar, banner = await stored_images(db_engine, mentor)
    assert (avatar if kind == "avatar" else banner) == response.json()[f"{kind}_url"]
    assert len(fake_storage.objects) == 1


async def test_removing_images_in_review_clears_them(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "rev-clear")

    banner = await _remove_banner(api_client, db_engine, mentor, auth)
    avatar = await _remove_avatar(api_client, db_engine, mentor, auth)

    assert (banner.status_code, avatar.status_code) == (204, 204)
    assert await stored_images(db_engine, mentor) == (None, None)


@pytest.mark.parametrize(
    ("path", "body", "pointer"),
    [
        (
            "profile",
            {"origin_country_id": "00000000-0000-7000-8000-000000000000"},
            "/origin_country_id",
        ),
        (
            "mentor-profile",
            {"guide_country_ids": ["00000000-0000-7000-8000-000000000000"]},
            "/guide_country_ids/0",
        ),
        ("profile", {"avatar_focus": {"x": 0.5, "y": 0.5}}, "/avatar_focus"),
    ],
    ids=["origin", "guide", "crop-without-photo"],
)
async def test_an_invalid_value_is_a_422_in_review_too(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    path: str,
    body: dict[str, object],
    pointer: str,
) -> None:
    mentor, auth = await submitted(db_engine, f"rev-422-{pointer.split('/')[1]}")
    await run_sql(
        db_engine, "UPDATE user_profiles SET avatar_url = NULL WHERE user_id = :u", {"u": mentor}
    )

    response = await api_client.patch(users(mentor, path), headers=auth, json=body)

    assert response.status_code == 422, response.text
    assert pointer in response.text
