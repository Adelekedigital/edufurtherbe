"""Constraints on the M1 identity tables that no model can express.

``alembic check`` sees tables, columns, types and regular indexes. It is blind to
partial indexes, ``CHECK`` constraints and ``ON DELETE`` behaviour — which is
most of what makes this schema correct. A green migration check means the chain
applies, not that it enforces anything.

Every test here asserts a rule that would otherwise be enforced by nothing but
the reviewer who read the migration.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

INSERT_USER = """
INSERT INTO users (id, email, slug, deleted_at)
VALUES (:id, :email, :slug, :deleted_at)
"""


async def add_user(
    conn: AsyncConnection,
    email: str,
    *,
    slug: str | None = None,
    deleted_at: datetime | None = None,
) -> uuid.UUID:
    """Insert a user and return its id.

    The id is supplied rather than defaulted, because ``users`` is the one table
    with no ``DEFAULT`` on its primary key — ADR 0009 §9. In production this
    value comes from Supabase Auth; here any uuid4 will do, and using uuid4
    rather than uuid7 mirrors what Supabase actually issues.
    """
    user_id = uuid.uuid4()
    await conn.execute(
        text(INSERT_USER),
        {"id": user_id, "email": email, "slug": slug, "deleted_at": deleted_at},
    )
    return user_id


LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# users.email — the partial unique index
# --------------------------------------------------------------------------


async def test_two_live_users_cannot_share_an_email(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await add_user(conn, "taken@example.com")

        with pytest.raises(IntegrityError):
            await add_user(conn, "taken@example.com")


async def test_a_soft_deleted_user_frees_their_email(db_engine: AsyncEngine) -> None:
    """The whole reason the index is partial.

    Without ``WHERE deleted_at IS NULL`` a soft-deleted user permanently blocks
    their own address from re-registering, and the rejection is indistinguishable
    from "that email is already taken" — so the bug presents as a support ticket
    nobody can reproduce.
    """
    async with db_engine.begin() as conn:
        await add_user(conn, "returning@example.com", deleted_at=LONG_AGO)

        # The positive case. A test that only proved duplicates are rejected
        # would pass just as happily against a non-partial index, which is the
        # defect being guarded against.
        user_id = await add_user(conn, "returning@example.com")

    assert user_id is not None


async def test_email_comparison_ignores_case(db_engine: AsyncEngine) -> None:
    """``citext``, so every call site is case-insensitive rather than the ones
    somebody remembered to ``lower()``."""
    async with db_engine.begin() as conn:
        await add_user(conn, "Mixed.Case@Example.COM")

        found = await conn.execute(
            text("SELECT count(*) FROM users WHERE email = :email"),
            {"email": "mixed.case@example.com"},
        )
        assert found.scalar_one() == 1

        with pytest.raises(IntegrityError):
            await add_user(conn, "MIXED.CASE@EXAMPLE.COM")


# --------------------------------------------------------------------------
# users.slug — partial unique index plus the format CHECK
# --------------------------------------------------------------------------


async def test_two_live_users_cannot_share_a_slug(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await add_user(conn, "a@example.com", slug="ada-lovelace")

        with pytest.raises(IntegrityError):
            await add_user(conn, "b@example.com", slug="ada-lovelace")


async def test_a_soft_deleted_user_frees_their_slug(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await add_user(conn, "a@example.com", slug="ada-lovelace", deleted_at=LONG_AGO)

        user_id = await add_user(conn, "b@example.com", slug="ada-lovelace")

    assert user_id is not None


async def test_many_users_may_have_no_slug(db_engine: AsyncEngine) -> None:
    """Null slugs must not collide with each other.

    PostgreSQL treats nulls as distinct in a unique index, so this holds without
    the ``slug IS NOT NULL`` clause too — but four of the 43 dev users have no
    slug, and a regression here would block the load rather than fail a query.
    """
    async with db_engine.begin() as conn:
        for i in range(3):
            await add_user(conn, f"nobody{i}@example.com")

        count = await conn.execute(text("SELECT count(*) FROM users WHERE slug IS NULL"))
        assert count.scalar_one() == 3


@pytest.mark.parametrize(
    "slug",
    ["Ada-Lovelace", "ada lovelace", "ada/lovelace", "ada_lovelace", "adá-lovelace"],
)
async def test_a_slug_that_is_not_url_safe_is_rejected(db_engine: AsyncEngine, slug: str) -> None:
    """Uppercase is in this list deliberately.

    ``slug`` is ``text``, so its unique index is case-sensitive; without the
    CHECK forcing lowercase, ``Ada-Lovelace`` and ``ada-lovelace`` would be two
    distinct slugs resolving to what every reader would call one profile.
    """
    async with db_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await add_user(conn, "a@example.com", slug=slug)


async def test_a_url_safe_slug_is_accepted(db_engine: AsyncEngine) -> None:
    """The positive half. All 39 slugs in the dev extract take this shape."""
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "a@example.com", slug="sakiratu-adeleke")

    assert user_id is not None


# --------------------------------------------------------------------------
# admin_users — one live grant per (user, role)
# --------------------------------------------------------------------------


async def test_two_live_grants_of_the_same_role_are_rejected(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "admin@example.com")
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": user_id},
        )

        with pytest.raises(IntegrityError):
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user_id},
            )


async def test_a_revoked_role_can_be_granted_again(db_engine: AsyncEngine) -> None:
    """The reason that index is partial.

    Without ``WHERE revoked_at IS NULL`` the historical row blocks the re-grant,
    and the way people get work done is by deleting from the audit trail.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "admin@example.com")
        await conn.execute(
            text(
                "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
                "VALUES (:u, 'super_admin', now())"
            ),
            {"u": user_id},
        )

        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": user_id},
        )

        rows = await conn.execute(
            text("SELECT count(*) FROM admin_users WHERE user_id = :u"), {"u": user_id}
        )
        assert rows.scalar_one() == 2, "the revoked grant must survive the re-grant"


