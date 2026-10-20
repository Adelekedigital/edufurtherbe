"""The purge job and admin restore for self-deleted accounts (Mentee Settings PRD).

30 days after `DELETE /me` the job hard-deletes the person's own data and
anonymises the account row; sessions and reviews stay, with nobody named. Until
then a super admin may restore the account.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import (
    add_availability,
    add_session_type,
    make_public_mentor,
    token_for,
)
from tests.integration.test_api_session_transitions import a_booking

from app.infra.auth.admin import AdminApiError
from app.infra.clients.loops_contacts import ContactsError
from app.infra.db import account_purge
from app.infra.db.account_purge import purge_due_accounts
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DAY = dt.timedelta(days=1)
AVATAR = "https://project.supabase.co/storage/v1/object/public/profile-images/users/x/avatar-1.png"


class FakeAdmin:
    def __init__(self, *, fail: bool = False) -> None:
        self.deleted: list[UUID] = []
        self.fail = fail

    def delete_user(self, auth_id: UUID) -> None:
        if self.fail:
            raise AdminApiError("delete user failed with 500")
        self.deleted.append(auth_id)


class FakeContacts:
    def __init__(self, *, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    def delete(self, email: str) -> None:
        if self.fail:
            raise ContactsError("loops contact delete failed with 500")
        self.deleted.append(email)


class FakeStorage:
    def __init__(self) -> None:
        self.dropped: list[str] = []

    def drop_url(self, url: str) -> bool:
        self.dropped.append(url)
        return True


async def one(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).mappings().one()


async def count(engine: AsyncEngine, table: str, column: str, user: UUID) -> int:
    row = await one(engine, f"SELECT count(*) AS n FROM {table} WHERE {column} = :u", u=user)  # noqa: S608
    return int(row["n"])


async def deleted(engine: AsyncEngine, user: UUID, *, purge_in: dt.timedelta) -> None:
    """Mark ``user`` deleted, with the purge date ``purge_in`` from now."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET deleted_at = now() - interval '31 days', "
                "deletion_purge_at = now() + :purge_in WHERE id = :u"
            ),
            {"u": user, "purge_in": purge_in},
        )


async def a_full_account(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID]:
    """A mentee with something in every table the purge clears."""
    auth = uuid4()
    async with engine.begin() as conn:
        user = await add_user(
            conn,
            f"purge-{tag}@example.test",
            auth_id=auth,
            slug=f"purge-{tag}",
            first_name="Ada",
            last_name="Obi",
            primary_role="mentee",
        )
        statements = [
            "INSERT INTO user_profiles (user_id, avatar_url, about_me) "
            "VALUES (:u, :a, 'About Ada')",
            "INSERT INTO user_languages (user_id, language_id) "
            "SELECT :u, id FROM languages ORDER BY id LIMIT 1",
            "INSERT INTO education_entries (user_id, school_name_raw) VALUES (:u, 'Unilag')",
            "INSERT INTO user_awards (user_id, institution, title) "
            "VALUES (:u, 'Chevening', 'Scholar')",
            "INSERT INTO mentee_goals (user_id, notes) VALUES (:u, 'Study in Canada')",
            "INSERT INTO mentee_goal_schools (goal_id, school_name_raw, position) "
            "SELECT id, 'UofT', 0 FROM mentee_goals WHERE user_id = :u",
            "INSERT INTO calendar_connections (user_id, provider, refresh_token_encrypted) "
            "VALUES (:u, 'google', 'secret')",
            "INSERT INTO auth_identities (user_id, provider, provider_user_id) "
            "VALUES (:u, 'google', :a)",
            "INSERT INTO feature_interest (user_id, feature) VALUES (:u, 'messaging')",
            "INSERT INTO user_onboarding (user_id) VALUES (:u)",
            "INSERT INTO legal_documents (type, version, content_url, effective_from) "
            "VALUES ('terms_of_service', :a, 'https://example.com/terms', now())",
            "INSERT INTO user_legal_consents (user_id, legal_document_id, ip_address) "
            "SELECT :u, id, '203.0.113.7' FROM legal_documents WHERE version = :a",
        ]
        for statement in statements:
            await conn.execute(
                text(statement),
                {"u": user, "a": AVATAR if "avatar_url" in statement else str(user)},
            )
    return user, auth


