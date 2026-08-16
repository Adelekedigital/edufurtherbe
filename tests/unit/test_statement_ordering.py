"""Every statement whose order a reader can see orders by something unique.

Three so far — mentor browse, mentor search, and the education list on a public
profile. This file began as the search one alone and is now named for the
invariant rather than the endpoint, because that is the third instance of one
shape: an ordering introduced deliberately, described accurately in prose, and
asserted nowhere. Each was found by mutation rather than by any gate.

**A rank is not unique, and ties are the normal case rather than an edge one.**
`ts_rank_cd` scores a document, so two similar profiles matching a term the same
way score identically — measured at 5,000 mentors, a term matching all of them
produced **one** distinct rank. Search pages on `OFFSET`, so an order that is
undefined between two queries means a mentor is returned on both pages while
another is returned on neither.

Asserted against the compiled statement rather than by paging tied rows, and the
reason is that the behavioural version can pass for the wrong reason: PostgreSQL
may return a stable order for the same plan by coincidence, so a green run would
prove nothing on the day the plan changes. The structural check cannot pass by
luck. `test_predicates.py` already reads compiled SQL for the same kind of
invariant — one that lives inside a statement, where no linter can see it.

The tiebreak was already load-bearing before this file existed:
`test_a_name_match_outranks_a_country_match` is built around it, creating its
name match *first* so that winning on the tiebreak cannot be mistaken for
winning on weights. It relies on the ordering and would not catch its removal —
its two mentors hold different ranks, so it would turn flaky rather than red.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.infra.db.mentor_search_store import _page, _ranked
from app.infra.db.profile_store import _education_statement

#: The column that makes the mentor orders total. It is the cursor for browse and
#: the tiebreak for search, which is why both modes name it.
MENTOR_KEY = "mentor_profiles.id"

#: Education lists rows of a different table, so its total order ends elsewhere.
EDUCATION_KEY = "education_entries.id"


def order_by(statement: object) -> str:
    """The **outer** `ORDER BY` of a compiled statement, whitespace collapsed.

    **Depth, not position.** The first version took the first `ORDER BY` in the
    text, which was the only one until the card's qualification lateral arrived
    with its own — `sort_order, date_end, id`, picking one education entry — and
    that one renders *before* the outer clause. The test then read a clause it
    was never about.

    It failed loudly, but only by luck: the lateral orders by
    `education_entries.id`, and had it ordered by `mentor_profiles.id` instead,
    `test_the_order_is_total` would have gone green while inspecting the wrong
    statement entirely. A helper that assumes one of anything is a helper waiting
    for the second one.

    Depth-zero is the property that actually distinguishes them: a subquery's
    clause is inside parentheses and the statement's own is not, whatever order
    they appear in and however many subqueries a future rank expression grows.
    """
    compiled = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]

    depth = 0
    for index, character in enumerate(compiled):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif depth == 0 and compiled.startswith("ORDER BY", index):
            tail = compiled[index + len("ORDER BY") :]
            clause = re.split(r"\s+LIMIT|\s+OFFSET", tail, maxsplit=1)[0]
            return " ".join(clause.split())

    raise AssertionError(f"no top-level ORDER BY in: {compiled[:200]}")


@pytest.mark.parametrize(
    ("mode", "statement", "unique_key"),
    [
        ("browse", _page(None, 20), MENTOR_KEY),
        ("search", _ranked("ada", 0, 20), MENTOR_KEY),
        ("education", _education_statement(uuid4()), EDUCATION_KEY),
    ],
)
def test_the_order_is_total(mode: str, statement: object, unique_key: str) -> None:
    """No listed statement may order by something two rows can share.

    Browse orders by the key alone; search orders by rank *then* the key;
    education orders by two dates and then the key. All three end up naming one,
    and any that stopped would return rows in an order the database is free to
    change between two identical requests.

    The key differs per statement, which is why it is parametrised rather than a
    module constant — education's rows are `education_entries`, not mentors.
    """
    clause = order_by(statement)

    assert unique_key in clause, f"{mode} orders by {clause!r}, which two rows can tie on"


def test_search_ranks_before_it_breaks_the_tie() -> None:
    """Order matters within the clause: rank first, key second.

    Reversed, the key would decide every comparison and the rank would only
    settle exact `id` collisions — which never happen. Search would silently
    become browse, returning the right rows in the wrong order, and every
    relevance test would still pass because each of them asserts on a set.
    """
    clause = order_by(_ranked("ada", 0, 20))

    assert clause.index("ts_rank_cd") < clause.index(MENTOR_KEY)


def test_education_sorts_by_when_a_degree_ended_not_when_it_started() -> None:
    """Which date leads is a product decision, and swapping them broke nothing.

    The list moved off `is_most_recent` — blank on every migrated row, so it
    decided nothing while reading as though it did — onto `date_end`, then
    `date_start`, then the key. Reversing the first two leaves every test green
    while silently changing what a profile shows first: a part-time master's
    spanning a doctorate orders differently by start than by end.

    Not reachable in today's data — no mentor has two entries whose orderings
    disagree — which is exactly why prose was the only thing holding it.
    """
    clause = order_by(_education_statement(uuid4()))

    assert clause.index("date_end") < clause.index("date_start")