async def test_one_user_may_hold_two_different_roles(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "admin@example.com")
        for role in ("super_admin", "mentor_approval"):
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :role)"),
                {"u": user_id, "role": role},
            )

        rows = await conn.execute(
            text("SELECT count(*) FROM admin_users WHERE user_id = :u"), {"u": user_id}
        )
        assert rows.scalar_one() == 2


# --------------------------------------------------------------------------
# user_languages
# --------------------------------------------------------------------------


async def test_a_user_has_at_most_one_primary_language(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "polyglot@example.com")
        await conn.execute(
            text(
                "INSERT INTO user_languages (user_id, language_code, is_primary) "
                "VALUES (:u, 'eng', true)"
            ),
            {"u": user_id},
        )

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO user_languages (user_id, language_code, is_primary) "
                    "VALUES (:u, 'yor', true)"
                ),
                {"u": user_id},
            )


async def test_a_user_may_hold_several_non_primary_languages(db_engine: AsyncEngine) -> None:
    """The positive case, and it is not redundant.

    A unique index on ``(user_id, is_primary)`` would satisfy the test above
    while permitting exactly one *non*-primary language — the opposite of the
    rule. Only this assertion distinguishes the two.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "polyglot@example.com")
        for code in ("eng", "yor", "pcm"):
            await conn.execute(
                text(
                    "INSERT INTO user_languages (user_id, language_code, is_primary) "
                    "VALUES (:u, :code, false)"
                ),
                {"u": user_id, "code": code},
            )

        rows = await conn.execute(
            text("SELECT count(*) FROM user_languages WHERE user_id = :u"), {"u": user_id}
        )
        assert rows.scalar_one() == 3


async def test_an_unknown_language_code_is_rejected(db_engine: AsyncEngine) -> None:
    """This is the assertion that will fire during M1c.

    Legacy stores comma-separated display names. Against the M0 seed ``Breton``
    and ``Aymara`` resolve, ``Abkhaz`` does not — ISO 639-3 calls it
    ``Abkhazian`` — and ``Avestan`` is absent because M0 filtered to living
    languages. ``ave`` stands in for that second class here.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "polyglot@example.com")

        with pytest.raises(IntegrityError):
            await conn.execute(
                text("INSERT INTO user_languages (user_id, language_code) VALUES (:u, 'ave')"),
                {"u": user_id},
            )


