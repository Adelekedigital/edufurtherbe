"""The two Mentor Settings tables (decisions 275 and 276).

`d6f2a9c4e185` adds `user_links` and copies `social_twitter` and
`social_youtube` into it; `e8b3c5f1a726` adds `mentor_guide_countries`.

Every constraint has an accepting and a rejecting case: a test that only proves
a constraint refuses garbage cannot tell it from one that refuses everything.
The backfill is checked against the values reads publish today, which is the
whole point of freezing the rule into the migration.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.guide_countries import MAX_GUIDE_COUNTRIES
from app.domain.social_links import SocialNetwork, canonical_social
from app.infra.db.models import UserLink

pytestmark = pytest.mark.db

LINKS = "d6f2a9c4e185"
GUIDES = "e8b3c5f1a726"
BEFORE = "d2f8b6a4c1e7"

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions"
_LINKS_FILE = _MIGRATION / "20261019_1200_user_links_d6f2a9c4e185.py"

_PERSON = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentee', 'UTC') RETURNING id) "
    "INSERT INTO user_profiles (user_id, social_twitter, social_youtube) "
    "SELECT id, {twitter}, {youtube} FROM u"
)

_MENTOR = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentor', 'UTC') RETURNING id) "
    "INSERT INTO mentor_profiles (user_id, headline) SELECT id, 'P' FROM u"
)

_LINK = (
    "INSERT INTO user_links (user_id, platform, url{extra_cols}) "
    "SELECT id, {platform}, {url}{extra_vals} FROM users WHERE email = '{email}'"
)

_GUIDE = (
    "INSERT INTO mentor_guide_countries (mentor_user_id, country_id, position) "
    "SELECT u.id, (SELECT id FROM countries ORDER BY code OFFSET {nth} LIMIT 1), {position} "
    "FROM users u WHERE u.email = '{email}'"
)


def _quote(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def _person(url: str, email: str, twitter: str | None, youtube: str | None) -> None:
    execute(url, _PERSON.format(email=email, twitter=_quote(twitter), youtube=_quote(youtube)))


def _link(url: str, email: str, platform: str, link: str, *, kind: str | None = None) -> None:
    extra_cols, extra_vals = ("", "") if kind is None else (", kind", f", {_quote(kind)}")
    execute(
        url,
        _LINK.format(
            email=email,
            platform=_quote(platform),
            url=_quote(link),
            extra_cols=extra_cols,
            extra_vals=extra_vals,
        ),
    )


def _links_of(url: str, email: str) -> list[tuple[str, str, int]]:
    async def _run() -> list[tuple[str, str, int]]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                "SELECT l.platform, l.url, l.position FROM user_links l "
                "JOIN users u ON u.id = l.user_id WHERE u.email = $1 ORDER BY l.position",
                email,
            )
        finally:
            await conn.close()
        return [(r["platform"], r["url"], r["position"]) for r in rows]

    return asyncio.run(_run())


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("user_links_migration", _LINKS_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- The frozen rule --------------------------------------------------------

CASES = [
    "https://x.com/ada",
    "twitter.com/ada",
    "https://mobile.twitter.com/ada/status/1",
    "@ada",
    "ada",
    "x.com/home",
    "https://x.com@evil.com/ada",
    "https://x.com:8080/ada",
    "https://evil.com/ada",
    "a" * 16,
    "not a link",
    "",
    "https://www.youtube.com/@dee",
    "youtube.com/@dee/videos",
    "https://www.youtube.com/channel/UC" + "a" * 22,
    "https://www.youtube.com/c/legacy",
    "@dee",
    "dee.creates",
    "ab",
]


@pytest.mark.parametrize("value", CASES)
@pytest.mark.parametrize("network", [SocialNetwork.X, SocialNetwork.YOUTUBE])
def test_the_frozen_rule_reads_like_the_live_one(network: SocialNetwork, value: str) -> None:
    """The backfill must copy exactly what a read publishes. If the live rule
    changes, this fails: decide then whether the migration's history matters."""
    assert _migration().canonical(network.value, value) == canonical_social(network, value)


# --- The backfill -----------------------------------------------------------


def test_the_backfill_copies_what_reads_publish(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, BEFORE)
    _person(disposable_database, "both@example.test", "twitter.com/ada", "youtube.com/@dee")
    _person(disposable_database, "x@example.test", "https://x.com/bo", None)
    _person(disposable_database, "junk@example.test", "not a link", "https://evil.com/@dee")
    _person(disposable_database, "none@example.test", None, None)

    command.upgrade(config, LINKS)

    assert _links_of(disposable_database, "both@example.test") == [
        ("x", "https://x.com/ada", 0),
        ("youtube", "https://www.youtube.com/@dee", 1),
    ]
    assert _links_of(disposable_database, "x@example.test") == [("x", "https://x.com/bo", 0)]
    assert _links_of(disposable_database, "junk@example.test") == []
    assert _links_of(disposable_database, "none@example.test") == []
    # An unreadable value stays where it was, for the release the column lives.
    assert (
        scalar(
            disposable_database,
            "SELECT p.social_twitter FROM user_profiles p JOIN users u ON u.id = p.user_id "
            "WHERE u.email = 'junk@example.test'",
        )
        == "not a link"
    )


