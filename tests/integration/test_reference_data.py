"""The seeded reference data, and the guarantees that need a live database.

Row counts are asserted exactly. A future ISO refresh will change them and this
test will fail — that is the intent. The refresh is a deliberate act producing a
new migration, and a count that drifted without anybody noticing would mean the
seed had changed under a chain that is supposed to be immutable.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

COUNTRY_COUNT = 249
LANGUAGE_COUNT = 7078

# Only 174 of the living set carry a two-letter code. The gap is the whole reason
# this table is keyed on 639-3.
LANGUAGES_WITH_639_1 = 174


async def scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as conn:
        result = await conn.execute(text(sql))
        return result.scalar_one()


async def test_countries_are_seeded(db_engine: AsyncEngine) -> None:
    assert await scalar(db_engine, "SELECT count(*) FROM countries") == COUNTRY_COUNT


async def test_languages_are_seeded(db_engine: AsyncEngine) -> None:
    assert await scalar(db_engine, "SELECT count(*) FROM languages") == LANGUAGE_COUNT


async def test_nigerian_pidgin_is_present_without_a_two_letter_code(
    db_engine: AsyncEngine,
) -> None:
    """The case that justifies ISO 639-3 over 639-1.

    The two-letter set omits Nigerian Pidgin entirely. If this row is missing, the
    table has been seeded from the wrong standard and a large part of this
    platform's market cannot state the language they actually speak.
    """
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT display_name, code_639_1 FROM languages WHERE code_639_3 = 'pcm'")
        )
        row = result.one()

    assert row.display_name == "Nigerian Pidgin"
    assert row.code_639_1 is None


async def test_macrolanguages_are_included(db_engine: AsyncEngine) -> None:
    """``ara`` and ``swa`` are scope M, and are kept deliberately.

    For "what languages do you speak" the macrolanguage is the right granularity.
    Filtering them out would leave a picker demanding a choice between thirty
    Arabic variants.
    """
    count = await scalar(
        db_engine, "SELECT count(*) FROM languages WHERE code_639_3 IN ('ara', 'swa')"
    )

    assert count == 2


async def test_most_languages_have_no_two_letter_code(db_engine: AsyncEngine) -> None:
    count = await scalar(db_engine, "SELECT count(*) FROM languages WHERE code_639_1 IS NOT NULL")

    assert count == LANGUAGES_WITH_639_1


async def test_a_country_round_trips_without_padding(db_engine: AsyncEngine) -> None:
    """``char(n)`` blank-pads shorter values, so this checks the codes are exact."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT code, code_alpha3, display_name FROM countries WHERE code = 'NG'")
        )
        row = result.one()

    assert row.code == "NG"
    assert row.code_alpha3 == "NGA"
    assert row.display_name == "Nigeria"


async def test_every_table_with_updated_at_has_the_trigger(db_engine: AsyncEngine) -> None:
    """The half of the timestamp guarantee that a model cannot show.

    Columns with a ``server_default`` get a value at INSERT whether or not a
    trigger exists — so a missing trigger looks completely normal until somebody
    notices ``updated_at`` has never moved on a row that has been edited for
    months. The handoff specified this check as a CI query and nothing had ever
    run it.
    """
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("""
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE c.relkind = 'r'
                  AND n.nspname = 'public'
                  AND a.attname = 'updated_at'
                  AND NOT a.attisdropped
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_trigger t
                      WHERE t.tgrelid = c.oid
                        AND t.tgname = 'trg_set_updated_at'
                        AND NOT t.tgisinternal
                  )
                ORDER BY c.relname
            """)
        )
        untriggered = [row.relname for row in result]

        covered = await conn.execute(
            text("""
                SELECT count(DISTINCT c.relname)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE c.relkind = 'r' AND n.nspname = 'public'
                  AND a.attname = 'updated_at' AND NOT a.attisdropped
            """)
        )
        table_count = covered.scalar_one()

    assert untriggered == [], f"tables whose updated_at will never move: {untriggered}"
    assert table_count > 0, "no tables inspected; this assertion would pass on an empty schema"


async def test_updated_at_actually_moves_on_a_seeded_table(db_engine: AsyncEngine) -> None:
    """End to end, on a real table rather than a probe.

    Two details make this test work, and getting either wrong makes it pass or
    fail for the wrong reason:

    **The trigger has to be disabled to plant the old timestamp.** It overwrites
    ``updated_at`` unconditionally, so an ordinary ``UPDATE`` cannot age a row
    backwards — the value written is replaced by ``now()`` on the way in. Disabling
    it is also the supported way to load source timestamps during an import.

    **The two writes must be in separate transactions.** ``now()`` returns the
    transaction's start time and does not advance within it, so a single
    transaction would produce identical before and after values whether the
    trigger fired or not.
    """
    async with db_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE countries DISABLE TRIGGER trg_set_updated_at"))
        await conn.execute(
            text("UPDATE countries SET updated_at = '2019-01-01T00:00:00Z' WHERE code = 'NG'")
        )
        await conn.execute(text("ALTER TABLE countries ENABLE TRIGGER trg_set_updated_at"))

    async with db_engine.begin() as conn:
        before = (
            await conn.execute(text("SELECT updated_at FROM countries WHERE code = 'NG'"))
        ).scalar_one()

        await conn.execute(text("UPDATE countries SET display_name = 'Nigeria' WHERE code = 'NG'"))

        after = (
            await conn.execute(text("SELECT updated_at FROM countries WHERE code = 'NG'"))
        ).scalar_one()

    assert before.year == 2019, "the trigger was not disabled; the old value never landed"
    assert after > before
