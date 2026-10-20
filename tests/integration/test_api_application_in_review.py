"""A submitted application's reviewed fields hold still until the decision
(owner, 2026-10-18).

Refused with 409 `/problems/mentor-application-in-review` and `fields`; only a
change counts, and approve or decline unlocks.
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

TYPE = "/problems/mentor-application-in-review"

#: What reopens an application's fields: not yet submitted, or decided.
OPENED = {
    "unsubmitted": "UPDATE mentor_profiles SET submitted_at = NULL WHERE user_id = :u",
    "approved": "UPDATE mentor_profiles SET approval_status = 'approved' WHERE user_id = :u",
    "declined": "UPDATE mentor_profiles SET approval_status = 'declined' WHERE user_id = :u",
}


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def api_storage(fake_storage: FakeStorage) -> SupabaseStorage:
    """A bucket for this module, so an upload let through can complete."""
    return storage_for(fake_storage)


async def submitted(engine: AsyncEngine, tag: str) -> tuple[UUID, dict[str, str]]:
    """A complete application, submitted and pending."""
    mentor = await make_applicant(engine, tag)
    await run_sql(
        engine, "UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u", {"u": mentor}
    )
    return mentor, bearer(await token_for(engine, mentor))


async def an_entry(engine: AsyncEngine, mentor: UUID) -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text("SELECT id FROM education_entries WHERE user_id = :u"), {"u": mentor}
                    )
                ).scalar_one()
            )
        )


def refused(response: httpx.Response, fields: list[str]) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["type"] == TYPE
    assert response.json()["fields"] == fields


# --------------------------------------------------------------------------
# Education: every write changes it
# --------------------------------------------------------------------------


async def test_education_cannot_be_added_changed_or_removed_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-edu")
    entry = await an_entry(db_engine, mentor)
    base = f"/api/v1/users/{mentor}/education"

    added = await api_client.post(base, headers=auth, json={"school_name_raw": "Lagos"})
    changed = await api_client.patch(f"{base}/{entry}", headers=auth, json={"study_course": "Law"})
    removed = await api_client.delete(f"{base}/{entry}", headers=auth)

    for response in (added, changed, removed):
        refused(response, ["education"])
    async with db_engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT count(*), max(study_course) FROM education_entries WHERE user_id = :u"),
            {"u": mentor},
        )
        assert tuple(rows.one()) == (1, "Data Science")


@pytest.mark.parametrize("state", ["unsubmitted", "approved", "declined"])
async def test_education_is_writable_before_submitting_and_after_the_decision(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, state: str
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-open-{state}")
    await run_sql(db_engine, OPENED[state], {"u": mentor})

    response = await api_client.post(
        f"/api/v1/users/{mentor}/education", headers=auth, json={"school_name_raw": "Lagos"}
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# Profile and mentor profile: only a change is refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"social_linkedin": None}, ["social_linkedin"]),
        ({"about_me": "A new bio"}, ["about_me"]),
        (
            {"about_me": "A new bio", "social_linkedin": "", "cover_art": "icons"},
            ["social_linkedin", "about_me", "cover_art"],
        ),
    ],
    ids=["linkedin-cleared", "about", "several"],
)
async def test_a_profile_write_changing_a_reviewed_field_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, str], fields: list[str]
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-prof-{fields[-1]}-{len(fields)}")

    response = await api_client.patch(f"/api/v1/users/{mentor}/profile", headers=auth, json=body)

    refused(response, fields)


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Europe/London"},
        {"primary_role": "mentee"},
        # The stored link resent in another spelling is not a change.
        {"social_linkedin": "linkedin.com/in/ada", "timezone": "Europe/London"},
        # A corrected link is allowed in review (2026-10-19); only clearing is not.
        {"social_linkedin": "linkedin.com/in/ada-lovelace"},
        {"social_twitter": "@ada", "social_youtube": "@ada"},
        {"first_name": "Bo", "gender": "female"},
    ],
    ids=["timezone", "primary-role", "same-linkedin", "new-linkedin", "socials", "name"],
)
async def test_a_profile_write_that_leaves_reviewed_fields_alone_saves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, body: dict[str, str]
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-ok-{sorted(body)[0]}-{len(body)}")

    response = await api_client.patch(f"/api/v1/users/{mentor}/profile", headers=auth, json=body)

    assert response.status_code == 204, response.text


async def test_a_mentor_profile_write_changing_a_reviewed_field_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-mp")
    url = f"/api/v1/users/{mentor}/mentor-profile"
    other = await _offering(db_engine, "document-preparation")

    headline = await api_client.patch(url, headers=auth, json={"headline": "New"})
    offerings = await api_client.patch(url, headers=auth, json={"offering_ids": [str(other)]})
    country = await api_client.patch(
        url, headers=auth, json={"primary_study_country_id": str(await _country(db_engine, "US"))}
    )

    refused(headline, ["headline"])
    refused(offerings, ["offering_ids"])
    refused(country, ["primary_study_country_id"])


async def test_a_mentor_profile_write_that_leaves_reviewed_fields_alone_saves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-mp-ok")
    same = await _offering(db_engine, "test-preparation")

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={
            "requires_booking_confirmation": True,
            "headline": "MSc Data Science · Oxford",
            "offering_ids": [str(same)],
        },
    )

    assert response.status_code == 200, response.text


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


async def test_a_legacy_stored_link_may_be_corrected_but_not_cleared(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Only clearing is refused (decision 273 as amended), whatever the stored
    link's spelling: a correction of a legacy link saves, and a clear of it does not."""
    mentor, auth = await submitted(db_engine, "frz-legacy")
    await run_sql(
        db_engine,
        "UPDATE user_profiles SET social_linkedin = 'linkedin.com/in/ada/' WHERE user_id = :u",
        {"u": mentor},
    )
    url = f"/api/v1/users/{mentor}/profile"

    cleared = await api_client.patch(url, headers=auth, json={"social_linkedin": ""})
    corrected = await api_client.patch(url, headers=auth, json={"social_linkedin": "ada-l"})

    refused(cleared, ["social_linkedin"])
    assert corrected.status_code == 204, corrected.text


