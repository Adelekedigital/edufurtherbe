"""Constraints on the M1 identity tables that no model can express.

``alembic check`` sees tables, columns, types and regular indexes. It is blind to
partial indexes, ``CHECK`` constraints and ``ON DELETE`` behaviour — which is
most of what makes this schema correct. A green migration check means the chain
applies, not that it enforces anything.

Every test here asserts a rule that would otherwise be enforced by nothing but
the reviewer who read the migration.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

INSERT_USER = """
INSERT INTO users (email, slug, deleted_at)
VALUES (:email, :slug, :deleted_at)
RETURNING id
"""


async def add_user(
    conn: AsyncConnection,
    email: str,
    *,
    slug: str | None = None,
    deleted_at: datetime | None = None,
) -> uuid.UUID:
    """Insert a user and return the id the database generated.

    The id is **not** supplied. ADR 0013 made it ours and gave it back its
    ``uuid_generate_v7()`` default, so letting the column fill itself is both
    what production does and a standing check that the default is still there —
    a test that passed its own id would keep passing after somebody removed it.

    ``auth_id`` is left null, which is the state every migrated user starts in.
    """
    created = await conn.execute(
        text(INSERT_USER),
        {"email": email, "slug": slug, "deleted_at": deleted_at},
    )
    return uuid.UUID(str(created.scalar_one()))


LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


async def language_id(conn: AsyncConnection, code: str) -> uuid.UUID:
    """Resolve an ISO 639-3 code to its surrogate id.

    Reference ids are generated per environment (ADR 0014), so no test may name
    one literally — the code is still the human-facing key and every lookup goes
    through it.
    """
    found = await conn.execute(text("SELECT id FROM languages WHERE code_639_3 = :c"), {"c": code})
    return uuid.UUID(str(found.scalar_one()))


async def country_id(conn: AsyncConnection, code: str) -> uuid.UUID:
    """Resolve an ISO 3166-1 alpha-2 code to its surrogate id."""
    found = await conn.execute(text("SELECT id FROM countries WHERE code = :c"), {"c": code})
    return uuid.UUID(str(found.scalar_one()))


async def user_exists(conn: AsyncConnection, user_id: uuid.UUID) -> bool:
    """Read the row back.

    Every "the insert succeeded" assertion goes through this rather than through
    ``assert user_id is not None`` — ``add_user`` returns a uuid4 on every path,
    so that comparison can never fail and describes nothing about the schema.
    """
    found = await conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": user_id})
    return bool(found.scalar_one() == 1)


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

        # Read the row back. `assert user_id is not None` would be vacuous —
        # `add_user` returns a uuid4 on every path, so it can never be None and
        # the line describes nothing about the schema.
        live = await conn.execute(
            text("SELECT count(*) FROM users WHERE email = :e AND deleted_at IS NULL"),
            {"e": "returning@example.com"},
        )
        assert live.scalar_one() == 1
        assert await user_exists(conn, user_id)


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

        assert await user_exists(conn, user_id)
        live = await conn.execute(
            text("SELECT count(*) FROM users WHERE slug = 'ada-lovelace' AND deleted_at IS NULL")
        )
        assert live.scalar_one() == 1


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

        assert await user_exists(conn, user_id)


async def test_an_absurdly_long_slug_is_rejected_as_validation(db_engine: AsyncEngine) -> None:
    """Without the length bound this fails as a storage error, not a validation one.

    ``slug`` is unbounded ``text`` under a unique btree, so a value past roughly
    2,700 bytes raises ``index row size exceeds btree version 4 maximum`` — and
    anything under that but over a few hundred characters is accepted while being
    useless in a URL. The CHECK turns both into one predictable rejection.
    """
    async with db_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await add_user(conn, "a@example.com", slug="a" * 61)


# --------------------------------------------------------------------------
# auth_identities — the provider identity is the account-takeover surface
# --------------------------------------------------------------------------


async def test_one_provider_identity_cannot_attach_to_two_users(db_engine: AsyncEngine) -> None:
    """The unique index with the most serious consequence if it were missing.

    ``(provider, provider_user_id)`` is what "this Google account is this user"
    means. Without uniqueness, a second row linking the same Google subject to a
    different local user makes first login ambiguous — and ADR 0009 §6 auto-links
    a migrated user on a provider-verified email, so the accounts most worth
    claiming are exactly the ones this protects.
    """
    async with db_engine.begin() as conn:
        first = await add_user(conn, "one@example.com")
        second = await add_user(conn, "two@example.com")

        await conn.execute(
            text(
                "INSERT INTO auth_identities (user_id, provider, provider_user_id) "
                "VALUES (:u, 'google', 'google-subject-1')"
            ),
            {"u": first},
        )

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO auth_identities (user_id, provider, provider_user_id) "
                    "VALUES (:u, 'google', 'google-subject-1')"
                ),
                {"u": second},
            )


async def test_one_user_may_link_two_different_providers(db_engine: AsyncEngine) -> None:
    """The positive case, and the whole reason this is its own table.

    Legacy ``Registration format`` was a single option set, so a user who signed
    up with Google could never also link LinkedIn.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "both@example.com")
        for provider, subject in (("google", "g-1"), ("linkedin", "l-1")):
            await conn.execute(
                text(
                    "INSERT INTO auth_identities (user_id, provider, provider_user_id) "
                    "VALUES (:u, :p, :s)"
                ),
                {"u": user_id, "p": provider, "s": subject},
            )

        linked = await conn.execute(
            text("SELECT count(*) FROM auth_identities WHERE user_id = :u"), {"u": user_id}
        )
        assert linked.scalar_one() == 2


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
        insert = text(
            "INSERT INTO user_languages (user_id, language_id, is_primary) VALUES (:u, :lang, true)"
        )
        await conn.execute(insert, {"u": user_id, "lang": await language_id(conn, "eng")})

        with pytest.raises(IntegrityError):
            await conn.execute(insert, {"u": user_id, "lang": await language_id(conn, "yor")})


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
                    "INSERT INTO user_languages (user_id, language_id, is_primary) "
                    "VALUES (:u, :lang, false)"
                ),
                {"u": user_id, "lang": await language_id(conn, code)},
            )

        rows = await conn.execute(
            text("SELECT count(*) FROM user_languages WHERE user_id = :u"), {"u": user_id}
        )
        assert rows.scalar_one() == 3