PURGED_TABLES = (
    ("user_profiles", "user_id"),
    ("user_languages", "user_id"),
    ("education_entries", "user_id"),
    ("user_awards", "user_id"),
    ("mentee_goals", "user_id"),
    ("calendar_connections", "user_id"),
    ("auth_identities", "user_id"),
    ("feature_interest", "user_id"),
    ("user_onboarding", "user_id"),
)


def factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def purge(
    engine: AsyncEngine,
    admin: FakeAdmin,
    storage: FakeStorage,
    contacts: FakeContacts | None = None,
    **kw: Any,
) -> Any:
    return await purge_due_accounts(
        factory(engine),
        admin=admin,
        contacts=contacts or FakeContacts(),
        storage=storage,
        now=dt.datetime.now(dt.UTC),
        **kw,
    )


# --------------------------------------------------------------------------
# The purge
# --------------------------------------------------------------------------


async def test_a_due_account_is_purged_and_its_history_kept_unnamed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    booked = await a_booking(db_engine, api_client, "purge-history", confirmed=True)
    user, auth = await a_full_account(db_engine, "due")
    async with db_engine.begin() as conn:
        # A session that ran: history the purge keeps.
        await conn.execute(
            text("UPDATE sessions SET status = 'completed' WHERE id = :s"), {"s": booked["id"]}
        )
    await deleted(db_engine, user, purge_in=-DAY)
    await deleted(db_engine, booked["mentee_id"], purge_in=-DAY)
    admin, storage, contacts = FakeAdmin(), FakeStorage(), FakeContacts()

    counts = await purge(db_engine, admin, storage, contacts)

    assert counts == {"due": 2, "purged": 2, "failed": 0}
    for table, column in PURGED_TABLES:
        assert await count(db_engine, table, column, user) == 0, table
    schools = await one(
        db_engine, "SELECT count(*) AS n FROM mentee_goal_schools WHERE school_name_raw = 'UofT'"
    )
    assert schools["n"] == 0
    row = await one(
        db_engine,
        "SELECT email, first_name, last_name, slug, auth_id, email_verified_at, "
        "deleted_at IS NOT NULL AS still_deleted, deletion_purge_at FROM users WHERE id = :u",
        u=user,
    )
    assert dict(row) == {
        "email": f"deleted-{user}@deleted.invalid",
        "first_name": None,
        "last_name": None,
        "slug": None,
        "auth_id": None,
        "email_verified_at": None,
        "still_deleted": True,
        "deletion_purge_at": None,
    }
    consent = await one(
        db_engine, "SELECT ip_address FROM user_legal_consents WHERE user_id = :u", u=user
    )
    assert consent["ip_address"] is None
    assert auth in admin.deleted
    # The email provider's copy goes too, found by the real address before it
    # was tombstoned.
    assert "purge-due@example.test" in contacts.deleted
    assert storage.dropped == [AVATAR]
    # The session stays, as history.
    kept = await one(db_engine, "SELECT status FROM sessions WHERE id = :s", s=booked["id"])
    assert kept["status"] == "completed"


async def test_only_accounts_past_their_purge_date_are_touched(
    db_engine: AsyncEngine,
) -> None:
    live, _ = await a_full_account(db_engine, "live")
    waiting, _ = await a_full_account(db_engine, "waiting")
    migrated, _ = await a_full_account(db_engine, "migrated")
    await deleted(db_engine, waiting, purge_in=DAY)
    async with db_engine.begin() as conn:
        # A migrated soft delete: deleted, never given a purge date.
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": migrated}
        )

    counts = await purge(db_engine, FakeAdmin(), FakeStorage())

    assert counts == {"due": 0, "purged": 0, "failed": 0}
    for user in (live, waiting, migrated):
        assert await count(db_engine, "user_profiles", "user_id", user) == 1


async def test_a_supabase_failure_leaves_the_account_for_the_next_run(
    db_engine: AsyncEngine,
) -> None:
    """Supabase first: if its account cannot be removed, ours is not touched,
    so the next run tries both again rather than orphaning a sign-in."""
    user, auth = await a_full_account(db_engine, "retry")
    await deleted(db_engine, user, purge_in=-DAY)

    counts = await purge(db_engine, FakeAdmin(fail=True), FakeStorage())

    assert counts == {"due": 1, "purged": 0, "failed": 1}
    assert await count(db_engine, "user_profiles", "user_id", user) == 1

    admin = FakeAdmin()
    assert (await purge(db_engine, admin, FakeStorage()))["purged"] == 1
    assert admin.deleted == [auth]
    assert (await purge(db_engine, FakeAdmin(), FakeStorage()))["due"] == 0