@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_stored_link_cleared_is_not_a_change(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, blank: str
) -> None:
    """A legacy blank is no link, as a write would store it, so clearing it
    removes nothing the reviewer has."""
    mentor, auth = await submitted(db_engine, f"frz-blank-{len(blank)}")
    await run_sql(
        db_engine,
        "UPDATE user_profiles SET social_linkedin = :b WHERE user_id = :u",
        {"u": mentor, "b": blank},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile", headers=auth, json={"social_linkedin": None}
    )

    assert response.status_code == 204, response.text


async def test_a_programme_change_and_a_redraft_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-prog")

    programme = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={"primary_study_program": "Law"},
    )
    redraft = await api_client.patch(
        f"/api/v1/users/{mentor}/profile",
        headers=auth,
        json={
            "about_me": "A drafted bio",
            "about_me_drafted": True,
            "about_me_prompt_version": "bio_draft-v1",
        },
    )

    refused(programme, ["primary_study_program"])
    refused(redraft, ["about_me"])


async def test_stored_text_is_compared_the_way_a_write_cleans_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A legacy headline with trailing space, resent trimmed, is not a change."""
    mentor, auth = await submitted(db_engine, "frz-space")
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET headline = 'MSc Data Science · Oxford  ' WHERE user_id = :u",
        {"u": mentor},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/mentor-profile",
        headers=auth,
        json={"headline": "MSc Data Science · Oxford", "requires_booking_confirmation": True},
    )

    assert response.status_code == 200, response.text


async def test_clearing_a_stored_link_that_is_not_canonical_is_still_a_change(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A legacy value the rule cannot read is kept as itself, so it cannot be
    cleared out from under the reviewer."""
    mentor, auth = await submitted(db_engine, "frz-oddlink")
    await run_sql(
        db_engine,
        "UPDATE user_profiles SET social_linkedin = 'https://linkedin.test/ada' WHERE user_id = :u",
        {"u": mentor},
    )

    response = await api_client.patch(
        f"/api/v1/users/{mentor}/profile", headers=auth, json={"social_linkedin": None}
    )

    refused(response, ["social_linkedin"])