# --- user_links constraints -------------------------------------------------


@pytest.fixture
def links_db(disposable_database: str, make_alembic_config: ConfigFactory) -> str:
    command.upgrade(make_alembic_config(disposable_database), LINKS)
    _person(disposable_database, "owner@example.test", None, None)
    return disposable_database


def test_each_platform_takes_one_link_and_websites_take_several(links_db: str) -> None:
    for platform, link in [
        ("x", "https://x.com/ada"),
        ("youtube", "https://www.youtube.com/@ada"),
        ("instagram", "https://www.instagram.com/ada"),
        ("facebook", "https://www.facebook.com/ada.lovelace"),
        ("tiktok", "https://www.tiktok.com/@ada"),
    ]:
        _link(links_db, "owner@example.test", platform, link)
    # The database admits a fourth website; three is the writer's count.
    for n in range(4):
        _link(links_db, "owner@example.test", "website", f"https://ada{n}.example")
    assert scalar(links_db, "SELECT count(*) FROM user_links") == 9

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_user_links_one_per_platform"):
        _link(links_db, "owner@example.test", "x", "https://x.com/other")


def test_two_people_may_each_hold_the_same_platform(links_db: str) -> None:
    _person(links_db, "second@example.test", None, None)
    _link(links_db, "owner@example.test", "x", "https://x.com/ada")
    _link(links_db, "second@example.test", "x", "https://x.com/ada")
    assert scalar(links_db, "SELECT count(*) FROM user_links") == 2


@pytest.mark.parametrize(
    ("platform", "link", "kind", "constraint"),
    [
        ("linkedin", "https://www.linkedin.com/in/ada", None, "platform_is_known"),
        ("X", "https://x.com/ada", None, "platform_is_known"),
        ("x", "http://x.com/ada", None, "url_shape"),
        ("x", "https://x.com/" + "a" * 2040, None, "url_shape"),
        ("x", "https://x.com/ada", "custom", "kind_is_known"),
    ],
)
def test_a_malformed_link_is_refused(
    links_db: str, platform: str, link: str, kind: str | None, constraint: str
) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match=f"ck_user_links_{constraint}"):
        _link(links_db, "owner@example.test", platform, link, kind=kind)


def test_a_position_is_never_negative(links_db: str) -> None:
    _link(links_db, "owner@example.test", "x", "https://x.com/ada")
    execute(links_db, "UPDATE user_links SET position = 5")
    with pytest.raises(asyncpg.CheckViolationError, match="ck_user_links_position_not_negative"):
        execute(links_db, "UPDATE user_links SET position = -1")


def test_a_link_defaults_to_social_and_goes_with_its_user(links_db: str) -> None:
    _link(links_db, "owner@example.test", "x", "https://x.com/ada")
    assert scalar(links_db, "SELECT kind FROM user_links") == "social"
    execute(links_db, "DELETE FROM user_profiles")
    execute(links_db, "DELETE FROM users WHERE email = 'owner@example.test'")
    assert scalar(links_db, "SELECT count(*) FROM user_links") == 0


def test_the_one_per_platform_index_leaves_websites_out(links_db: str) -> None:
    """`alembic check` cannot compare a partial index's predicate."""
    definition = scalar(
        links_db,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_user_links_one_per_platform'",
    )
    assert "UNIQUE" in definition
    assert "(user_id, platform)" in definition
    assert "WHERE (platform <> 'website'::text)" in definition


def test_the_model_declares_the_same_partial_index(links_db: str) -> None:
    """`alembic check` skips predicates, so the model's `WHERE` could drift from
    the migration's with every gate green; this compares the two."""
    index = next(
        i for i in UserLink.__table__.indexes if i.name == "ix_user_links_one_per_platform"
    )
    where = index.dialect_options["postgresql"]["where"]
    assert index.unique
    assert where is not None and str(where) == "platform <> 'website'"
    definition = scalar(
        links_db,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_user_links_one_per_platform'",
    )
    assert f"WHERE ({where}::text)" in definition, definition


def test_an_edit_moves_updated_at(links_db: str) -> None:
    _link(links_db, "owner@example.test", "x", "https://x.com/ada")
    execute(links_db, "UPDATE user_links SET updated_at = now() - interval '1 day'")
    execute(links_db, "UPDATE user_links SET url = 'https://x.com/bo'")
    assert scalar(links_db, "SELECT updated_at > now() - interval '1 hour' FROM user_links")


# --- The links downgrade ----------------------------------------------------


