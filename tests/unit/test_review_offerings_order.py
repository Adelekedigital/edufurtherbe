"""A review's offerings have one order even where positions tie (decision 282).

`session_type_offerings.position` defaults to 0 and is not unique, so a set
written outside `set_session_type_offerings` can hold two rows at the same
position. PostgreSQL does not keep a sort stable, so the order is pinned here,
on the statement, because no database test can make an unstable sort fail on
demand.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.infra.db.review_topics import offerings_column


def test_a_tied_position_falls_back_to_the_platforms_order() -> None:
    sql = str(select(offerings_column()).compile(dialect=postgresql.dialect()))

    assert "ORDER BY session_type_offerings.position, service_offerings.sort_order" in sql
