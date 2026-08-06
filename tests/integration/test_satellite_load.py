"""The five tables that hang off ``users``, loaded from a legacy snapshot.

Two things here would otherwise be silent. A name that fails to resolve could be
dropped without comment, leaving a profile with no country and nobody aware. And
``auth_identities`` loading zero rows from an export looks identical to nobody
having linked a provider — one is the wrong source, the other is a fact.
"""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.resolve import COUNTRY_ALIASES, LANGUAGE_ALIASES, resolve_names
from app.domain.transform import TransformError, plan_identity, to_admin_grant, to_identities
from app.infra.db.engine import to_async_dsn
from app.infra.etl.loader import UserLoader
from app.infra.etl.satellites import SatelliteLoader, reference_maps, user_id_map

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

USER_ID = "1701974206179x877854702892984200"
PROFILE_ID = "1761272910139x746213933324959700"


def user(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bubble_id": USER_ID,
        "email": "sakiratu@example.com",
        "👥Role": "Mentor",
        "UserTimezonID": "Africa/Lagos",
        "created_at": "2023-12-07T18:36:46.179Z",
        "modified_at": "2025-11-06T11:52:41.383Z",
        "User-last-onboarding-step": "5",
        "registration completed ": "2023-12-07T18:38:28.221Z",
        "👤Personal Info": PROFILE_ID,
        "Registration format": "Email",
        "provider_identities": {},
    }
    return base | overrides


def profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bubble_id": PROFILE_ID,
        "created_at": "2025-09-03T05:31:00.000Z",
        "modified_at": "2025-09-03T05:44:00.000Z",
        "About me": "Hello",
        "Gender": "Female",
        "OriginCountry(text)": "Nigeria",
        "list-Language": "Yoruba , English",
    }
    return base | overrides


async def load_everything(
    conn: AsyncConnection, users: list[dict[str, Any]], profiles: list[dict[str, Any]]
) -> Any:
    plan = plan_identity(users, profiles)
    assert plan.ok, plan.errors

    await UserLoader(conn).load(plan.users)
    ids = await user_id_map(conn)
    country_ref, language_ref = await reference_maps(conn)

    return await SatelliteLoader(conn).load(
        users=ids,
        profiles=plan.profiles,
        onboarding=plan.onboarding,
        identities=plan.identities,
        admin_grants=plan.admin_grants,
        countries=resolve_names(plan.country_names(), country_ref, COUNTRY_ALIASES),
        languages=resolve_names(plan.language_names(), language_ref, LANGUAGE_ALIASES),
    )


# --------------------------------------------------------------------------
# the happy path, end to end
# --------------------------------------------------------------------------


async def test_all_five_tables_load_from_one_snapshot(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        counts = await load_everything(conn, [user(**{"Admin 🎩": "Super Admin"})], [profile()])

        stored = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM user_profiles), "
                "(SELECT count(*) FROM user_languages), "
                "(SELECT count(*) FROM user_onboarding), "
                "(SELECT count(*) FROM admin_users)"
            )
        )
        profiles, languages, onboarding, admins = stored.one()

    assert (profiles, languages, onboarding, admins) == (1, 2, 1, 1)
    assert counts.profiles == 1
    assert counts.languages == 2