# --------------------------------------------------------------------------
# ON DELETE — ADR 0012
# --------------------------------------------------------------------------

# (table, insert, count) as literal SQL rather than a table name interpolated
# into a f-string at the call site. A table name cannot be a bind parameter, so
# building the SELECT from a loop variable is the one thing here that would need
# an S608 suppression — and a suppressed rule in a test file is how the rule
# stops being read in the source files that matter.
OWNED_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "user_profiles",
        "INSERT INTO user_profiles (user_id) VALUES (:u)",
        "SELECT count(*) FROM user_profiles WHERE user_id = :u",
    ),
    (
        "user_onboarding",
        "INSERT INTO user_onboarding (user_id) VALUES (:u)",
        "SELECT count(*) FROM user_onboarding WHERE user_id = :u",
    ),
    (
        "user_languages",
        "INSERT INTO user_languages (user_id, language_code) VALUES (:u, 'eng')",
        "SELECT count(*) FROM user_languages WHERE user_id = :u",
    ),
    (
        "auth_identities",
        "INSERT INTO auth_identities (user_id, provider, provider_user_id) "
        "VALUES (:u, 'google', 'g-1')",
        "SELECT count(*) FROM auth_identities WHERE user_id = :u",
    ),
)


async def test_deleting_a_user_cascades_to_rows_that_only_describe_them(
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "going@example.com")
        for _, insert, _count in OWNED_ROWS:
            await conn.execute(text(insert), {"u": user_id})

        # Every row must exist first. Without this the loop below would pass
        # against a schema where the inserts silently did nothing — counting zero
        # rows that were never there is not evidence of a cascade.
        for table, _insert, count_sql in OWNED_ROWS:
            before = await conn.execute(text(count_sql), {"u": user_id})
            assert before.scalar_one() == 1, f"{table} row was never created"

        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})

        for table, _insert, count_sql in OWNED_ROWS:
            left = await conn.execute(text(count_sql), {"u": user_id})
            assert left.scalar_one() == 0, f"{table} should have cascaded"


async def add_consent(conn: AsyncConnection, user_id: uuid.UUID) -> None:
    document_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO legal_documents (id, type, version, content_url, effective_from) "
            "VALUES (:d, 'terms_of_service', 'v1', 'https://example.com/terms', now())"
        ),
        {"d": document_id},
    )
    await conn.execute(
        text("INSERT INTO user_legal_consents (user_id, legal_document_id) VALUES (:u, :d)"),
        {"u": user_id, "d": document_id},
    )


async def add_admin_grant(conn: AsyncConnection, user_id: uuid.UUID) -> None:
    await conn.execute(
        text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
        {"u": user_id},
    )


@pytest.mark.parametrize(
    ("blocking_table", "add_blocker"),
    [("user_legal_consents", add_consent), ("admin_users", add_admin_grant)],
)
async def test_an_audit_row_blocks_deleting_the_user(
    db_engine: AsyncEngine,
    blocking_table: str,
    add_blocker: Callable[[AsyncConnection, uuid.UUID], object],
) -> None:
    """ADR 0012, and the half that matters.

    A test asserting only the cascades above would pass unchanged against blanket
    ``ON DELETE CASCADE`` — which is what ``01_identity.sql`` writes and what
    0012 rejects. This is the assertion that tells the two apart: the delete must
    *fail*, so consent evidence and the elevated-access audit trail cannot be
    destroyed by a statement nobody thought about.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "protected@example.com")
        await add_blocker(conn, user_id)  # type: ignore[misc]

        with pytest.raises(IntegrityError) as caught:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})

    # Which constraint refused matters. "Some IntegrityError was raised" would
    # also be satisfied by an unrelated failure — a not-null violation in the
    # fixture, say — and would then report a passing test for a schema that
    # cascades exactly as ADR 0012 forbids.
    assert blocking_table in str(caught.value), (
        f"the delete was blocked, but not by {blocking_table}: {caught.value}"
    )