async def test_a_revoker_without_a_revocation_time_is_rejected(db_engine: AsyncEngine) -> None:
    """``revoked_by`` set while ``revoked_at`` is null is an incoherent audit row.

    It still matches ``WHERE revoked_at IS NULL``, so the active-grant index
    counts it as live while the trail reads "revoked by X". On a table whose
    entire purpose is being auditable, the two have to move together.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "admin@example.com")

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO admin_users (user_id, admin_role, revoked_by) "
                    "VALUES (:u, 'super_admin', :u)"
                ),
                {"u": user_id},
            )


async def test_a_revocation_records_both_time_and_actor(db_engine: AsyncEngine) -> None:
    """The positive half: a complete revocation is accepted."""
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "admin@example.com")
        await conn.execute(
            text(
                "INSERT INTO admin_users (user_id, admin_role, revoked_at, revoked_by) "
                "VALUES (:u, 'super_admin', now(), :u)"
            ),
            {"u": user_id},
        )

        revoked = await conn.execute(
            text(
                "SELECT count(*) FROM admin_users "
                "WHERE user_id = :u AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL"
            ),
            {"u": user_id},
        )
        assert revoked.scalar_one() == 1


# --------------------------------------------------------------------------
# legal documents and consents
# --------------------------------------------------------------------------


async def test_a_document_version_is_unique_per_type(db_engine: AsyncEngine) -> None:
    """Two rows claiming to be terms 'v1' make a consent ambiguous about what
    was accepted, which is the one thing this table exists to answer."""
    async with db_engine.begin() as conn:
        insert = text(
            "INSERT INTO legal_documents (type, version, content_url, effective_from) "
            "VALUES ('terms_of_service', 'v1', 'https://example.com/terms', now())"
        )
        await conn.execute(insert)

        with pytest.raises(IntegrityError):
            await conn.execute(insert)


async def test_the_same_version_may_exist_for_two_document_types(db_engine: AsyncEngine) -> None:
    """Uniqueness is per (type, version), not per version. Terms v1 and the
    privacy policy v1 are different documents that happen to share a label."""
    async with db_engine.begin() as conn:
        for doc_type in ("terms_of_service", "privacy_policy"):
            await conn.execute(
                text(
                    "INSERT INTO legal_documents (type, version, content_url, effective_from) "
                    "VALUES (:t, 'v1', 'https://example.com/doc', now())"
                ),
                {"t": doc_type},
            )

        count = await conn.execute(text("SELECT count(*) FROM legal_documents WHERE version='v1'"))
        assert count.scalar_one() == 2


async def test_a_user_cannot_consent_to_one_document_twice(db_engine: AsyncEngine) -> None:
    """A second acceptance of the same version is not a new fact."""
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "signer@example.com")
        document_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO legal_documents (id, type, version, content_url, effective_from) "
                "VALUES (:d, 'terms_of_service', 'v1', 'https://example.com/terms', now())"
            ),
            {"d": document_id},
        )
        insert = text(
            "INSERT INTO user_legal_consents (user_id, legal_document_id) VALUES (:u, :d)"
        )
        await conn.execute(insert, {"u": user_id, "d": document_id})

        with pytest.raises(IntegrityError):
            await conn.execute(insert, {"u": user_id, "d": document_id})


# --------------------------------------------------------------------------
# reference-table foreign keys — RESTRICT, per ADR 0012
# --------------------------------------------------------------------------


async def test_an_unknown_country_code_is_rejected(db_engine: AsyncEngine) -> None:
    """``origin_country_code`` is a real foreign key, not a two-letter string.

    The M1c transform maps country *names* from the legacy text columns, so a
    name that fails to resolve must fail here rather than land as a plausible
    two-letter value nobody can join on.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "traveller@example.com")

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO user_profiles (user_id, origin_country_id) VALUES (:u, :country)"
                ),
                {"u": user_id, "country": uuid.uuid4()},
            )