async def test_an_edit_racing_a_submit_waits_for_it_and_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The race the lock order closes.** A submit holds the mentor row; an edit
    started meanwhile waits on it, then sees the application submitted and is
    refused, instead of reading "not yet submitted" and landing after."""
    mentor, auth = await submitted(db_engine, "frz-race")
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
        edit = asyncio.create_task(
            api_client.patch(
                f"/api/v1/users/{mentor}/mentor-profile", headers=auth, json={"headline": "New"}
            )
        )
        await until_blocked(db_engine)
        await submit.execute(
            text("UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )
        await transaction.commit()

    refused(await edit, ["headline"])


# --------------------------------------------------------------------------
# The settings page's fields (Mentor Settings PRD PR 2, owner 2026-10-19)
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


async def _guide(engine: AsyncEngine, mentor: UUID, codes: list[str]) -> list[str]:
    """Store `codes` as the mentor's guide countries, in order; their ids as sent."""
    ids = [str(await _country(engine, code)) for code in codes]
    for position, country in enumerate(ids):
        await run_sql(
            engine,
            "INSERT INTO mentor_guide_countries (mentor_user_id, country_id, position) "
            "VALUES (:u, :c, :p)",
            {"u": mentor, "c": country, "p": position},
        )
    return ids


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


#: Every newly frozen write, and the one field its refusal names.
FROZEN_WRITES: dict[str, tuple[Write, str]] = {
    "avatar-upload": (_upload_avatar, "avatar"),
    "avatar-removal": (_remove_avatar, "avatar"),
    "avatar-focus": (_profile({"avatar_focus": {"x": 0.1, "y": 0.9}}), "avatar"),
    "banner-upload": (_upload_banner, "banner"),
    "banner-removal": (_remove_banner, "banner"),
    "cover-color": (_profile({"cover_color": "mint"}), "cover_color"),
    "cover-art": (_profile({"cover_art": "icons"}), "cover_art"),
    "origin-country": (_origin, "origin_country_id"),
    "languages": (_languages, "languages"),
    "guide-countries": (_guide_countries, "guide_country_ids"),
    "award-added": (_add_award, "awards"),
    "award-changed": (_change_award, "awards"),
    "award-removed": (_remove_award, "awards"),
}


@pytest.mark.parametrize("write", FROZEN_WRITES)
async def test_each_settings_write_is_refused_while_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, write: str
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-set-{write}")
    send, field = FROZEN_WRITES[write]

    refused(await send(api_client, db_engine, mentor, auth), [field])


@pytest.mark.parametrize("state", ["unsubmitted", "approved"])
@pytest.mark.parametrize("write", FROZEN_WRITES)
async def test_each_settings_write_saves_before_submitting_and_after_approval(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, write: str, state: str
) -> None:
    mentor, auth = await submitted(db_engine, f"frz-sok-{write}-{state}")
    await run_sql(db_engine, OPENED[state], {"u": mentor})
    send, _ = FROZEN_WRITES[write]

    response = await send(api_client, db_engine, mentor, auth)

    assert response.is_success, response.text


@pytest.mark.parametrize("kind", ["avatar", "banner"])
async def test_an_upload_in_review_is_refused_and_leaves_nothing_behind(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage, kind: str
) -> None:
    """The bytes are stored before the check (no lock is held through storage),
    so a refusal deletes what it stored and the profile never points at it."""
    mentor, auth = await submitted(db_engine, f"frz-bytes-{kind}")

    response = await api_client.post(users(mentor, kind), headers=auth, files=a_photo())

    refused(response, [kind])
    assert len(fake_storage.uploads) == 1
    assert fake_storage.objects == {}
    assert await stored_images(db_engine, mentor) == ("https://cdn.example.test/a.png", None)


async def test_a_refused_upload_of_the_photo_already_shown_keeps_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    """Objects are content-addressed, so the same file uploaded again is the
    live photo's own object: the refusal's clean-up must not delete it."""
    mentor, auth = await submitted(db_engine, "frz-same-photo")
    await run_sql(db_engine, OPENED["unsubmitted"], {"u": mentor})
    first = await api_client.post(users(mentor, "avatar"), headers=auth, files=a_photo())
    assert first.status_code == 200, first.text
    await run_sql(
        db_engine,
        "UPDATE mentor_profiles SET submitted_at = now() WHERE user_id = :u",
        {"u": mentor},
    )

    again = await api_client.post(users(mentor, "avatar"), headers=auth, files=a_photo())

    refused(again, ["avatar"])
    assert list(fake_storage.objects) == [fake_storage.uploads[0]]
    assert (await stored_images(db_engine, mentor))[0] == first.json()["avatar_url"]


