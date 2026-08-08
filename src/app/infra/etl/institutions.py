"""Mirroring the catalogue, and linking education entries to it.

Two passes, deliberately separate. The mirror writes what the source says; the
link resolves ``school_name_raw`` against what the mirror holds. Keeping them
apart is what makes matching **re-runnable** — the link is an ``UPDATE`` over
rows that already exist, so it can be re-run after a refresh without reloading
anything (ADR 0020).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.institutions import (
    CatalogueRow,
    collapse_by_domain,
    index_names,
    match,
)

# `updated_at` is set explicitly rather than left to the trigger, which the
# caller holds off for this statement.
#
# Every row the sync saw is stamped with `last_synced_at`, including unchanged
# ones — that is what makes `max(last_synced_at)` mean "when we last refreshed"
# rather than "when something last changed upstream". So there is nothing to skip
# and no reason for a change-detection WHERE clause.
#
# `updated_at` is a different question and must still mean "this row's content
# changed", so it moves only when something actually differs. Both facts, one
# statement, one pass.
UPSERT_INSTITUTION = """
INSERT INTO institutions (name, domain, country_id, web_page, source, last_synced_at)
VALUES (:name, :domain, :country_id, :web_page, 'hipolabs', :synced_at)
ON CONFLICT (domain) DO UPDATE SET
    name           = EXCLUDED.name,
    country_id     = EXCLUDED.country_id,
    web_page       = EXCLUDED.web_page,
    last_synced_at = EXCLUDED.last_synced_at,
    updated_at     = CASE
        WHEN institutions.name       IS DISTINCT FROM EXCLUDED.name
          OR institutions.country_id IS DISTINCT FROM EXCLUDED.country_id
          OR institutions.web_page   IS DISTINCT FROM EXCLUDED.web_page
        THEN :synced_at
        ELSE institutions.updated_at
    END
"""

LINK_EDUCATION = """
UPDATE education_entries SET institution_id = :institution_id
WHERE id = :id AND institution_id IS NULL
"""


@dataclass(frozen=True, slots=True)
class MirrorCounts:
    seen: int = 0
    written: int = 0
    #: Records whose country code is not in `countries`. Skipped, never given a
    #: wrong country — measured at 5 of 10,257, all `XK` (Kosovo), which is a
    #: user-assigned code rather than official ISO 3166-1.
    skipped_no_country: int = 0
    #: Distinct domains carried by more than one upstream record. Collapsed to
    #: one row before writing — two names, one institution — and named here, or
    #: the report claims more records reached the table than did.
    collapsed_domains: tuple[str, ...] = ()
    unresolved_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LinkCounts:
    considered: int = 0
    linked: int = 0
    #: Names the catalogue does not carry. The institution needs adding.
    unmatched: tuple[str, ...] = ()
    #: Names the catalogue carries **more than once**. Not a gap — a question
    #: only a person can answer, since `City University` is three universities
    #: on three continents and the country of study follows the answer.
    ambiguous: tuple[str, ...] = ()


async def country_ids(connection: AsyncConnection) -> dict[str, UUID]:
    """ISO alpha-2 code to `countries.id`.

    Keyed on the code rather than the display name: hipolabs supplies
    `alpha_two_code`, and its country *names* do not always match ISO's.
    """
    result = await connection.execute(text("SELECT code, id FROM countries"))
    return {row.code: row.id for row in result}


async def mirror(
    connection: AsyncConnection,
    rows: Sequence[CatalogueRow],
    countries: dict[str, UUID],
    *,
    synced_at: datetime,
) -> MirrorCounts:
    """Write the catalogue, stamping every row it saw.

    The caller must hold ``trg_set_updated_at`` off — the statement sets
    ``updated_at`` itself, precisely so an unchanged row keeps the timestamp it
    had.
    """
    # Collapsed *before* the write, not absorbed by `ON CONFLICT` afterwards:
    # writing both would rewrite the row on every sync forever, moving
    # `updated_at` on content that never changed.
    deduplicated, duplicated = collapse_by_domain(rows)
    unresolved: set[str] = set()

    written = 0
    skipped = 0
    for row in deduplicated:
        country_id = countries.get(row.country_code)
        if country_id is None:
            # Reported by code, never defaulted. A wrong country propagates into
            # "who studied in the UK" and nothing would ever surface it.
            unresolved.add(row.country_code or "(blank)")
            skipped += 1
            continue

        await connection.execute(
            text(UPSERT_INSTITUTION),
            {
                "name": row.name,
                "domain": row.domain,
                "country_id": country_id,
                "web_page": row.web_page,
                "synced_at": synced_at,
            },
        )
        written += 1

    return MirrorCounts(
        seen=len(rows),
        written=written,
        skipped_no_country=skipped,
        collapsed_domains=duplicated,
        unresolved_codes=tuple(sorted(unresolved)),
    )


async def link_education(connection: AsyncConnection) -> LinkCounts:
    """Resolve unlinked ``school_name_raw`` values against the mirror.

    Matching happens in ``domain.institutions.match`` rather than in SQL. A
    ``lower(a) = lower(b)`` join would be shorter and would put the rule — exact,
    then case-folded, never fuzzy — somewhere no unit test can reach it. The same
    reason ``resolve_names`` takes a reference map instead of doing its own
    lookup.
    """
    names = await connection.execute(text("SELECT name, id FROM institutions"))
    index = index_names((row.name, row.id) for row in names)

    pending = await connection.execute(
        text(
            "SELECT id, school_name_raw FROM education_entries "
            "WHERE institution_id IS NULL AND deleted_at IS NULL"
        )
    )
    entries = [(row.id, row.school_name_raw) for row in pending]

    linked = 0
    unmatched: set[str] = set()
    ambiguous: set[str] = set()
    for entry_id, school in entries:
        institution_id = match(school, index)
        if institution_id is None:
            # The entry keeps `school_name_raw` and stays linkable later. This is
            # what makes an incomplete catalogue a display concern rather than
            # data loss (ADR 0008 point 5).
            #
            # Ambiguity is reported apart from a miss because the two need
            # different work: a miss wants the institution added, an ambiguity
            # wants somebody to say *which* `City University` this person
            # attended. Folding them together would bury the second in the first.
            (ambiguous if index.is_ambiguous(school) else unmatched).add(school)
            continue
        await connection.execute(
            text(LINK_EDUCATION), {"id": entry_id, "institution_id": institution_id}
        )
        linked += 1

    return LinkCounts(
        considered=len(entries),
        linked=linked,
        unmatched=tuple(sorted(unmatched)),
        ambiguous=tuple(sorted(ambiguous)),
    )