async def test_a_known_country_code_is_accepted(db_engine: AsyncEngine) -> None:
    """The positive half, against a code the M0 seed really contains."""
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "traveller@example.com")
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, origin_country_id, current_country_id) "
                "VALUES (:u, :origin, :current)"
            ),
            {
                "u": user_id,
                "origin": await country_id(conn, "NG"),
                "current": await country_id(conn, "GB"),
            },
        )

        # Read back through the join, because that is what every caller now does.
        stored = await conn.execute(
            text(
                "SELECT c.code FROM user_profiles p JOIN countries c ON c.id = p.origin_country_id "
                "WHERE p.user_id = :u"
            ),
            {"u": user_id},
        )
        assert stored.scalar_one() == "NG"


async def test_a_referenced_country_cannot_be_deleted(db_engine: AsyncEngine) -> None:
    """RESTRICT on a reference table, asserted rather than assumed.

    ADR 0012 lists all twelve foreign keys with their rule; three of them point
    at ``countries`` and ``languages`` and would otherwise be the only ones in
    that table with no test behind them.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "traveller@example.com")
        await conn.execute(
            text("INSERT INTO user_profiles (user_id, origin_country_id) VALUES (:u, :country)"),
            {"u": user_id, "country": await country_id(conn, "NG")},
        )

        with pytest.raises(IntegrityError):
            await conn.execute(text("DELETE FROM countries WHERE code = 'NG'"))


async def test_an_unknown_language_code_is_rejected(db_engine: AsyncEngine) -> None:
    """This is the assertion that will fire during M1c.

    Legacy stores comma-separated display names. Against the M0 seed ``Breton``
    and ``Aymara`` resolve, ``Abkhaz`` does not — ISO 639-3 calls it
    ``Abkhazian`` — and ``Avestan`` is absent because M0 filtered to living
    languages. ``ave`` stands in for that second class here.
    """
    async with db_engine.begin() as conn:
        user_id = await add_user(conn, "polyglot@example.com")

        # `ave` (Avestan) is absent from the M0 seed because it filtered to
        # living languages, so there is no id to resolve — which is exactly the
        # shape the M1c transform will hit. A random uuid stands in for "a
        # language this database does not have".
        with pytest.raises(IntegrityError):
            await conn.execute(
                text("INSERT INTO user_languages (user_id, language_id) VALUES (:u, :lang)"),
                {"u": user_id, "lang": uuid.uuid4()},
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
        "INSERT INTO user_languages (user_id, language_id) "
        "VALUES (:u, (SELECT id FROM languages WHERE code_639_3 = 'eng'))",
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
    add_blocker: Callable[[AsyncConnection, uuid.UUID], Awaitable[None]],
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
        await add_blocker(conn, user_id)

        with pytest.raises(IntegrityError) as caught:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})

    # Which constraint refused matters. "Some IntegrityError was raised" would
    # also be satisfied by an unrelated failure — a not-null violation in the
    # fixture, say — and would then report a passing test for a schema that
    # cascades exactly as ADR 0012 forbids.
    assert blocking_table in str(caught.value), (
        f"the delete was blocked, but not by {blocking_table}: {caught.value}"
    )