async def test_removing_an_image_in_review_keeps_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, fake_storage: FakeStorage
) -> None:
    mentor, auth = await submitted(db_engine, "frz-keep")

    refused(await _remove_banner(api_client, db_engine, mentor, auth), ["banner"])
    refused(await _remove_avatar(api_client, db_engine, mentor, auth), ["avatar"])

    assert fake_storage.deletes == []
    assert await stored_images(db_engine, mentor) == (
        "https://cdn.example.test/a.png",
        "https://cdn.example.test/b.png",
    )


async def test_a_cover_colour_resent_unchanged_beside_other_edits_saves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-cover-same")
    await run_sql(
        db_engine, "UPDATE user_profiles SET cover_color = 'sky' WHERE user_id = :u", {"u": mentor}
    )

    response = await api_client.patch(
        users(mentor, "profile"),
        headers=auth,
        json={"cover_color": "sky", "cover_art": "none", "timezone": "Europe/London"},
    )

    assert response.status_code == 204, response.text


async def test_guide_countries_reordered_are_refused_and_resent_in_order_save(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-guide-order")
    stored = await _guide(db_engine, mentor, ["US", "CA"])
    url = users(mentor, "mentor-profile")

    reordered = await api_client.patch(url, headers=auth, json={"guide_country_ids": stored[::-1]})
    same = await api_client.patch(
        url,
        headers=auth,
        json={"guide_country_ids": stored, "requires_booking_confirmation": True},
    )

    refused(reordered, ["guide_country_ids"])
    assert same.status_code == 200, same.text


async def test_languages_resent_as_stored_save_and_a_new_proficiency_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Omitted proficiency and primary keep what is stored, so resending only the
    ids is the same list; changing how well one is spoken is not."""
    mentor, auth = await submitted(db_engine, "frz-lang-same")
    english = str(await _language(db_engine, "English"))
    await run_sql(
        db_engine,
        "INSERT INTO user_languages (user_id, language_id, proficiency, is_primary) "
        "VALUES (:u, :l, 'native', true)",
        {"u": mentor, "l": english},
    )
    url = users(mentor, "languages")

    same = await api_client.put(url, headers=auth, json={"languages": [{"language_id": english}]})
    changed = await api_client.put(
        url, headers=auth, json={"languages": [{"language_id": english, "proficiency": "fluent"}]}
    )

    assert same.status_code == 204, same.text
    refused(changed, ["languages"])


async def test_a_corrected_linkedin_link_is_saved_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth = await submitted(db_engine, "frz-li-edit")

    response = await api_client.patch(
        users(mentor, "profile"), headers=auth, json={"social_linkedin": "ada-lovelace"}
    )

    assert response.status_code == 204, response.text
    async with db_engine.connect() as conn:
        stored = await conn.execute(
            text("SELECT social_linkedin FROM user_profiles WHERE user_id = :u"), {"u": mentor}
        )
        assert stored.scalar_one() == "https://www.linkedin.com/in/ada-lovelace"


async def test_every_link_stays_open_in_review(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`user_links` is not reviewed (decision 273 as amended): add, change and
    delete all save while the application is in review."""
    _, auth = await submitted(db_engine, "frz-links")

    added = await api_client.post(
        "/api/v1/me/links", headers=auth, json={"platform": "instagram", "value": "ada.l"}
    )
    changed = await api_client.patch(
        f"/api/v1/me/links/{added.json()['id']}", headers=auth, json={"value": "ada.lovelace"}
    )
    deleted = await api_client.delete(f"/api/v1/me/links/{added.json()['id']}", headers=auth)

    assert (added.status_code, changed.status_code, deleted.status_code) == (201, 200, 204)


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
    """The writer's own validation runs before the freeze, so the client gets
    the same answer for a bad value whatever the review state."""
    mentor, auth = await submitted(db_engine, f"frz-422-{pointer.split('/')[1]}")
    await run_sql(
        db_engine, "UPDATE user_profiles SET avatar_url = NULL WHERE user_id = :u", {"u": mentor}
    )

    response = await api_client.patch(users(mentor, path), headers=auth, json=body)

    assert response.status_code == 422, response.text
    assert pointer in response.text