async def test_the_country_comes_from_the_text_field(db_engine: AsyncEngine) -> None:
    """The canonical field mapping says the opposite, and is wrong.

    It sends ``Country of Origin`` to ``origin_country_id`` and drops
    ``OriginCountry(text)`` as a duplicate. The coded field is empty on every dev
    row. Following the document migrates zero countries and reports success —
    which is why this asserts the resolved country rather than merely a non-null.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn,
            [user()],
            [profile(**{"Country of Origin": "", "OriginCountry(text)": "Ghana"})],
        )

        resolved = await conn.execute(
            text(
                "SELECT c.code FROM user_profiles p JOIN countries c ON c.id = p.origin_country_id"
            )
        )

    assert resolved.scalar_one() == "GH"


async def test_bubble_timestamps_survive_on_profiles_too(db_engine: AsyncEngine) -> None:
    """The satellites load inside the same trigger-disabled window as ``users``."""
    async with db_engine.begin() as conn:
        await load_everything(conn, [user()], [profile()])
        stored = await conn.execute(text("SELECT updated_at FROM user_profiles"))

    assert stored.scalar_one().year == 2025


async def test_a_second_load_changes_nothing(db_engine: AsyncEngine) -> None:
    """Idempotent across all six tables, not only ``users``."""
    async with db_engine.begin() as conn:
        await load_everything(conn, [user(**{"Admin 🎩": "Super Admin"})], [profile()])
        first = (await conn.execute(text("SELECT count(*) FROM user_languages"))).scalar_one()

    async with db_engine.begin() as conn:
        await load_everything(conn, [user(**{"Admin 🎩": "Super Admin"})], [profile()])
        counts = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM user_profiles), "
                "(SELECT count(*) FROM user_languages), "
                "(SELECT count(*) FROM admin_users)"
            )
        )

    assert counts.one() == (1, first, 1), "a re-run duplicated rows"


# --------------------------------------------------------------------------
# resolution: report, never substitute
# --------------------------------------------------------------------------


async def test_an_unresolvable_language_is_reported_and_skipped(db_engine: AsyncEngine) -> None:
    """``Avestan`` is a real ISO 639-3 code the M0 seed deliberately excluded.

    No alias fixes that — it needs a decision about the seed. So the row loads
    without that language and the *name* is reported, because the next action is
    to look at it. A nearest-match would file the user under something plausible
    and wrong.
    """
    async with db_engine.begin() as conn:
        counts = await load_everything(
            conn, [user()], [profile(**{"list-Language": "Yoruba , Avestan"})]
        )

        stored = await conn.execute(text("SELECT count(*) FROM user_languages"))

    assert counts.languages_skipped == ("Avestan",)
    assert stored.scalar_one() == 1, "the resolvable language still loaded"


async def test_a_naming_variant_resolves_through_the_alias_table(db_engine: AsyncEngine) -> None:
    """``Abkhaz`` is ISO's ``Abkhazian`` — a variant, unlike ``Avestan``."""
    async with db_engine.begin() as conn:
        counts = await load_everything(conn, [user()], [profile(**{"list-Language": "Abkhaz"})])

        code = await conn.execute(
            text(
                "SELECT l.code_639_3 FROM user_languages ul "
                "JOIN languages l ON l.id = ul.language_id"
            )
        )

    assert counts.languages_skipped == ()
    assert code.scalar_one() == "abk"


async def test_an_unresolvable_country_does_not_discard_the_profile(
    db_engine: AsyncEngine,
) -> None:
    """The bio, avatar and socials are worth more than the country field."""
    async with db_engine.begin() as conn:
        counts = await load_everything(
            conn, [user()], [profile(**{"OriginCountry(text)": "Wakanda"})]
        )

        stored = await conn.execute(text("SELECT about_me, origin_country_id FROM user_profiles"))
        about, country = stored.one()

    assert counts.countries_skipped == ("Wakanda",)
    assert about == "Hello"
    assert country is None


async def test_a_repeated_language_is_counted_once(db_engine: AsyncEngine) -> None:
    """What the transform's dedupe actually buys — and it is not what I assumed.

    The first version of this asserted the stored row count and claimed the
    dedupe stops a duplicate hitting the composite unique index. It does not:
    ``ON CONFLICT (user_id, language_id) DO NOTHING`` absorbs it either way, and
    removing the dedupe left this test green.

    What the dedupe protects is the **reported** count. Without it the loader
    reports two languages written where one row exists, and reconciliation is
    only worth having if its numbers are true. So the assertion is on both.
    """
    async with db_engine.begin() as conn:
        counts = await load_everything(
            conn, [user()], [profile(**{"list-Language": "Yoruba , Yoruba"})]
        )
        stored = await conn.execute(text("SELECT count(*) FROM user_languages"))

    assert stored.scalar_one() == 1
    assert counts.languages == 1, "the reported count disagreed with the rows written"


# --------------------------------------------------------------------------
# auth_identities: the emptiness has to mean something
# --------------------------------------------------------------------------


def test_an_export_can_never_produce_an_identity() -> None:
    """It has ``Registration format`` and no subject id.

    ``provider_user_id`` is NOT NULL, so there is nothing to insert — and the
    plan says so explicitly rather than reporting a bare zero.
    """
    plan = plan_identity([user(**{"Registration format": "Google"})], [])

    assert plan.identities == ()
    assert not plan.source_carries_identities


