"""Reading the catalogues: institution search, and the lookup lists.

Built with SQLAlchemy Core rather than ``text()``, for the reason
``provisioning_store`` gives: **this module composes.** One visibility rule —
"approved, and not merged away" — is shared by institution search and the
scholarship-programme list, and the search itself reuses it across three
statements. An f-string would build SQL by concatenation, which ruff's ``S608``
flags as an injection vector, and a `WHERE` clause that is a real expression
object is a stronger form of "one representation" than a substring that happens
to be pasted in the right places.

THE SEARCH IS TIERED, AND THAT IS MEASURED
==========================================
Against the real 10,250-row catalogue, on a single query combining the tiers with
``OR``:

    q                   ILIKE only   trigram only   OR combined
    La                     36.5 ms        9.4 ms      123.6 ms
    Zz                     23.5 ms        2.3 ms       90.9 ms

The disjunction defeats the planner — the combined query is slower than either
half. Run separately, with the cheap tier first and the expensive one only when
the cheap tiers come up short:

    q                   prefix    substring    trigram
    Lagos                1.4 ms       2.0 ms     7.5 ms
    University           4.3 ms      15.7 ms    75.0 ms
    Univerity of Lagos   2.1 ms       3.0 ms   100.0 ms  <- the typo path

Those are per-tier costs. End to end, a query that **fills its page from the
prefix tier alone** stops there and costs about 6 ms; one that comes up short
runs all three and pays for the fuzzy pass. Measured through this function:

    University    6.1 ms   10 results, prefix only
    La            6.0 ms   10 results, prefix only
    Lagos        14.0 ms    5 results, all three tiers
    Zz           60.8 ms    3 results, all three tiers

So the cost is governed by how many results exist, not by how long the query is
— and the common case, a query with plenty of matches, is the cheap one.

**Fuzzy matching is safe here and nowhere else.** ``domain.institutions.match``
refuses it, because linking an education entry is a decision a machine makes
silently and permanently, and the country of study derives from it. Search
returns a list a human picks from, so a wrong row costs one glance. The
inconsistency is deliberate.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LookupStatus
from app.infra.db.models.education import DegreeLevel, Institution
from app.infra.db.models.mentoring import ServiceOffering
from app.infra.db.models.reference import Country, Language
from app.infra.db.models.scholarships import ScholarshipProgram

#: What `LIKE` treats as a wildcard, and the character that escapes it.
#:
#: **The search term is user-supplied and becomes part of a *pattern*, not just a
#: value.** Binding it as a parameter stops SQL injection and does nothing about
#: this: measured against the real catalogue, `q=%` matched every institution and
#: `q=____________` matched every name of twelve characters or more, while
#: `q=100%` matched nothing where it should find "100% Academy". Wrong results
#: for anyone whose school contains one of these characters, and on a public
#: unauthenticated endpoint a one-character way to make every tier match
#: everything.
#:
#: The trigram tier is deliberately *not* escaped: `%` there is the similarity
#: operator and `similarity()` takes a value, not a pattern.
LIKE_ESCAPE = "\\"

#: Passed explicitly as `ESCAPE` on every pattern below, though PostgreSQL already
#: defaults to backslash — verified against the database, not assumed. So removing
#: it is an **equivalent mutation**: no test can distinguish the two. Recorded
#: here so nobody hunts for the missing case. It stays because it says out loud
#: which character escapes, and because a future `ESCAPE ''` elsewhere would
#: otherwise change these queries silently.


def escape_like(term: str) -> str:
    """Neutralise `LIKE` wildcards in a user-supplied term.

    The escape character is replaced first — doing it last would escape the
    backslashes this function just introduced.
    """
    return (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


#: The similarity floor for the fuzzy tier, applied explicitly rather than left
#: to `pg_trgm.similarity_threshold`. The `%` operator reads that GUC, so a
#: result set that depended on it would change with a database setting nobody in
#: this repository sets. Measured: the typo `Univerity of Lagos` scores 0.773
#: against `University of Lagos`, while unrelated universities land at 0.481, so
#: 0.5 keeps the true match and drops the noise.
SIMILARITY_FLOOR = 0.5

#: "A user may be shown this row." Approved, and not the losing side of a merge.
#:
#: **This belongs to search, never to reading an entity.** An education entry
#: whose institution is `pending_review` must still render its school — that row
#: is exactly the one its own creator is waiting on, and filtering it out of
#: their profile would blank the school they just typed. Search excludes it so
#: nobody else selects an unvetted duplicate; the entity read follows the
#: foreign key unconditionally.
VISIBLE = and_(
    Institution.status == LookupStatus.APPROVED,
    Institution.merged_into_id.is_(None),
)


def _institution_columns() -> Select[Any]:
    """The projection, once.

    `source`, `status`, `merged_into_id`, `created_by` and `last_synced_at` are
    absent deliberately — internal curation state, not something a client has any
    use for. Defined here rather than per-tier so three statements cannot drift
    into three shapes.
    """
    return select(
        Institution.id,
        Institution.name,
        Institution.web_page,
        Country.code.label("country_code"),
        Country.display_name.label("country_name"),
    ).outerjoin(Country, Country.id == Institution.country_id)


async def search_institutions(session: AsyncSession, *, q: str, limit: int) -> list[dict[str, Any]]:
    """Institutions matching ``q``, best first, across three tiers.

    Each tier fills what the previous one left, and a tier is only run when the
    page is still short — so the expensive fuzzy pass is skipped entirely for a
    query that already found its answers.
    """
    term = q.strip()
    if not term:
        # Not an error. An empty box is the normal state of a search field, and
        # a 422 mid-keystroke is a worse answer than an empty list.
        return []

    found: list[dict[str, Any]] = []
    seen: set[UUID] = set()

    # Escaped for the two pattern tiers; the raw term is what the fuzzy tier
    # scores against.
    pattern = escape_like(term)
    lowered = func.lower(pattern)
    tiers = (
        # 1. Prefix. What somebody typing a name hits, and the only tier the
        #    `ix_institutions_name_prefix` btree can serve — the expression must
        #    match the index's `lower(name)` exactly or it is silently unused.
        (
            func.lower(Institution.name).like(lowered + "%", escape=LIKE_ESCAPE),
            Institution.name,
        ),
        # 2. Substring. `University of Lagos` for a search of `Lagos`.
        (Institution.name.ilike("%" + pattern + "%", escape=LIKE_ESCAPE), Institution.name),
        # 3. Fuzzy, for the typo. Bounded by both the `%` operator (so the GIN
        #    trigram index is used) and an explicit floor (so the result does not
        #    depend on a database GUC).
        (
            and_(
                Institution.name.bool_op("%")(term),
                func.similarity(Institution.name, term) >= SIMILARITY_FLOOR,
            ),
            func.similarity(Institution.name, term).desc(),
        ),
    )

    for predicate, ordering in tiers:
        if len(found) >= limit:
            break
        statement = (
            _institution_columns()
            .where(and_(VISIBLE, predicate))
            .order_by(ordering, Institution.name)
            .limit(limit - len(found))
        )
        if seen:
            statement = statement.where(Institution.id.notin_(seen))
        for row in (await session.execute(statement)).mappings():
            found.append(dict(row))
            seen.add(row["id"])

    return found


#: The five lookup lists, by their URL segment.
#:
#: One mapping and one handler rather than five near-identical functions. Five
#: copies is five chances for one to forget a filter, and the forgotten one is
#: the one nobody reads again.
#:
#: `scholarship_programs` is the only **open** catalogue here — users can create
#: rows in it — so it is the only one carrying a visibility filter. The other
#: four are closed vocabularies with no `status` column at all, which is why the
#: filter is per-entry rather than applied to all of them.
LOOKUPS: dict[str, dict[str, Any]] = {
    "degree-levels": {
        "model": DegreeLevel,
        "columns": (DegreeLevel.id, DegreeLevel.slug, DegreeLevel.display_name),
        "order": (DegreeLevel.sort_order, DegreeLevel.display_name),
        "active_only": True,
        "approved_only": False,
        "paged": False,
    },
    "service-offerings": {
        "model": ServiceOffering,
        "columns": (
            ServiceOffering.id,
            ServiceOffering.slug,
            ServiceOffering.display_name,
            ServiceOffering.category,
        ),
        "order": (ServiceOffering.sort_order, ServiceOffering.display_name),
        "active_only": True,
        "approved_only": False,
        "paged": False,
    },
    "scholarship-programs": {
        "model": ScholarshipProgram,
        "columns": (
            ScholarshipProgram.id,
            ScholarshipProgram.slug,
            ScholarshipProgram.display_name,
            ScholarshipProgram.official_url,
        ),
        "order": (ScholarshipProgram.display_name,),
        "active_only": False,
        "approved_only": True,
        "paged": True,
    },
    "countries": {
        "model": Country,
        "columns": (Country.id, Country.code, Country.display_name),
        "order": (Country.display_name,),
        "active_only": False,
        "approved_only": False,
        "paged": True,
    },
    "languages": {
        "model": Language,
        "columns": (Language.id, Language.code_639_3, Language.display_name),
        "order": (Language.display_name,),
        "active_only": False,
        "approved_only": False,
        "paged": True,
    },
}


async def list_lookup(
    session: AsyncSession,
    name: str,
    *,
    q: str | None = None,
    limit: int,
    cursor: tuple[str, UUID] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One lookup catalogue, in display order, filtered and paged.

    **`languages` has 7,078 rows**, not the couple of hundred a lookup list
    suggests, so this is the endpoint that genuinely needs a cursor rather than
    just the envelope. The obvious trim — restrict to the 174 ISO 639-1 codes —
    is wrong here on the schema's own reasoning: 639-3 was chosen precisely
    because the two-letter set omits Nigerian Pidgin (`pcm`), which for this
    platform's market is not an acceptable gap.

    So every lookup takes an optional `q` and pages by keyset. Countries (249)
    and the closed vocabularies (6 each) fit in one page and return
    `next_cursor: null`; languages pages, or is typed into.
    """
    spec = LOOKUPS[name]
    model = spec["model"]
    statement = select(*spec["columns"])

    if spec["active_only"]:
        statement = statement.where(model.is_active.is_(True))
    if spec["approved_only"]:
        # The same rule institution search uses, for the same reason: an
        # unreviewed row a user created must not be offered to everybody else as
        # though it were curated.
        statement = statement.where(
            and_(
                model.status == LookupStatus.APPROVED,
                model.merged_into_id.is_(None),
            )
        )
    if q and q.strip():
        statement = statement.where(
            model.display_name.ilike("%" + escape_like(q.strip()) + "%", escape=LIKE_ESCAPE)
        )

    if not spec["paged"]:
        # A closed vocabulary: six rows, ordered by `sort_order` because
        # "Undergraduate, Masters, PhD" is a sequence a person chose and
        # alphabetical would render it "Masters, PhD, Undergraduate". It cannot
        # outgrow a page, so it needs no cursor and keeps its intended order.
        statement = statement.order_by(*spec["order"])
        rows = [dict(r) for r in (await session.execute(statement)).mappings()]
        return rows, False

    # Keyset, on (display_name, id). `id` breaks ties — without it two rows
    # sharing a display name straddle a page boundary and one is skipped or
    # repeated, which is the classic keyset bug and stays invisible until the
    # data happens to collide. Both columns are in every paged catalogue's
    # projection, so the caller can always build the next cursor.
    statement = statement.order_by(model.display_name, model.id)
    if cursor is not None:
        after_name, after_id = cursor
        statement = statement.where(
            tuple_(model.display_name, model.id) > tuple_(literal(after_name), literal(after_id))
        )

    # One more than asked for: if it comes back, there is a next page. Cheaper
    # and more honest than a second COUNT, which can disagree with the page it
    # claims to describe.
    rows = [dict(r) for r in (await session.execute(statement.limit(limit + 1))).mappings()]
    return rows[:limit], len(rows) > limit
