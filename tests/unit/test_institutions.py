"""The catalogue transform and the matching rule.

Pure, so none of this fetches anything. That is the point of keeping the fetch in
``infra``: the source is a third party with no SLA, and a test that reached it
would be flaky by construction and would go red on somebody else's outage.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.institutions import CatalogueError, index_names, match, to_catalogue_row


def record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "University of Lagos",
        "domains": ["unilag.edu.ng"],
        "web_pages": ["https://unilag.edu.ng/"],
        "country": "Nigeria",
        "alpha_two_code": "NG",
        "state-province": None,
    }
    return base | overrides


# --------------------------------------------------------------------------
# The record transform
# --------------------------------------------------------------------------


def test_a_record_becomes_a_row() -> None:
    row = to_catalogue_row(record())

    assert row.name == "University of Lagos"
    assert row.domain == "unilag.edu.ng"
    assert row.web_page == "https://unilag.edu.ng/"
    assert row.country_code == "NG"


def test_the_first_domain_and_page_are_taken() -> None:
    """Both are arrays upstream. A second domain is an alias for one school, not
    a second school, so the first is the key and the rest are dropped."""
    row = to_catalogue_row(
        record(domains=["unilag.edu.ng", "unilag.ng"], web_pages=["https://a/", "https://b/"])
    )

    assert row.domain == "unilag.edu.ng"
    assert row.web_page == "https://a/"


def test_the_domain_is_case_folded() -> None:
    """It is the natural key and the unique constraint is exact, so two casings
    of one domain would otherwise become two institutions."""
    assert to_catalogue_row(record(domains=["UniLag.EDU.ng"])).domain == "unilag.edu.ng"


def test_a_record_with_no_domain_is_refused() -> None:
    """No key means no upsert target. Inventing one from the name would produce a
    row that can never be deduplicated against the real entry.

    Measured at 0 of 10,257 records today — which is a statement about a file
    that changes every two days, not a reason to skip the check.
    """
    with pytest.raises(CatalogueError, match="no domain"):
        to_catalogue_row(record(domains=[]))


def test_a_record_with_no_name_is_refused() -> None:
    with pytest.raises(CatalogueError, match="no name"):
        to_catalogue_row(record(name="   "))


def test_a_record_with_no_web_page_is_still_usable() -> None:
    """The page is display only. Refusing over it would drop a real institution
    for a missing link."""
    assert to_catalogue_row(record(web_pages=[])).web_page is None


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

INDEX = index_names([("University of Lagos", "a"), ("MIT", "b")])


def test_an_exact_name_matches() -> None:
    assert match("University of Lagos", INDEX) == "a"


def test_a_differently_cased_name_matches() -> None:
    assert match("UNIVERSITY OF LAGOS", INDEX) == "a"


def test_surrounding_whitespace_does_not_prevent_a_match() -> None:
    assert match("  University of Lagos  ", INDEX) == "a"


@pytest.mark.parametrize(
    "name",
    [
        "Univerity of Lagos",  # one character short — a real typo
        "University of Lagos Akoka",
        "Unilag",
        "University of Ilorin",
        "",
    ],
)
def test_anything_short_of_an_exact_name_does_not_match(name: str) -> None:
    """**There is no fuzzy tier, and that is measured rather than preferred.**

    Against real school names a genuine typo scores 0.773 while
    ``Federal University of Technology, Yola`` and ``… Akure`` — two different
    Nigerian universities — score 0.750. No threshold separates them, and the
    two failures are not symmetric: a wrong link is silent and permanent, and the
    country of study derives from it, while a miss is visible and recoverable
    because ``school_name_raw`` is always kept.
    """
    assert match(name, INDEX) is None


def test_an_exact_hit_wins_over_a_folded_one() -> None:
    """If two institutions differ only in case, the exact hit is unambiguous and
    the folded one is a coin toss. Same ordering ``resolve_names`` uses for
    countries and languages. Real: `GateWay Community College` and
    `Gateway Community College` are both in the catalogue."""
    index = index_names([("Mit", "exact-hit"), ("mit", "folded-hit")])

    assert match("Mit", index) == "exact-hit"
    assert match("mit", index) == "folded-hit"


def test_a_name_the_catalogue_holds_twice_does_not_match() -> None:
    """**The bug this rule exists for.** `City University` is a real university
    in the United States, in Bangladesh and in the United Kingdom — 73 names over
    158 records collide this way.

    A plain dictionary keeps whichever row it saw last, from a ``SELECT`` with no
    ``ORDER BY``, so the link is a coin toss that can change between runs — and
    the country of study is derived from whichever side won. Silent, permanent,
    and exactly what the no-fuzzy-matching rule exists to prevent.
    """
    index = index_names([("City University", "us"), ("City University", "gb")])

    assert match("City University", index) is None
    assert index.is_ambiguous("City University")


def test_an_ambiguity_only_in_case_still_blocks_the_folded_match() -> None:
    """The exact hits stay usable; only the coin toss is refused."""
    index = index_names([("GateWay Community College", "a"), ("Gateway Community College", "b")])

    assert match("GateWay Community College", index) == "a"
    assert match("Gateway Community College", index) == "b"
    # Neither spelling — nothing to prefer between two real colleges.
    assert match("GATEWAY COMMUNITY COLLEGE", index) is None


def test_an_unambiguous_name_is_not_reported_as_ambiguous() -> None:
    """The positive case. Without it, `is_ambiguous` returning True always would
    pass every test above."""
    assert not INDEX.is_ambiguous("University of Lagos")
    assert not INDEX.is_ambiguous("Never Heard Of It")


def test_the_index_keeps_one_entry_per_distinct_name() -> None:
    index = index_names([("A", 1), ("A", 2), ("B", 3)])

    assert index.ambiguous == frozenset({"A"})
    assert set(index.exact) == {"A", "B"}
