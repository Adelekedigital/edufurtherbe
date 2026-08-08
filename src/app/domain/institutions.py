"""Turning a hipolabs record into an institution, and a school name into a link.

Pure: dictionaries in, dataclasses out, no I/O. The catalogue is fetched in
``infra`` and handed here as data, so every mapping and matching decision is
testable without a network call — which matters more than usual, because the
source is a third party with no SLA and a test that fetched it would be flaky by
construction.

**Matching is exact, or case-folded, or nothing.** There is no fuzzy tier, and
that is a measurement rather than a preference: against real school names a
genuine typo scores 0.773 while `Federal University of Technology, Yola` and
`… Akure` — two different Nigerian universities — score 0.750. No threshold
separates them. A wrong link is silent and permanent, and the country of study
derives from it; a miss is visible and recoverable, because
``education_entries.school_name_raw`` is always kept (ADR 0008).

Measured against the dev extract, exact matching alone links **18 of 18** distinct
school names, including 5 of 5 African institutions. The mechanism explains it:
the legacy application populated those names from hipolabs autocomplete, so they
are hipolabs' own strings.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: The record shape, from the source repository's own documentation.
NAME = "name"
DOMAINS = "domains"
WEB_PAGES = "web_pages"
ALPHA_TWO = "alpha_two_code"


class CatalogueError(ValueError):
    """A catalogue record this transform will not guess about."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class CatalogueRow:
    """One institution, as the mirror will store it."""

    name: str
    #: The natural key. ``domains`` is an array upstream; the first entry is
    #: taken, because a second domain is an alias rather than a second school.
    domain: str
    #: Display only. A university changes its website — scheme, ``www``, a
    #: redesign — without changing its domain, which is why the key is the
    #: domain and not this.
    web_page: str | None
    #: Left as a code. Resolving it to a `countries.id` needs a query, and this
    #: module does not have one.
    country_code: str


def to_catalogue_row(record: dict[str, Any]) -> CatalogueRow:
    """One hipolabs record, or a refusal naming what was wrong with it.

    Both refusals are measured as impossible against the current catalogue — 0 of
    10,257 records lack a name or a domain. They raise anyway: "impossible today"
    is a statement about a file that changes every two days.
    """
    name = str(record.get(NAME) or "").strip()
    if not name:
        raise CatalogueError(f"record has no name: {sorted(record)[:4]}")

    domains = [str(d).strip().casefold() for d in (record.get(DOMAINS) or []) if str(d).strip()]
    if not domains:
        # No key means no upsert target, and inventing one from the name would
        # produce a row that can never be deduplicated against the real entry.
        raise CatalogueError(f"{name}: no domain, so no natural key")

    pages = [str(p).strip() for p in (record.get(WEB_PAGES) or []) if str(p).strip()]
    return CatalogueRow(
        name=name,
        domain=domains[0],
        web_page=pages[0] if pages else None,
        country_code=str(record.get(ALPHA_TWO) or "").strip().upper(),
    )


def collapse_by_domain(
    rows: Sequence[CatalogueRow],
) -> tuple[list[CatalogueRow], tuple[str, ...]]:
    """One row per domain, keeping the first, naming the domains that collided.

    Two upstream records share `khio.no` and two share `jazanu.edu.sa` — a merged
    art school, and a college inside a university. One domain is one institution,
    so collapsing them is right.

    **Collapsing here rather than letting ``ON CONFLICT`` absorb it is what stops
    the row churning.** Written separately, the second record rewrites the first
    on every sync, forever, while the stored content is identical every time —
    so ``updated_at`` would come to mean "a sync ran", which is precisely what
    ``last_synced_at`` exists to say. A conditional upsert does not help: the two
    records genuinely differ, so the write is never a no-op.

    First wins, deliberately and deterministically — the same rule
    ``index_names`` uses, rather than whichever record the loop reached last.
    """
    kept: dict[str, CatalogueRow] = {}
    collided: set[str] = set()
    for row in rows:
        if row.domain in kept:
            collided.add(row.domain)
            continue
        kept[row.domain] = row
    return list(kept.values()), tuple(sorted(collided))


@dataclass(frozen=True, slots=True)
class NameIndex[T]:
    """Institution names, with the ones that identify nobody held separately.

    Generic in the mapped value because both callers want a different one: the
    mirror maps a name to a domain, the link pass to an institution id.

    **A name shared by two institutions is not a match, it is a question.**
    Upstream carries 73 exactly-duplicated names over 158 records, and they cross
    borders: `City University` is a university in the United States, in
    Bangladesh *and* in the United Kingdom. A plain dictionary keeps whichever
    row it saw last, from a ``SELECT`` with no ``ORDER BY`` — so the link would be
    a coin toss that changes between runs, and the country of study is derived
    from whichever side won. That is the failure this module's no-fuzzy-matching
    rule exists to prevent, arriving through the exact path instead.
    """

    exact: dict[str, T]
    folded: dict[str, T]
    #: Names carried by more than one institution, verbatim.
    ambiguous: frozenset[str]
    #: Case-folded forms carried by more than one institution. Kept apart from
    #: ``ambiguous`` because an exact hit is still unambiguous when two rows
    #: differ only in case — the same ordering the matcher uses below.
    ambiguous_folded: frozenset[str]

    def is_ambiguous(self, name: str) -> bool:
        """Whether this name identifies more than one institution.

        For reporting. ``match`` uses it too, so the rule has one definition
        rather than a copy in whatever decides how to describe a miss.
        """
        cleaned = name.strip()
        if cleaned in self.ambiguous:
            return True
        return cleaned not in self.exact and cleaned.casefold() in self.ambiguous_folded


def index_names[T](rows: Iterable[tuple[str, T]]) -> NameIndex[T]:
    """Build the lookup, counting collisions rather than discarding them."""
    exact: dict[str, T] = {}
    folded: dict[str, T] = {}
    seen: Counter[str] = Counter()
    seen_folded: Counter[str] = Counter()

    for name, value in rows:
        seen[name] += 1
        seen_folded[name.casefold()] += 1
        exact.setdefault(name, value)
        folded.setdefault(name.casefold(), value)

    return NameIndex(
        exact=exact,
        folded=folded,
        ambiguous=frozenset(n for n, count in seen.items() if count > 1),
        ambiguous_folded=frozenset(n for n, count in seen_folded.items() if count > 1),
    )


def match[T](name: str, index: NameIndex[T]) -> T | None:
    """A legacy school name to a mapped value, or ``None``.

    Exact is tried before case-folded, so that where two institutions differ only
    in case an exact hit stays unambiguous — the same ordering ``resolve_names``
    uses for countries and languages.

    Returns ``None`` rather than a best guess, for a miss and for an ambiguity
    alike. That is the whole design: the caller leaves ``institution_id`` null,
    ``school_name_raw`` still renders, and the entry can be linked later by
    somebody who can tell the two universities apart.
    """
    cleaned = name.strip()
    if not cleaned or index.is_ambiguous(cleaned):
        return None
    if cleaned in index.exact:
        return index.exact[cleaned]
    return index.folded.get(cleaned.casefold())
