"""Writing a user's education, goals, awards, mentor profile and profile.

**This file exists for the refusals, and for one asymmetry.** A read that leaks
shows somebody data; a write that leaks *changes* it, and there is no version to
compare against afterwards. So every write endpoint has its own refusal test —
not a parametrised sweep, because a decorator covering eight of nine reads as
complete and the ninth is the one that ships.

The asymmetry: **an admin may read these records and may not write them.** That
is one clause of difference between `TargetUserDep` and `OwnerDep`, and nothing
but a test says which endpoint got which.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def make_user(engine: AsyncEngine, auth_id: UUID, email: str, *, admin: bool = False) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()
        if admin:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user_id},
            )
    return user_id


async def add_institution(engine: AsyncEngine, name: str, domain: str) -> UUID:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO institutions (name, domain, source) "
                    "VALUES (:n, :d, 'hipolabs') RETURNING id"
                ),
                {"n": name, "d": domain},
            )
        ).scalar_one()


def url(user_id: UUID, suffix: str) -> str:
    return f"/api/v1/users/{user_id}/{suffix}"


# --------------------------------------------------------------------------
# Refusals — one per write endpoint
# --------------------------------------------------------------------------

#: Every write, as (method, path suffix, body). A new endpoint added without a
#: refusal test is a line missing from here, which is visible.
WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("post", "education", {"school_name_raw": "Somewhere"}),
    ("patch", "education/{id}", {"study_course": "Law"}),
    ("delete", "education/{id}", {}),
    ("put", "goal", {"notes": "hello"}),
    ("delete", "goal", {}),
    ("post", "awards", {"title": "T", "institution": "I"}),
    ("patch", "awards/{id}", {"title": "changed"}),
    ("delete", "awards/{id}", {}),
    ("post", "mentor-profile", {"headline": "hi"}),
    ("patch", "mentor-profile", {"headline": "changed"}),
    ("patch", "profile", {"about_me": "changed"}),
]


@pytest.mark.parametrize(
    ("method", "suffix", "body"), WRITES, ids=[f"{m}-{s}" for m, s, _ in WRITES]
)
async def test_another_users_records_cannot_be_written(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    suffix: str,
    body: dict[str, Any],
) -> None:
    """Parametrised **only** because the target is the same dependency on every
    route. The admin case below is the one that varies, and it is written out."""
    owner_auth, caller_auth = uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner@example.com")
    await make_user(db_engine, caller_auth, "caller@example.com")

    path = url(owner, suffix.replace("{id}", str(uuid4())))
    # `request` rather than `api_client.delete(...)`: httpx's delete helper takes
    # no `json=`, and this table carries a body for most rows.
    response = await api_client.request(
        method.upper(), path, json=body or None, headers=bearer(api_token(caller_auth))
    )

    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    ("method", "suffix", "body"), WRITES, ids=[f"{m}-{s}" for m, s, _ in WRITES]
)
async def test_an_admin_may_not_write(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    suffix: str,
    body: dict[str, Any],
) -> None:
    """**The asymmetry this PR introduces.**

    A live admin reads these records — `test_an_admin_may_read_another_users_education`
    proves that still works. Writing is one clause different in the dependency,
    and only this says so. An admin editing somebody's education silently is an
    audit trail nobody has designed.
    """
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "admin@example.com", admin=True)
    target = await make_user(db_engine, uuid4(), "member@example.com")

    path = url(target, suffix.replace("{id}", str(uuid4())))
    response = await api_client.request(
        method.upper(), path, json=body or None, headers=bearer(api_token(admin_auth))
    )

    assert response.status_code == 404, f"an admin wrote {suffix}: {response.text}"


async def test_a_write_without_a_token_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    owner = await make_user(db_engine, uuid4(), "owner@example.com")

    response = await api_client.post(url(owner, "education"), json={"school_name_raw": "X"})

    assert response.status_code == 401


async def test_another_users_record_id_under_my_own_url_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The leak the refusal tests cannot see.**

    Every refusal above is stopped by `OwnerDep` at the URL, so the statement's
    own `user_id` scope is never exercised — a mutation removing it survived.
    Here the URL *is* mine and only the record id is someone else's, so the
    dependency passes and the `WHERE` clause is the only thing left.
    """
    mine_auth = uuid4()
    mine = await make_user(db_engine, mine_auth, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        their_entry = (
            await conn.execute(
                text(
                    "INSERT INTO education_entries (user_id, school_name_raw, study_course) "
                    "VALUES (:u, 'Theirs', 'Untouched') RETURNING id"
                ),
                {"u": theirs},
            )
        ).scalar_one()

    patched = await api_client.patch(
        url(mine, f"education/{their_entry}"),
        json={"study_course": "Hijacked"},
        headers=bearer(api_token(mine_auth)),
    )
    deleted = await api_client.delete(
        url(mine, f"education/{their_entry}"), headers=bearer(api_token(mine_auth))
    )

    assert (patched.status_code, deleted.status_code) == (404, 404)
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT study_course, deleted_at FROM education_entries WHERE id = :i"),
            {"i": their_entry},
        )
        course, deleted_at = row.one()
    assert course == "Untouched", "another user's record was edited"
    assert deleted_at is None, "another user's record was deleted"


