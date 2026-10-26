"""One deadline for an unanswered request, in Python and in SQL (#8).

`request_deadline` is what a message says; `pending_requests._past_deadline`
is what the sweep, accept, decline and the reminder decide on. They are pinned
together here, because a copy that drifts tells a mentor one deadline and
enforces another.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.dialects import postgresql

from app.domain.sessions import request_deadline
from app.infra.db.pending_requests import _past_deadline

STARTS = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.UTC)
RESPOND_BY = dt.datetime(2026, 11, 1, 21, 0, tzinfo=dt.UTC)


def test_the_deadline_is_respond_by_else_the_start() -> None:
    assert request_deadline(RESPOND_BY, STARTS) == RESPOND_BY
    assert request_deadline(None, STARTS) == STARTS


def test_the_sql_deadline_is_the_same_coalesce() -> None:
    compiled = str(_past_deadline(STARTS).compile(dialect=postgresql.dialect()))

    assert "coalesce(sessions.respond_by, sessions.starts_at)" in compiled
