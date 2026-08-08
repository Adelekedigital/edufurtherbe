"""The four M2 catalogues: what they were seeded with, and what they refuse.

Most of this cannot be reached by ``alembic check``, which sees tables, columns,
types and regular indexes and is blind to CHECK constraints, partial indexes and
triggers. A green migration check means the chain applies, not that it is
correct.

The seed assertions are exact rather than "at least". A vocabulary that grew
without anybody deciding to is precisely the failure worth catching — for
``service_offerings`` especially, since its six rows are the matching axis and a
seventh would change what every mentor and mentee can select.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# The six parents of the legacy option set. **This tuple is the tripwire.**
# Bubble held one option set used by both sides, stored as text at the moment of
# selection — so the mentee column carries six parents and the mentor column
# carries five parents plus five children and renames. Collapsing to these six is
# what makes a mentee's need and a mentor's offer the same row.
SERVICE_OFFERING_SLUGS = (
    "test-preparation",
    "document-preparation",
    "school-selection",
    "program-selection",
    "scholarships-financial-aid",
    "interview-preparation",
)

DEGREE_LEVEL_SLUGS = (
    "undergraduate",
    "diploma",
    "masters",
    "mba",
    "phd",
    "postdoc",
)

SCHOLARSHIP_COUNT = 10
# Fulbright and Knight-Hennessy (US), Chevening, Commonwealth, Rhodes and Skoll
# (GB), DAAD (DE). MasterCard Foundation and the two assistantships are
# deliberately unattributed.
SCHOLARSHIPS_WITH_A_COUNTRY = 7


async def country_id(conn: AsyncConnection, code: str = "NG") -> uuid.UUID:
    result = await conn.execute(text("SELECT id FROM countries WHERE code = :c"), {"c": code})
    return result.scalar_one()


async def add_institution(
    conn: AsyncConnection,
    name: str,
    *,
    domain: str | None = None,
    source: str = "hipolabs",
    country: uuid.UUID | None = None,
) -> uuid.UUID:
    result = await conn.execute(
        text(
            "INSERT INTO institutions (name, domain, country_id, source) "
            "VALUES (:n, :d, :c, :s) RETURNING id"
        ),
        {"n": name, "d": domain, "c": country or await country_id(conn), "s": source},
    )
    return result.scalar_one()


# --------------------------------------------------------------------------
# The seeds
# --------------------------------------------------------------------------


async def test_the_six_service_offerings_are_exactly_the_agreed_set(
    db_engine: AsyncEngine,
) -> None:
    """A seventh parent must be a decision, not a drift.

    Splitting or renaming a parent later is the expensive move — this table has
    no ``merged_into_id``, so every junction row pointing at a changed parent
    would have to be repointed by hand with nothing recording that it happened.
    All future specificity belongs in children instead, which is additive.
    """
    async with db_engine.connect() as conn:
        result = await conn.execute(text("SELECT slug FROM service_offerings ORDER BY sort_order"))
        slugs = tuple(result.scalars())

    assert slugs == SERVICE_OFFERING_SLUGS


async def test_degree_levels_are_seeded_from_the_package(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        result = await conn.execute(text("SELECT slug FROM degree_levels ORDER BY sort_order"))
        slugs = tuple(result.scalars())

    assert slugs == DEGREE_LEVEL_SLUGS


async def test_scholarship_programs_are_seeded_with_countries_resolved(
    db_engine: AsyncEngine,
) -> None:
    """Country is resolved by ISO code at insert, never by a literal id.

    Reference ids are generated per environment, so a literal would be correct in
    one database and silently wrong in every other. A code that failed to resolve
    would leave a null ``country_id`` — indistinguishable from one of the three
    programmes deliberately left unattributed, which is why the count matters.
    """
    async with db_engine.connect() as conn:
        total = await conn.execute(text("SELECT count(*) FROM scholarship_programs"))
        attributed = await conn.execute(
            text("SELECT count(*) FROM scholarship_programs WHERE country_id IS NOT NULL")
        )
        chevening = await conn.execute(
            text(
                "SELECT c.code FROM scholarship_programs s JOIN countries c ON c.id = s.country_id "
                "WHERE s.slug = 'chevening'"
            )
        )

    assert total.scalar_one() == SCHOLARSHIP_COUNT
    assert attributed.scalar_one() == SCHOLARSHIPS_WITH_A_COUNTRY
    assert chevening.scalar_one() == "GB"


async def test_seeded_scholarships_carry_no_funding_type_or_degree_levels(
    db_engine: AsyncEngine,
) -> None:
    """Both are left empty deliberately, so filling them is a visible change.

    Reliable coverage data exists for perhaps four of the ten, and a
    half-populated column reads as authoritative to whoever next writes a filter
    against it. ``degree_levels`` is additionally a ``text[]`` of slugs with no
    foreign key behind it, so nothing would catch a slug rename.
    """
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM scholarship_programs "
                "WHERE funding_type IS NOT NULL OR cardinality(degree_levels) > 0"
            )
        )

    assert result.scalar_one() == 0


async def test_institutions_ships_empty(db_engine: AsyncEngine) -> None:
    """The catalogue is loaded by a sync, never by a migration (ADR 0020).

    Still correct after mirroring was adopted, and for a sharper reason than
    when it was written: 10,257 rows in a migration would make the chain slow,
    unreplayable and wrong within a fortnight, since the source changes every
    ~2 days. `scripts/sync_institutions.py` fills the table, which is why a
    fresh environment starts empty until that runs.
    """
    async with db_engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM institutions"))

    assert result.scalar_one() == 0


# --------------------------------------------------------------------------
# institutions.domain — unique, and nullable on purpose
# --------------------------------------------------------------------------


async def test_two_institutions_cannot_share_a_domain(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await add_institution(conn, "University of Lagos", domain="unilag.edu.ng")

        with pytest.raises(IntegrityError):
            await add_institution(conn, "Unilag", domain="unilag.edu.ng")


async def test_many_manual_institutions_may_have_no_domain(db_engine: AsyncEngine) -> None:
    """The positive case, and it is what makes the registry survivable.

    PostgreSQL treats nulls as distinct, so any number of ``source='manual'``
    rows coexist without a domain while two hipolabs rows can never share one.
    ADR 0008 accepts a known coverage gap for African institutions on exactly
    this basis — if a null domain collided, ``source='manual'`` would not be a
    fallback at all.
    """
    async with db_engine.begin() as conn:
        for name in ("Bells University", "Elizade University", "Kings University"):
            await add_institution(conn, name, domain=None, source="manual")

        result = await conn.execute(text("SELECT count(*) FROM institutions WHERE domain IS NULL"))

    assert result.scalar_one() == 3


# --------------------------------------------------------------------------
# The foreign keys and the CHECK
# --------------------------------------------------------------------------


async def test_an_unknown_country_is_rejected(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await add_institution(conn, "Nowhere University", country=uuid.uuid4())


async def test_a_referenced_country_cannot_be_deleted(db_engine: AsyncEngine) -> None:
    """RESTRICT, spelled out rather than left to NO ACTION (ADR 0013).

    Deleting a country must not take every education entry in it along.
    """
    async with db_engine.begin() as conn:
        nigeria = await country_id(conn)
        await add_institution(conn, "University of Ibadan", domain="ui.edu.ng", country=nigeria)

        with pytest.raises(IntegrityError):
            await conn.execute(text("DELETE FROM countries WHERE id = :c"), {"c": nigeria})


async def test_an_unknown_source_is_rejected(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await add_institution(conn, "Somewhere", source="wikipedia")


@pytest.mark.parametrize("source", ["hipolabs", "manual", "ror"])
async def test_every_declared_source_is_accepted(db_engine: AsyncEngine, source: str) -> None:
    """The positive case for all three, including ``'ror'``.

    Nothing writes ``'ror'`` — ADR 0008 keeps it as a provenance label rather
    than an implementation claim, and removing it would foreclose the documented
    revisit. A CHECK that silently stopped accepting it would be invisible until
    the day somebody needed it.
    """
    async with db_engine.begin() as conn:
        institution = await add_institution(conn, f"Test {source}", source=source)

    assert institution is not None


async def test_a_merged_institution_still_points_at_its_survivor(db_engine: AsyncEngine) -> None:
    """The losing row of a merge survives, which is the point of ``merged_into_id``.

    A client holding a cached reference to the duplicate still resolves rather
    than 404ing. That is why ``MERGED`` is a status and not a delete.
    """
    async with db_engine.begin() as conn:
        winner = await add_institution(conn, "University of Lagos", domain="unilag.edu.ng")
        loser = await add_institution(conn, "Unilag", domain=None, source="manual")
        await conn.execute(
            text("UPDATE institutions SET merged_into_id = :w, status = 'merged' WHERE id = :l"),
            {"w": winner, "l": loser},
        )

        result = await conn.execute(
            text(
                "SELECT w.name FROM institutions l JOIN institutions w ON w.id = l.merged_into_id "
                "WHERE l.id = :l"
            ),
            {"l": loser},
        )

    assert result.scalar_one() == "University of Lagos"


async def test_status_defaults_to_approved(db_engine: AsyncEngine) -> None:
    """Records the fail-open default so that flipping it is a visible change.

    A write path that forgets to set ``status`` publishes a user-created row
    globally. The default flips to ``'pending_review'`` in the pull request that
    ships admin curation; until then every user-facing write path must set it
    explicitly. This test is what turns that flip into a deliberate edit rather
    than a silent behaviour change.
    """
    async with db_engine.begin() as conn:
        institution = await add_institution(conn, "Default Status University")
        result = await conn.execute(
            text("SELECT status::text FROM institutions WHERE id = :i"), {"i": institution}
        )

    assert result.scalar_one() == "approved"


# --------------------------------------------------------------------------
# pg_trgm
# --------------------------------------------------------------------------


async def test_trigram_similarity_is_operational(db_engine: AsyncEngine) -> None:
    """Proves the extension *and* its operator class, which are separate facts.

    ``CREATE EXTENSION`` succeeding does not mean ``gin_trgm_ops`` resolves: the
    extension installs into ``public`` locally and into Supabase's ``extensions``
    schema in production, and an index referencing an operator class that is not
    on the search path fails at ``CREATE INDEX`` with the extension already
    present. The migration would have caught that; this catches a later
    environment where it stops being true.

    The numbers are worth reading. A single-character typo scores **0.773**,
    below the ``> 0.85`` auto-link threshold the package specifies for the M2
    institution matching pass — so that threshold would miss the exact case it
    was written for. Asserted here so the fact is visible when the matching pass
    is designed.
    """
    async with db_engine.connect() as conn:
        typo = await conn.execute(
            text("SELECT similarity('University of Lagos', 'Univerity of Lagos')")
        )
        different = await conn.execute(
            text("SELECT similarity('University of Lagos', 'University of Ghana')")
        )

    typo_score = typo.scalar_one()
    assert 0.75 < typo_score < 0.80
    assert typo_score < 0.85, "a one-character typo scores below the package's auto-link threshold"
    assert different.scalar_one() < typo_score