async def test_clearing_my_goal_does_not_clear_anybody_elses(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The goal delete takes no id — it targets "the caller's goal" — so the
    `user_id` in its `WHERE` is the *only* thing scoping it. A mutation widening
    that clause wiped every goal in the table and no test noticed."""
    mine_auth = uuid4()
    mine = await make_user(db_engine, mine_auth, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentee_goals (user_id, notes) VALUES (:m, 'Mine'), (:t, 'Theirs')"),
            {"m": mine, "t": theirs},
        )

    response = await api_client.delete(url(mine, "goal"), headers=bearer(api_token(mine_auth)))

    assert response.status_code == 204
    async with db_engine.connect() as conn:
        remaining = await conn.execute(text("SELECT notes FROM mentee_goals"))
    assert [row[0] for row in remaining] == ["Theirs"], "another user's goal was destroyed"


async def test_an_empty_patch_is_not_an_existence_oracle_for_education(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The one branch the earlier mutation batch never reached.**

    A `PATCH` carrying no recognised field takes a different path: it answers
    "does this row exist and is it yours" with a `SELECT` rather than an
    `UPDATE`. That `SELECT` is scoped in the code and nothing held it — removing
    `user_id` left all 570 tests green, while an empty body at another user's id
    returned 200 and a random uuid returned 404.

    Which is precisely the distinction the house rule forbids: not-found and
    not-yours are the same answer, because telling them apart hands anyone with
    a token an oracle over ids they do not own. The patch models are all
    optional, so `{}` validates and reaches here over HTTP.
    """
    mine_auth = uuid4()
    mine = await make_user(db_engine, mine_auth, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        their_entry = (
            await conn.execute(
                text(
                    "INSERT INTO education_entries (user_id, school_name_raw) "
                    "VALUES (:u, 'Theirs') RETURNING id"
                ),
                {"u": theirs},
            )
        ).scalar_one()

    real = await api_client.patch(
        url(mine, f"education/{their_entry}"), json={}, headers=bearer(api_token(mine_auth))
    )
    absent = await api_client.patch(
        url(mine, f"education/{uuid4()}"), json={}, headers=bearer(api_token(mine_auth))
    )

    assert real.status_code == 404, "an empty patch confirmed another user's entry exists"
    assert real.status_code == absent.status_code
    assert real.json() == absent.json(), "a real id and a made-up one answered differently"


async def test_an_empty_patch_is_not_an_existence_oracle_for_awards(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same branch on the other writer. Written out rather than
    parametrised: two functions, two `SELECT`s, either of which could
    individually lose its scope."""
    mine_auth = uuid4()
    mine = await make_user(db_engine, mine_auth, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        their_award = (
            await conn.execute(
                text(
                    "INSERT INTO user_awards (user_id, institution, title) "
                    "VALUES (:u, 'A Body', 'Theirs') RETURNING id"
                ),
                {"u": theirs},
            )
        ).scalar_one()

    real = await api_client.patch(
        url(mine, f"awards/{their_award}"), json={}, headers=bearer(api_token(mine_auth))
    )
    absent = await api_client.patch(
        url(mine, f"awards/{uuid4()}"), json={}, headers=bearer(api_token(mine_auth))
    )

    assert real.status_code == 404, "an empty patch confirmed another user's award exists"
    assert real.status_code == absent.status_code
    assert real.json() == absent.json(), "a real id and a made-up one answered differently"


# --------------------------------------------------------------------------
# Education, and the institution transaction
# --------------------------------------------------------------------------


async def test_a_known_school_links_without_creating_an_institution(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    await add_institution(db_engine, "University of Lagos", "unilag.edu.ng")

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "University of Lagos"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201
    assert response.json()["institution_created"] is False
    assert response.headers["Location"].endswith(response.json()["id"])

    async with db_engine.connect() as conn:
        count = await conn.execute(text("SELECT count(*) FROM institutions"))
    assert count.scalar_one() == 1, "a duplicate institution was created for a known school"


async def test_an_unlisted_school_creates_a_pending_institution(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The create-on-write path, and the `created_by` invariant behind it.

    A `CHECK` refuses a `manual` row with no creator, so this also proves the
    caller is the one recorded — there is no other value the write could use.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Some Unlisted Polytechnic"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201
    assert response.json()["institution_created"] is True

    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT status, source, created_by FROM institutions "
                "WHERE name = 'Some Unlisted Polytechnic'"
            )
        )
        status_value, source, created_by = row.one()

    assert (str(status_value), source) == ("LookupStatus.PENDING_REVIEW", "manual") or (
        str(status_value),
        source,
    ) == ("pending_review", "manual")
    assert created_by == user_id, "created_by is not the caller"


async def test_an_ambiguous_name_is_queued_never_linked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`City University` is a real university in three countries.

    Linking one would file the degree in the wrong country, silently and
    permanently, and the country of study derives from it. So the write queues a
    new pending row instead of choosing — the same rule the ETL follows.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    us = await add_institution(db_engine, "City University", "cityu.edu")
    gb = await add_institution(db_engine, "City University", "city.ac.uk")

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "City University"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201
    assert response.json()["institution_created"] is True

    async with db_engine.connect() as conn:
        linked = await conn.execute(
            text("SELECT institution_id FROM education_entries WHERE user_id = :u"), {"u": user_id}
        )
        institution_id = linked.scalar_one()

    assert institution_id not in {us, gb}, "an ambiguous name was linked to one of the candidates"


async def test_a_supplied_institution_id_is_validated_not_trusted(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A merged row is not selectable in search, so it must not be attachable by
    id either — otherwise the id is a way around every rule search enforces."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    target = await add_institution(db_engine, "Real University", "real.edu")
    async with db_engine.begin() as conn:
        merged = (
            await conn.execute(
                text(
                    "INSERT INTO institutions (name, domain, source, merged_into_id) "
                    "VALUES ('Reel University', 'reel.edu', 'hipolabs', :t) RETURNING id"
                ),
                {"t": target},
            )
        ).scalar_one()

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Reel University", "institution_id": str(merged)},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201
    async with db_engine.connect() as conn:
        linked = await conn.execute(
            text("SELECT institution_id FROM education_entries WHERE user_id = :u"), {"u": user_id}
        )
    assert linked.scalar_one() != merged, "a merged institution was attached by id"


async def test_a_second_most_recent_clears_the_first(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`ix_education_entries_one_most_recent` is a unique partial index, so
    getting the order wrong is a 500 on an ordinary edit rather than a silent
    duplicate. The clear has to happen first."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))

    first = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "First School", "is_most_recent": True},
        headers=headers,
    )
    second = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Second School", "is_most_recent": True},
        headers=headers,
    )

    assert (first.status_code, second.status_code) == (201, 201)
    async with db_engine.connect() as conn:
        flagged = await conn.execute(
            text(
                "SELECT school_name_raw FROM education_entries "
                "WHERE user_id = :u AND is_most_recent"
            ),
            {"u": user_id},
        )
    assert [row[0] for row in flagged] == ["Second School"]


async def test_a_user_id_in_the_body_is_ignored(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Whose row this is comes from the URL the dependency resolved. A body field
    would be a second answer, and the caller would choose it."""
    auth_id = uuid4()
    mine = await make_user(db_engine, auth_id, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")

    await api_client.post(
        url(mine, "education"),
        json={"school_name_raw": "Mine", "user_id": str(theirs), "created_by": str(theirs)},
        headers=bearer(api_token(auth_id)),
    )

    async with db_engine.connect() as conn:
        owner = await conn.execute(
            text("SELECT user_id FROM education_entries WHERE school_name_raw = 'Mine'")
        )
    assert owner.scalar_one() == mine


async def test_dates_out_of_order_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")

    response = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "X", "date_start": "2020-01-01", "date_end": "2019-01-01"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_patch_changes_only_what_was_sent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent is not null. Without `exclude_unset` a one-field edit would write
    every other field as its default and blank the rest."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    created = await api_client.post(
        url(user_id, "education"),
        json={"school_name_raw": "Keep Me", "study_course": "Law", "degree_category": "BSc"},
        headers=headers,
    )
    entry_id = created.json()["id"]

    await api_client.patch(
        url(user_id, f"education/{entry_id}"), json={"study_course": "Medicine"}, headers=headers
    )

    entries = (await api_client.get(url(user_id, "education"), headers=headers)).json()["data"]
    assert entries[0]["study_course"] == "Medicine"
    assert entries[0]["degree_category"] == "BSc", "an unsent field was blanked"
    assert entries[0]["school_name_raw"] == "Keep Me"


async def test_education_delete_is_soft(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    entry_id = (
        await api_client.post(
            url(user_id, "education"), json={"school_name_raw": "Gone"}, headers=headers
        )
    ).json()["id"]

    response = await api_client.delete(url(user_id, f"education/{entry_id}"), headers=headers)

    assert response.status_code == 204
    async with db_engine.connect() as conn:
        rows = await conn.execute(text("SELECT deleted_at FROM education_entries"))
        stored = rows.all()
    assert len(stored) == 1, "the row was destroyed, not soft-deleted"
    assert stored[0][0] is not None
    assert (await api_client.get(url(user_id, "education"), headers=headers)).json()["data"] == []


# --------------------------------------------------------------------------
# Goals, awards, and the delete asymmetry
# --------------------------------------------------------------------------


async def test_goal_delete_is_real(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    """`mentee_goals` has no `deleted_at`, so this destroys the row. The same
    verb means something different one table over, which is why both route
    descriptions say which."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.put(url(user_id, "goal"), json={"notes": "Gone"}, headers=headers)

    response = await api_client.delete(url(user_id, "goal"), headers=headers)

    assert response.status_code == 204
    async with db_engine.connect() as conn:
        count = await conn.execute(text("SELECT count(*) FROM mentee_goals"))
    assert count.scalar_one() == 0, "a goal was soft-deleted in a table with no such column"


async def test_award_delete_is_soft(api_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    award_id = (
        await api_client.post(
            url(user_id, "awards"), json={"title": "Gone", "institution": "A Body"}, headers=headers
        )
    ).json()["id"]

    response = await api_client.delete(url(user_id, f"awards/{award_id}"), headers=headers)

    assert response.status_code == 204
    async with db_engine.connect() as conn:
        rows = await conn.execute(text("SELECT deleted_at FROM user_awards"))
        stored = rows.all()
    assert len(stored) == 1, "the award row was destroyed, not soft-deleted"
    assert stored[0][0] is not None


async def test_setting_the_goal_again_replaces_it(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A `PUT`, because there is only ever one goal.

    `mentee_goals.user_id` is unique and the model says "1:1 with the user". The
    first version of this endpoint was a `POST` that appended, and the second
    call violated the constraint — which is how the 1:1 was found.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    async with db_engine.connect() as conn:
        ids = (await conn.execute(text("SELECT id FROM countries ORDER BY code LIMIT 2"))).scalars()
        first, second = list(ids)

    await api_client.put(
        url(user_id, "goal"), json={"notes": "A", "country_ids": [str(first)]}, headers=headers
    )
    await api_client.put(
        url(user_id, "goal"), json={"notes": "B", "country_ids": [str(second)]}, headers=headers
    )

    goal = (await api_client.get(url(user_id, "goal"), headers=headers)).json()
    async with db_engine.connect() as conn:
        rows = await conn.execute(text("SELECT count(*) FROM mentee_goals"))

    assert rows.scalar_one() == 1, "a second PUT created a second goal"
    assert goal["notes"] == "B"
    assert len(goal["countries"]) == 1, "the country list was merged rather than replaced"


async def test_an_omitted_list_is_left_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent means "leave alone"; an empty list means "clear". Conflating them
    makes a `PATCH` that omits countries silently wipe them."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    async with db_engine.connect() as conn:
        country = (await conn.execute(text("SELECT id FROM countries LIMIT 1"))).scalar_one()

    await api_client.put(
        url(user_id, "goal"), json={"notes": "A", "country_ids": [str(country)]}, headers=headers
    )
    await api_client.put(url(user_id, "goal"), json={"notes": "B"}, headers=headers)

    goal = (await api_client.get(url(user_id, "goal"), headers=headers)).json()
    assert goal["notes"] == "B"
    assert len(goal["countries"]) == 1, "an omitted list was wiped"


# --------------------------------------------------------------------------
# Mentor profile and user profile
# --------------------------------------------------------------------------


async def test_applying_creates_a_pending_profile_and_flips_the_role(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")

    response = await api_client.post(
        url(user_id, "mentor-profile"),
        json={"headline": "I help"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 201
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT p.approval_status, u.primary_role FROM mentor_profiles p "
                "JOIN users u ON u.id = p.user_id WHERE p.user_id = :u"
            ),
            {"u": user_id},
        )
        approval, role = row.one()
    assert str(approval).endswith("pending") or str(approval) == "pending"
    assert str(role).endswith("mentor") or str(role) == "mentor"


async def test_a_second_application_is_a_conflict(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """409 rather than a duplicate row or a constraint violation surfacing as a
    500. `uq_mentor_profiles_user_id` is the real guard; this is the considered
    answer in front of it."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "mentor-profile"), json={"headline": "A"}, headers=headers)

    response = await api_client.post(
        url(user_id, "mentor-profile"), json={"headline": "B"}, headers=headers
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_mentor_cannot_approve_themselves(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`approval_status` is not in the writable set, so sending it is ignored
    rather than honoured. A mentor who could approve themselves is not being
    reviewed."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    await api_client.post(url(user_id, "mentor-profile"), json={"headline": "A"}, headers=headers)

    await api_client.patch(
        url(user_id, "mentor-profile"),
        json={"headline": "B", "approval_status": "approved", "listing_status": "listed"},
        headers=headers,
    )

    async with db_engine.connect() as conn:
        approval = await conn.execute(
            text("SELECT approval_status FROM mentor_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert not str(approval.scalar_one()).endswith("approved")


async def test_a_profile_patch_creates_the_row_when_there_is_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`/me` reports `has_profile: false` until a user first writes here, so a
    plain `UPDATE` would write nothing and answer 204."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))

    response = await api_client.patch(
        url(user_id, "profile"), json={"about_me": "  Hello  "}, headers=headers
    )

    assert response.status_code == 204
    me = (await api_client.get("/api/v1/me", headers=headers)).json()
    # Trimmed by the shared normaliser, not by this handler.
    assert me["profile"]["about_me"] == "Hello"


async def test_an_image_url_cannot_be_set_through_the_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Images are content-addressed in Supabase Storage (ADR 0019). Accepting a
    URL here would let a profile point at any host and bypass that entirely."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))

    await api_client.patch(
        url(user_id, "profile"),
        json={"about_me": "hi", "avatar_url": "https://evil.example/track.gif"},
        headers=headers,
    )

    async with db_engine.connect() as conn:
        avatar = await conn.execute(
            text("SELECT avatar_url FROM user_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert avatar.scalar_one() is None, "an arbitrary image URL was accepted"


async def test_an_emptied_string_becomes_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A form posts `""` for a field the user cleared. Storing that gives a
    column holding two spellings of "absent"."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))

    await api_client.patch(url(user_id, "profile"), json={"about_me": "   "}, headers=headers)

    async with db_engine.connect() as conn:
        about = await conn.execute(
            text("SELECT about_me FROM user_profiles WHERE user_id = :u"), {"u": user_id}
        )
    assert about.scalar_one() is None


# --------------------------------------------------------------------------
# Languages — the item carried over from the previous PR
# --------------------------------------------------------------------------


async def language_ids(engine: AsyncEngine, *names: str) -> list[UUID]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, display_name FROM languages WHERE display_name = ANY(:n)"),
            {"n": list(names)},
        )
        by_name = {row.display_name: row.id for row in rows}
    return [by_name[name] for name in names]


async def test_languages_replace_rather_than_merge(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The request carries the whole list, so a merge could not express removal
    — a user unticking their last language would have no way to say so."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    english, yoruba = await language_ids(db_engine, "English", "Yoruba")

    await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(english)}, {"language_id": str(yoruba)}]},
        headers=headers,
    )
    await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(yoruba)}]},
        headers=headers,
    )

    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT l.display_name FROM user_languages ul "
                "JOIN languages l ON l.id = ul.language_id WHERE ul.user_id = :u"
            ),
            {"u": user_id},
        )
    assert [row[0] for row in rows] == ["Yoruba"]


async def test_two_primary_languages_are_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`ix_user_languages_one_primary` is a unique partial index, so a second
    primary is an `IntegrityError`. Refusing it at the boundary gives a 422
    naming the problem instead of a 500 to decode."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    english, yoruba = await language_ids(db_engine, "English", "Yoruba")

    response = await api_client.put(
        url(user_id, "languages"),
        json={
            "languages": [
                {"language_id": str(english), "is_primary": True},
                {"language_id": str(yoruba), "is_primary": True},
            ]
        },
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_one_primary_is_accepted_and_replacing_it_works(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive case, and the clear-then-set ordering: writing a new primary
    before removing the old one violates the same index."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    headers = bearer(api_token(auth_id))
    english, yoruba = await language_ids(db_engine, "English", "Yoruba")

    first = await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(english), "is_primary": True}]},
        headers=headers,
    )
    second = await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(yoruba), "is_primary": True}]},
        headers=headers,
    )

    assert (first.status_code, second.status_code) == (204, 204)
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT l.display_name FROM user_languages ul "
                "JOIN languages l ON l.id = ul.language_id "
                "WHERE ul.user_id = :u AND ul.is_primary"
            ),
            {"u": user_id},
        )
    assert [row[0] for row in rows] == ["Yoruba"]