def test_the_downgrade_returns_x_and_youtube_to_their_columns(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, BEFORE)
    _person(disposable_database, "back@example.test", "twitter.com/ada", None)
    command.upgrade(config, LINKS)
    # Written after the upgrade, as the next release's writer does.
    execute(disposable_database, "UPDATE user_profiles SET social_twitter = NULL")
    _link(disposable_database, "back@example.test", "youtube", "https://www.youtube.com/@dee")

    command.downgrade(config, BEFORE)

    row = scalar(
        disposable_database,
        "SELECT social_twitter || ' ' || social_youtube FROM user_profiles",
    )
    assert row == "https://x.com/ada https://www.youtube.com/@dee"
    assert scalar(disposable_database, "SELECT to_regclass('user_links')") is None
    command.upgrade(config, LINKS)


def test_the_downgrade_refuses_while_a_link_has_no_column(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, LINKS)
    _person(disposable_database, "insta@example.test", None, None)
    _link(disposable_database, "insta@example.test", "instagram", "https://www.instagram.com/ada")

    with pytest.raises(RuntimeError, match="no column to return to"):
        command.downgrade(config, BEFORE)

    execute(disposable_database, "DELETE FROM user_links")
    command.downgrade(config, BEFORE)
    assert scalar(disposable_database, "SELECT to_regclass('user_links')") is None


# --- mentor_guide_countries -------------------------------------------------


@pytest.fixture
def guides_db(disposable_database: str, make_alembic_config: ConfigFactory) -> str:
    command.upgrade(make_alembic_config(disposable_database), GUIDES)
    execute(disposable_database, _MENTOR.format(email="mentor@example.test"))
    return disposable_database


def _guide(url: str, email: str, nth: int, position: int) -> None:
    execute(url, _GUIDE.format(email=email, nth=nth, position=position))


def test_a_mentor_names_up_to_eight_countries_and_no_ninth(guides_db: str) -> None:
    for n in range(MAX_GUIDE_COUNTRIES):
        _guide(guides_db, "mentor@example.test", n, n)
    assert scalar(guides_db, "SELECT count(*) FROM mentor_guide_countries") == MAX_GUIDE_COUNTRIES

    with pytest.raises(
        asyncpg.CheckViolationError, match="ck_mentor_guide_countries_position_in_range"
    ):
        _guide(guides_db, "mentor@example.test", MAX_GUIDE_COUNTRIES, MAX_GUIDE_COUNTRIES)
    with pytest.raises(asyncpg.UniqueViolationError, match="ix_mentor_guide_countries_position"):
        _guide(guides_db, "mentor@example.test", MAX_GUIDE_COUNTRIES, 0)


def test_the_eight_is_the_domains(guides_db: str) -> None:
    definition = scalar(
        guides_db,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_mentor_guide_countries_position_in_range'",
    )
    assert f'("position" < {MAX_GUIDE_COUNTRIES})' in definition, definition


def test_a_country_appears_once_per_mentor(guides_db: str) -> None:
    execute(guides_db, _MENTOR.format(email="other@example.test"))
    _guide(guides_db, "mentor@example.test", 0, 0)
    _guide(guides_db, "other@example.test", 0, 0)
    with pytest.raises(asyncpg.UniqueViolationError, match="ix_mentor_guide_countries_pair"):
        _guide(guides_db, "mentor@example.test", 0, 1)


def test_a_position_is_never_negative_for_a_country(guides_db: str) -> None:
    with pytest.raises(
        asyncpg.CheckViolationError, match="ck_mentor_guide_countries_position_in_range"
    ):
        _guide(guides_db, "mentor@example.test", 0, -1)


def test_only_a_mentor_names_guide_countries(guides_db: str) -> None:
    _person(guides_db, "mentee@example.test", None, None)
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="fk_mentor_guide_countries_mentor"):
        _guide(guides_db, "mentee@example.test", 0, 0)


def test_the_countries_go_with_the_mentor_profile_and_hold_their_country(guides_db: str) -> None:
    _guide(guides_db, "mentor@example.test", 0, 0)
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="fk_mentor_guide_countries_country"):
        execute(
            guides_db,
            "DELETE FROM countries WHERE id = (SELECT country_id FROM mentor_guide_countries)",
        )
    execute(guides_db, "DELETE FROM mentor_profiles")
    assert scalar(guides_db, "SELECT count(*) FROM mentor_guide_countries") == 0


def test_the_guides_downgrade_refuses_while_a_mentor_named_one(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, GUIDES)
    execute(disposable_database, _MENTOR.format(email="mentor@example.test"))
    _guide(disposable_database, "mentor@example.test", 0, 0)

    with pytest.raises(RuntimeError, match="guide-to country"):
        command.downgrade(config, LINKS)

    execute(disposable_database, "DELETE FROM mentor_guide_countries")
    command.downgrade(config, LINKS)
    assert scalar(disposable_database, "SELECT to_regclass('mentor_guide_countries')") is None
    command.upgrade(config, GUIDES)
