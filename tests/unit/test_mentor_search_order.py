"""Both pages of mentor discovery order by something unique.

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

import pytest
from sqlalchemy.dialects import postgresql

from app.infra.db.mentor_search_store import _page, _ranked

#: The column that makes an order total. It is the cursor for browse and the
#: tiebreak for search, which is why both modes name it.
UNIQUE_KEY = "mentor_profiles.id"


def order_by(statement: object) -> str:
    """The `ORDER BY` clause of a compiled statement, whitespace collapsed."""
    compiled = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    match = re.search(r"ORDER BY(.*?)(?:\s+LIMIT|\s+OFFSET|$)", compiled, re.S)
    assert match is not None, f"no ORDER BY at all in: {compiled[:200]}"
    return " ".join(match.group(1).split())


@pytest.mark.parametrize(
    ("mode", "statement"),
    [("browse", _page(None, 20)), ("search", _ranked("ada", 0, 20))],
)
def test_the_order_is_total(mode: str, statement: object) -> None:
    """Neither mode may order by something two rows can share.

    Browse orders by the key alone; search orders by rank *then* the key. Both
    end up naming it, and a mode that stopped would page non-deterministically.
    """
    clause = order_by(statement)

    assert UNIQUE_KEY in clause, f"{mode} orders by {clause!r}, which two rows can tie on"


def test_search_ranks_before_it_breaks_the_tie() -> None:
    """Order matters within the clause: rank first, key second.

    Reversed, the key would decide every comparison and the rank would only
    settle exact `id` collisions — which never happen. Search would silently
    become browse, returning the right rows in the wrong order, and every
    relevance test would still pass because each of them asserts on a set.
    """
    clause = order_by(_ranked("ada", 0, 20))

    assert clause.index("ts_rank_cd") < clause.index(UNIQUE_KEY)
