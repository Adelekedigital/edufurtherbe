"""The public catalogue reads: institution search and the lookup lists.

**These endpoints take no token, and that is the thing most worth asserting.**
An endpoint meant to be public that quietly starts refusing anonymous callers
breaks signup — where somebody picks their school before an account exists — and
it breaks it for exactly the users who cannot report it.

The other load-bearing assertion is what search *excludes*. A `pending_review`
institution must not be offered to everybody else, or the review queue mints the
duplicates it exists to prevent.
"""

from __future__ import annotations

from types import ModuleType
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SEARCH = "/api/v1/institutions"
CATALOG = "/api/v1/catalog"


async def add_institution(
    engine: AsyncEngine,
    name: str,
    *,
    domain: str | None = None,
    status: str = "approved",
    merged_into: UUID | None = None,
) -> UUID:
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "INSERT INTO institutions (name, domain, status, merged_into_id, source) "
                "VALUES (:n, :d, CAST(:s AS lookup_status), :m, 'hipolabs') RETURNING id"
            ),
            {"n": name, "d": domain, "s": status, "m": merged_into},
        )
        return row.scalar_one()


def names(response: httpx.Response) -> list[str]:
    return [row["name"] for row in response.json()["data"]]


# --------------------------------------------------------------------------
# Public access — the property the whole design rests on
# --------------------------------------------------------------------------


async def test_search_needs_no_token(api_client: httpx.AsyncClient) -> None:
    """Signup asks for a school before an account exists. If this ever starts
    returning 401, that flow breaks for people who cannot work around it."""
    response = await api_client.get(SEARCH, params={"q": "anything"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "catalogue",
    ["degree-levels", "service-offerings", "scholarship-programs", "countries", "languages"],
)
async def test_every_lookup_needs_no_token(api_client: httpx.AsyncClient, catalogue: str) -> None:
    response = await api_client.get(f"{CATALOG}/{catalogue}")

    assert response.status_code == 200
    assert response.json()["data"], f"{catalogue} came back empty"


# --------------------------------------------------------------------------
# Search: ranking, and what it refuses to show
# --------------------------------------------------------------------------


async def test_a_prefix_match_outranks_a_substring_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Somebody typing `Lagos` means a school called Lagos-something before one
    that merely contains the word."""
    await add_institution(db_engine, "University of Lagos", domain="unilag.edu.ng")
    await add_institution(db_engine, "Lagos State University", domain="lasu.edu.ng")

    found = names(await api_client.get(SEARCH, params={"q": "Lagos"}))

    assert found.index("Lagos State University") < found.index("University of Lagos")


async def test_a_typo_still_finds_the_school(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The fuzzy tier, which only runs because prefix and substring both miss."""
    await add_institution(db_engine, "University of Lagos", domain="unilag.edu.ng")

    found = names(await api_client.get(SEARCH, params={"q": "Univerity of Lagos"}))

    assert found == ["University of Lagos"]


async def test_a_short_typo_is_honestly_not_rescued(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Pinning a **limitation**, not a feature.

    Six letters share too few trigrams with a long name, so `Oxfrod` finds
    nothing at any usable floor — catching it would need ~0.4, which floods the
    list with unrelated universities. The route description says so; this is what
    stops that claim quietly becoming false.
    """
    await add_institution(db_engine, "Oxford Brookes University", domain="brookes.ac.uk")

    assert names(await api_client.get(SEARCH, params={"q": "Oxfrod"})) == []


async def test_a_pending_institution_is_not_offered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The row one user typed yesterday is not shown to everybody else today.

    Offering it is how the review queue mints duplicates instead of preventing
    them — five people picking five unvetted spellings of one university.
    """
    await add_institution(db_engine, "Unlisted Polytechnic", status="pending_review")

    assert names(await api_client.get(SEARCH, params={"q": "Unlisted"})) == []


async def test_a_merged_institution_is_not_offered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Selecting the losing side of a merge would recreate the duplicate an
    admin has just resolved."""
    target = await add_institution(db_engine, "Real University", domain="real.edu")
    await add_institution(db_engine, "Reel University", domain="reel.edu", merged_into=target)

    assert names(await api_client.get(SEARCH, params={"q": "University"})) == ["Real University"]


async def test_an_approved_institution_is_offered(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive case. Without it, excluding everything would satisfy the two
    tests above."""
    await add_institution(db_engine, "Findable University", domain="findable.edu")

    assert names(await api_client.get(SEARCH, params={"q": "Findable"})) == ["Findable University"]


async def test_the_country_comes_back_nested(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO institutions (name, domain, country_id, source) "
                "SELECT 'Country University', 'cu.edu.ng', id, 'hipolabs' "
                "FROM countries WHERE code = 'NG'"
            )
        )

    row = (await api_client.get(SEARCH, params={"q": "Country"})).json()["data"][0]

    assert row["country"] == {"code": "NG", "display_name": "Nigeria"}


async def test_a_manual_institution_with_no_country_still_serialises(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`country_id` is nullable so an admin can complete it at review. The
    response model has to survive that, not 500 on it."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, primary_role, timezone) "
                "VALUES ('creator@example.com', 'mentee', 'UTC')"
            )
        )
        # `created_by` is required for a `manual` row — `ck_institutions_manual_names_its_creator`,
        # added with the write endpoints. A user-created institution always names
        # who asked for it; a mirrored one never does.
        await conn.execute(
            text(
                "INSERT INTO institutions (name, source, created_by) "
                "SELECT 'Countryless College', 'manual', id FROM users LIMIT 1"
            )
        )

    row = (await api_client.get(SEARCH, params={"q": "Countryless"})).json()["data"][0]

    assert row["country"] is None