def test_an_api_extract_produces_one() -> None:
    """``authentication.Google.id``, lifted by the reader, is the subject."""
    plan = plan_identity(
        [
            user(
                **{
                    "Registration format": "Google",
                    "provider_identities": {"google": "104789988730282226729"},
                }
            )
        ],
        [],
    )

    assert plan.source_carries_identities
    assert len(plan.identities) == 1
    assert plan.identities[0].provider_user_id == "104789988730282226729"


def test_an_email_registration_never_produces_an_identity() -> None:
    """``authentication.email`` is the account, not a linked provider. Mapping it
    to one would fail against the ``auth_provider`` enum, well into a load."""
    assert to_identities(user(**{"Registration format": "Email"})) == []


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("Admin 🎩", "Emperor", "unmapped admin role"),
        ("Registration format", "Apple", "unmapped registration"),
    ],
)
def test_an_unmapped_value_raises(field: str, value: str, expected: str) -> None:
    """Each has a plausible silent default — skip the grant, skip the identity.
    Both would produce a migration that looks complete and is not."""
    with pytest.raises(TransformError, match=expected):
        record = user(**{field: value})
        to_admin_grant(record)
        to_identities(record)


def test_a_profile_nobody_points_at_is_reported_not_attributed() -> None:
    """Legacy ``PersonalInfo`` carries no reference to its owner — only a
    ``Creator`` email. Matching on that would attribute a profile to whoever
    happened to create it, which is not the same person."""
    plan = plan_identity([user(**{"👤Personal Info": None})], [profile()])

    assert plan.profiles == ()
    assert plan.orphaned_profiles == (PROFILE_ID,)


async def test_a_profile_referencing_an_unknown_user_raises(db_engine: AsyncEngine) -> None:
    """Silently skipping it would lose a profile with no trace."""
    async with db_engine.begin() as conn:
        plan = plan_identity([user()], [profile()])
        await UserLoader(conn).load(plan.users)
        country_ref, language_ref = await reference_maps(conn)

        with pytest.raises(LookupError, match="unknown user"):
            await SatelliteLoader(conn).load(
                users={"someone-else": uuid4()},
                profiles=plan.profiles,
                onboarding=(),
                identities=(),
                admin_grants=(),
                countries=resolve_names(set(), country_ref, COUNTRY_ALIASES),
                languages=resolve_names(set(), language_ref, LANGUAGE_ALIASES),
            )


def test_the_export_timezone_constant_matches_the_extractor() -> None:
    """Two scripts hold it because ``scripts/`` is not a package. If they drift,
    the same export parses to two different instants depending on which script
    read it."""
    from scripts import extract_bubble, load_identity

    assert str(extract_bubble.EXPORT_TIMEZONE) == str(load_identity.EXPORT_TIMEZONE)


async def test_an_unresolved_name_makes_the_run_exit_two(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed load that skipped a name is not a clean run.

    `resolve.py` says an unresolved name "has to reach a human". Exit 0 is the one
    signal saying nothing needs attention, and during the read-only freeze a green
    run is what the operator acts on — ADR 0003 gives them one attempt.

    **2 rather than 1 is the assertion that matters.** 1 already means "refused,
    nothing written"; a runbook has to tell that apart from "written, now go
    decide something". Asserting merely non-zero would pass if the two collapsed.

    This drives the real ``load()``. Only the DSN is redirected — reimplementing
    its reporting tail here would assert the test's own arithmetic, which is the
    failure mode this file's neighbours exist to catch.
    """
    from scripts import load_identity

    monkeypatch.setattr(
        load_identity, "resolve_async_dsn", lambda _settings: to_async_dsn(migrated_database)
    )

    unresolvable = plan_identity([user()], [profile(**{"list-Language": "Yoruba , Avestan"})])
    assert unresolvable.ok, unresolvable.errors

    assert await load_identity.load(unresolvable) == 2

    resolvable = plan_identity([user()], [profile(**{"list-Language": "Yoruba , English"})])

    assert await load_identity.load(resolvable) == 0, "a clean load must still exit 0"