async def test_the_same_language_twice_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`ix_user_languages_user_language` is unique on the pair. A duplicate is a
    client bug worth naming rather than a constraint violation to decode."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    (english,) = await language_ids(db_engine, "English")

    response = await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(english)}, {"language_id": str(english)}]},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 422


async def test_proficiency_defaults_to_fluent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    (english,) = await language_ids(db_engine, "English")

    await api_client.put(
        url(user_id, "languages"),
        json={"languages": [{"language_id": str(english)}]},
        headers=bearer(api_token(auth_id)),
    )

    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT proficiency FROM user_languages WHERE user_id = :u"), {"u": user_id}
        )
    assert str(rows.scalar_one()).endswith("fluent")


async def test_setting_my_languages_leaves_anybody_elses_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The write deletes before it inserts, and the delete is scoped by
    `user_id` alone — so that clause is the only thing between one user and
    every row in the table."""
    auth_id = uuid4()
    mine = await make_user(db_engine, auth_id, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    english, yoruba = await language_ids(db_engine, "English", "Yoruba")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_languages (user_id, language_id) VALUES (:u, :l)"),
            {"u": theirs, "l": yoruba},
        )

    await api_client.put(
        url(mine, "languages"),
        json={"languages": [{"language_id": str(english)}]},
        headers=bearer(api_token(auth_id)),
    )

    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM user_languages WHERE user_id = :u"), {"u": theirs}
        )
    assert rows.scalar_one() == 1, "another user's languages were deleted"