async def test_a_loops_failure_leaves_the_account_for_the_next_run(
    db_engine: AsyncEngine,
) -> None:
    """Like Supabase: the contact is found by the real address, which the purge
    tombstones, so a failed delete must leave the account for a retry."""
    user, _ = await a_full_account(db_engine, "loops")
    await deleted(db_engine, user, purge_in=-DAY)
    admin = FakeAdmin()

    counts = await purge(db_engine, admin, FakeStorage(), FakeContacts(fail=True))

    assert counts == {"due": 1, "purged": 0, "failed": 1}
    assert admin.deleted == []
    assert await count(db_engine, "user_profiles", "user_id", user) == 1


async def test_one_account_failing_in_the_database_does_not_stop_the_rest(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first due account is always taken first, so an error there that
    stopped the run would stop every run behind it too."""
    first, _ = await a_full_account(db_engine, "poison")
    second, _ = await a_full_account(db_engine, "healthy")
    await deleted(db_engine, first, purge_in=-2 * DAY)
    await deleted(db_engine, second, purge_in=-DAY)
    real = account_purge.purge_account

    async def failing(session: Any, user_id: UUID, *, now: dt.datetime) -> Any:
        if user_id == first:
            await session.execute(text("SELECT 1 / 0"))
        return await real(session, user_id, now=now)

    monkeypatch.setattr(account_purge, "purge_account", failing)

    counts = await purge(db_engine, FakeAdmin(), FakeStorage())

    assert counts == {"due": 2, "purged": 1, "failed": 1}
    assert await count(db_engine, "user_profiles", "user_id", first) == 1
    assert await count(db_engine, "user_profiles", "user_id", second) == 0


async def test_a_dry_run_counts_and_changes_nothing(db_engine: AsyncEngine) -> None:
    user, _ = await a_full_account(db_engine, "dry")
    await deleted(db_engine, user, purge_in=-DAY)
    admin = FakeAdmin()

    counts = await purge(db_engine, admin, FakeStorage(), dry_run=True)

    assert counts == {"due": 1, "purged": 0, "failed": 0}
    assert admin.deleted == []
    assert await count(db_engine, "user_profiles", "user_id", user) == 1


async def test_a_mentors_profile_is_scrubbed_and_their_availability_cleared(
    db_engine: AsyncEngine,
) -> None:
    """The profile row stays, because offerings and the status log point at it;
    what the mentor wrote about themselves goes."""
    mentor = await make_public_mentor(db_engine, "purge-mentor")
    await add_availability(db_engine, mentor, day_of_week=1, start="09:00", end="12:00")
    offering = await add_session_type(db_engine, mentor, duration=60, notice=0)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET headline = 'PhD at Oxford', "
                "primary_study_program = 'DPhil' WHERE user_id = :u"
            ),
            {"u": mentor},
        )
        option = (
            await conn.execute(
                text(
                    "INSERT INTO mentor_conferencing_options (user_id, provider, custom_url) "
                    "VALUES (:u, 'custom', 'https://zoom.us/j/my-room') RETURNING id"
                ),
                {"u": mentor},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE session_types SET conferencing_option_id = :o, "
                "description = 'I studied at Oxford' WHERE id = :t"
            ),
            {"o": option, "t": offering},
        )
    await deleted(db_engine, mentor, purge_in=-DAY)

    assert (await purge(db_engine, FakeAdmin(), FakeStorage()))["purged"] == 1

    profile = await one(
        db_engine,
        "SELECT headline, primary_study_program, deleted_at IS NOT NULL AS gone "
        "FROM mentor_profiles WHERE user_id = :u",
        u=mentor,
    )
    assert dict(profile) == {"headline": None, "primary_study_program": None, "gone": True}
    assert await count(db_engine, "availability_rules", "mentor_user_id", mentor) == 0
    # The personal room link goes; the offering stays for its sessions, hidden
    # and without what the mentor wrote.
    assert await count(db_engine, "mentor_conferencing_options", "user_id", mentor) == 0
    kept = await one(
        db_engine,
        "SELECT description, is_active, deleted_at IS NOT NULL AS gone, conferencing_option_id "
        "FROM session_types WHERE id = :t",
        t=offering,
    )
    assert dict(kept) == {
        "description": None,
        "is_active": False,
        "gone": True,
        "conferencing_option_id": None,
    }


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


async def an_admin(engine: AsyncEngine, tag: str, role: str) -> dict[str, str]:
    auth = uuid4()
    async with engine.begin() as conn:
        admin = await add_user(conn, f"admin-{tag}@example.test", auth_id=auth, first_name="Al")
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"),
            {"u": admin, "r": role},
        )
    return bearer(api_token(auth))


def restore(client: httpx.AsyncClient, user: UUID, headers: dict[str, str]) -> Any:
    return client.post(f"/api/v1/admin/users/{user}/restore", headers=headers)


async def test_a_super_admin_restores_an_account_inside_its_window(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    user, auth = await a_full_account(db_engine, "restore")
    await deleted(db_engine, user, purge_in=DAY)
    own = bearer(api_token(auth))
    assert (await api_client.get("/api/v1/me", headers=own)).status_code == 404

    response = await restore(api_client, user, await an_admin(db_engine, "restore", "super_admin"))

    assert response.status_code == 204, response.text
    assert (await api_client.get("/api/v1/me", headers=own)).status_code == 200
    row = await one(
        db_engine, "SELECT deleted_at, deletion_purge_at FROM users WHERE id = :u", u=user
    )
    assert row["deleted_at"] is None and row["deletion_purge_at"] is None


async def test_restore_answers_404_once_the_window_has_closed(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    super_admin = await an_admin(db_engine, "closed", "super_admin")
    purged, _ = await a_full_account(db_engine, "closed-purged")
    await deleted(db_engine, purged, purge_in=-DAY)
    await purge(db_engine, FakeAdmin(), FakeStorage())
    # Past its date, and the job has not run since: still not restorable.
    due, _ = await a_full_account(db_engine, "closed-due")
    await deleted(db_engine, due, purge_in=-DAY)
    live, _ = await a_full_account(db_engine, "closed-live")

    for user in (due, purged, live, uuid4()):
        assert (await restore(api_client, user, super_admin)).status_code == 404


async def test_only_a_super_admin_may_restore(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    user, auth = await a_full_account(db_engine, "who")
    await deleted(db_engine, user, purge_in=DAY)
    own = bearer(api_token(auth))

    for headers in (
        await an_admin(db_engine, "who-approver", "mentor_approval"),
        await an_admin(db_engine, "who-limited", "limited_access"),
        bearer(await token_for(db_engine, await make_public_mentor(db_engine, "who-mentor"))),
    ):
        assert (await restore(api_client, user, headers)).status_code == 404
    assert (await restore(api_client, user, own)).status_code == 404
    row = await one(db_engine, "SELECT deleted_at FROM users WHERE id = :u", u=user)
    assert row["deleted_at"] is not None


async def test_a_loops_contact_a_live_account_shares_is_kept(db_engine: AsyncEngine) -> None:
    """The address came back as somebody's live account: the contact is theirs."""
    user, _ = await a_full_account(db_engine, "shared")
    await deleted(db_engine, user, purge_in=-DAY)
    async with db_engine.begin() as conn:
        await add_user(conn, "purge-shared@example.test", auth_id=uuid4(), first_name="Bo")
    contacts = FakeContacts()

    assert (await purge(db_engine, FakeAdmin(), FakeStorage(), contacts))["purged"] == 1
    assert contacts.deleted == []


async def test_an_account_restored_after_being_listed_keeps_its_sign_in(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run lists every due account first; one restored before its turn (a
    clock between two servers is enough) must keep its sign-in and contact,
    not only its rows. The account row is locked before either provider call."""
    first, _ = await a_full_account(db_engine, "listed-first")
    second, second_auth = await a_full_account(db_engine, "listed-restored")
    await deleted(db_engine, first, purge_in=-2 * DAY)
    await deleted(db_engine, second, purge_in=-DAY)
    real = account_purge._lock_due

    async def restored_first(session: Any, user_id: UUID, *, now: dt.datetime) -> Any:
        if user_id == second:
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE users SET deleted_at = NULL, deletion_purge_at = NULL WHERE id = :u"
                    ),
                    {"u": second},
                )
        return await real(session, user_id, now=now)

    monkeypatch.setattr(account_purge, "_lock_due", restored_first)
    admin, contacts = FakeAdmin(), FakeContacts()

    counts = await purge(db_engine, admin, FakeStorage(), contacts)

    assert counts == {"due": 2, "purged": 1, "failed": 0}
    assert second_auth not in admin.deleted
    assert "purge-listed-restored@example.test" not in contacts.deleted
    assert await count(db_engine, "user_profiles", "user_id", second) == 1