@pytest.mark.parametrize("q", ["", "   "])
async def test_an_empty_query_is_an_empty_list_not_an_error(
    api_client: httpx.AsyncClient, q: str
) -> None:
    """An empty search box is the normal state of a search box. A 422 mid-keystroke
    is a worse answer than nothing."""
    response = await api_client.get(SEARCH, params={"q": q})

    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_the_limit_is_capped(api_client: httpx.AsyncClient) -> None:
    """Refused rather than clamped at the boundary, because FastAPI validates it
    before the handler — the clamp exists for the `None` path."""
    assert (await api_client.get(SEARCH, params={"q": "a", "limit": 999})).status_code == 422


async def test_no_curation_state_leaks(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`status`, `source`, `merged_into_id`, `created_by` and `last_synced_at`
    describe how a row got here and who is reviewing it. A public endpoint
    exposing them publishes the shape of the moderation queue."""
    await add_institution(db_engine, "Opaque University", domain="opaque.edu")

    row = (await api_client.get(SEARCH, params={"q": "Opaque"})).json()["data"][0]

    assert set(row) == {"id", "name", "web_page", "country"}


# --------------------------------------------------------------------------
# The lookup lists
# --------------------------------------------------------------------------


async def test_degree_levels_keep_their_intended_order(api_client: httpx.AsyncClient) -> None:
    """`sort_order`, not alphabetical — and the order is ISCED ascending, so the
    dropdown reads as a progression. Sorting by name would render it
    "Bachelor's, Certificate, Doctorate, Master's", which reads as a bug to
    every user who sees it."""
    found = [
        row["display_name"]
        for row in (await api_client.get(f"{CATALOG}/degree-levels")).json()["data"]
    ]

    assert found[0] == "Certificate / Diploma"
    assert found != sorted(found)


async def test_a_closed_vocabulary_returns_whole_with_no_cursor(
    api_client: httpx.AsyncClient,
) -> None:
    body = (await api_client.get(f"{CATALOG}/service-offerings")).json()

    assert body["next_cursor"] is None
    assert len(body["data"]) == 6


async def test_countries_fit_in_one_page(api_client: httpx.AsyncClient) -> None:
    """249 rows, and a client rendering a select box wants all of them."""
    body = (await api_client.get(f"{CATALOG}/countries")).json()

    assert len(body["data"]) == 249
    assert body["next_cursor"] is None


async def test_languages_page_without_skipping_or_repeating(
    api_client: httpx.AsyncClient,
) -> None:
    """7,078 rows — the one list that genuinely pages.

    The seam is what this asserts. Ordering on `display_name` alone lets two rows
    sharing a name straddle the boundary, so one is silently dropped; the `id`
    tie-break is what prevents it.
    """
    first = (await api_client.get(f"{CATALOG}/languages", params={"limit": 5})).json()
    assert first["next_cursor"] is not None

    second = (
        await api_client.get(
            f"{CATALOG}/languages", params={"limit": 5, "cursor": first["next_cursor"]}
        )
    ).json()

    ids = {row["id"] for row in first["data"]}
    assert not ids & {row["id"] for row in second["data"]}, "a row appeared on both pages"
    assert first["data"][-1]["display_name"] <= second["data"][0]["display_name"]


async def test_languages_can_be_searched(api_client: httpx.AsyncClient) -> None:
    """Why the table is ISO 639-3 at all: the two-letter set omits Nigerian
    Pidgin, and this platform's market cannot lose it."""
    body = (await api_client.get(f"{CATALOG}/languages", params={"q": "Nigerian Pidgin"})).json()

    assert [row["display_name"] for row in body["data"]] == ["Nigerian Pidgin"]


async def test_a_language_search_ranks_the_exact_match_first(
    api_client: httpx.AsyncClient,
) -> None:
    """**The bug a real search exposed.**

    Twenty of the 7,078 ISO 639-3 names contain "English" — Antigua and Barbuda
    Creole English, Bahamas Creole English, and so on. Ordered alphabetically,
    the language the user meant came fifth. Ranking exact, then prefix, then
    anywhere puts it first, with no curation of the underlying list.
    """
    body = (await api_client.get(f"{CATALOG}/languages", params={"q": "english"})).json()

    assert body["data"][0]["display_name"] == "English"


async def test_a_lookup_search_does_not_claim_another_page(
    api_client: httpx.AsyncClient,
) -> None:
    """A search narrows by typing rather than paging — a cursor would have to
    order by the ranking instead of the keyset columns, which is how a keyset
    silently skips rows."""
    body = (await api_client.get(f"{CATALOG}/languages", params={"q": "english"})).json()

    assert body["next_cursor"] is None


async def test_the_common_set_is_the_default_picker(api_client: httpx.AsyncClient) -> None:
    """100 languages rather than 7,078. ISO 639-3 is a completeness registry and
    not a picker — searching "english" returned twenty creoles before ranking,
    and scrolling 7,078 rows was never going to work at all."""
    body = (await api_client.get(f"{CATALOG}/languages", params={"common": "true"})).json()

    names = {row["display_name"] for row in body["data"]}
    assert len(body["data"]) == 100
    assert {"English", "Yoruba", "Igbo", "Hausa", "Amharic", "Zulu", "Afrikaans"} <= names


async def test_the_long_tail_is_absent_from_the_picker_and_present_in_search(
    api_client: httpx.AsyncClient,
) -> None:
    """**Why the table was not simply replaced by the common set.**

    These are Nigerian languages on a platform for African students. Deleting
    them to shrink the picker would have been a coverage gap pointed at our own
    users, and would reverse the reasoning that put this table on 639-3 rather
    than 639-1.
    """
    common = (await api_client.get(f"{CATALOG}/languages", params={"common": "true"})).json()
    listed = {row["display_name"] for row in common["data"]}

    for name in ("Efik", "Ibibio", "Tiv", "Kanuri", "Idoma", "Urhobo"):
        assert name not in listed, f"{name} is in the default picker"
        found = (await api_client.get(f"{CATALOG}/languages", params={"q": name})).json()
        assert name in {row["display_name"] for row in found["data"]}, f"{name} is unsearchable"


async def test_nigerian_pidgin_is_excluded_from_the_default_and_still_selectable(
    api_client: httpx.AsyncClient,
) -> None:
    """Excluded **by decision, not by the standard** — CLDR includes `pcm` at
    modern tier. The 639-3 choice that put it in the table still stands: it has
    to be selectable, which it is."""
    common = (await api_client.get(f"{CATALOG}/languages", params={"common": "true"})).json()
    searched = (
        await api_client.get(f"{CATALOG}/languages", params={"q": "Nigerian Pidgin"})
    ).json()

    assert "Nigerian Pidgin" not in {row["display_name"] for row in common["data"]}
    assert [row["display_name"] for row in searched["data"]] == ["Nigerian Pidgin"]


async def test_asking_for_the_common_set_of_a_catalogue_without_one_is_refused(
    api_client: httpx.AsyncClient,
) -> None:
    """Named rather than ignored: a filter silently doing nothing is how a
    client ships believing it filtered."""
    response = await api_client.get(f"{CATALOG}/countries", params={"common": "true"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_the_seeded_common_set_matches_the_migrations_literal(
    db_engine: AsyncEngine, common_languages_migration: ModuleType
) -> None:
    """**The test that keeps the seed honest.**

    The migration holds a literal, because a migration must never fetch. The
    first version of that literal was six codes wrong — three invented, three
    dropped — because it was written out by hand instead of pasted from the
    script. This compares the two without touching the network: the script's
    module-level constant against what actually landed in the table.
    """
    expected = set(common_languages_migration.COMMON_LANGUAGES)

    async with db_engine.connect() as conn:
        rows = await conn.execute(text("SELECT code_639_3 FROM languages WHERE is_common"))
        seeded = {row[0].strip() for row in rows}

    assert seeded == expected, (
        f"only in the table: {sorted(seeded - expected)}; "
        f"only in the literal: {sorted(expected - seeded)}"
    )


async def test_a_pending_scholarship_programme_is_not_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The only *open* catalogue among the lookups, so the only one needing the
    filter — and the reason the handler applies it per-catalogue rather than to
    all five."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO scholarship_programs (display_name, status) "
                "VALUES ('Unvetted Award', CAST('pending_review' AS lookup_status))"
            )
        )

    body = (await api_client.get(f"{CATALOG}/scholarship-programs")).json()

    assert "Unvetted Award" not in [row["display_name"] for row in body["data"]]


async def test_an_unknown_catalogue_is_404(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(f"{CATALOG}/nonsense")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_forged_cursor_is_refused(api_client: httpx.AsyncClient) -> None:
    """Not silently treated as "start from the beginning" — that answers a paging
    bug with page one forever, which looks like working software and loses rows."""
    response = await api_client.get(f"{CATALOG}/languages", params={"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_the_catalogue_route_does_not_shadow_me(
    api_client: httpx.AsyncClient,
) -> None:
    """`/{catalogue}` at the prefix root would match `/api/v1/me`, with router
    registration order deciding the winner. It lives under `/catalog/` so it
    cannot. Without a token `/me` is 401 — a 404 would mean the catch-all ate it.
    """
    assert (await api_client.get("/api/v1/me")).status_code == 401


async def test_a_wildcard_is_searched_for_literally(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`%` and `_` are `LIKE` wildcards, and the search term is user-supplied.

    Binding the term as a parameter stops SQL injection and does nothing about
    this: unescaped, `q=%` matched **every** institution and `q=100%` matched
    **none**, because the pattern rather than the value was under the caller's
    control. On a public endpoint that is also a one-character way to make every
    tier match everything.
    """
    await add_institution(db_engine, "100% Academy", domain="hundred.edu")
    await add_institution(db_engine, "Ordinary College", domain="ordinary.edu")

    assert names(await api_client.get(SEARCH, params={"q": "100%"})) == ["100% Academy"]
    assert names(await api_client.get(SEARCH, params={"q": "%"})) == ["100% Academy"]


async def test_an_underscore_is_searched_for_literally(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`_` matches any single character, so unescaped it turns a search into a
    length filter — `____________` returned every name of twelve or more."""
    await add_institution(db_engine, "Under_score College", domain="under.edu")
    await add_institution(db_engine, "Underscore College", domain="under2.edu")

    assert names(await api_client.get(SEARCH, params={"q": "Under_"})) == ["Under_score College"]


async def test_a_backslash_is_searched_for_literally(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The escape character itself, which is the case ordering gets wrong.

    `escape_like` must double the backslashes **first**. Doing it after the
    wildcards would escape the backslashes it had just introduced, turning
    `%` into a literal backslash followed by a live wildcard. A mutation showed
    no test could tell, because no fixture name contained one.
    """
    await add_institution(db_engine, r"Back\slash Institute", domain="back.edu")

    assert names(await api_client.get(SEARCH, params={"q": "Back\\"})) == [r"Back\slash Institute"]


async def test_a_wildcard_in_a_lookup_filter_is_literal_too(
    api_client: httpx.AsyncClient,
) -> None:
    """The same rule on the other query that takes a user term."""
    body = (await api_client.get(f"{CATALOG}/languages", params={"q": "%"})).json()

    assert body["data"] == [], "the lookup filter treated % as a wildcard"


async def test_an_unexpected_failure_is_still_problem_details() -> None:
    """**The shape of a 500, which nothing asserted before.**

    Found by pointing the application at an unmigrated database: the missing
    table raised `ProgrammingError`, Starlette's default handler answered
    `text/plain`, and a client parsing JSON in its error path failed a second
    time. ADR 0016 promises *every* failure is Problem Details, and only
    deliberate `AppError`s were registered.

    Its own app and its own client, deliberately. The shared `api_client`
    fixture leaves `raise_app_exceptions` on so an unexpected error in any other
    test surfaces as a traceback rather than a quiet 500 — turning that off
    globally to serve this one test would blind every other one.
    """
    from fastapi import FastAPI

    from app.api import errors

    app = FastAPI()
    errors.register(app)

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError('asyncpg: relation "institutions" does not exist')

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/explode")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
    }
    # A database error names tables and columns; a driver error can name a host.
    assert "institutions" not in response.text, "the underlying error leaked to the caller"


def test_the_prefix_index_is_declared_on_the_model() -> None:
    """`alembic check` cannot see this one.

    It reports "Cannot compare index 'ix_institutions_name_prefix' ... detected
    as including operator clause" and skips it, so model-versus-migration drift
    on this index is invisible to the gate. Autocomplete's common tier is a
    sequential scan without it, and nothing else would say so.
    """
    from app.infra.db.models.education import Institution

    declared = {index.name for index in Institution.__table__.indexes}

    assert "ix_institutions_name_prefix" in declared


async def test_the_prefix_index_exists_in_the_database(db_engine: AsyncEngine) -> None:
    """The other half: declared on the model *and* created by the migration."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'institutions' AND indexname = 'ix_institutions_name_prefix'"
            )
        )
        definition = result.scalar_one_or_none()

    assert definition is not None, "the migration did not create the prefix index"
    assert "lower(name)" in definition
    assert "text_pattern_ops" in definition
