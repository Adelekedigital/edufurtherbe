"""The claim and the send-time check share one eligibility rule.

They drifted once — only one of them read `LIVE` — and the gate cannot see a
condition missing from a second copy. This pins both call sites to
`reminder_eligible()` and forbids either from restating a condition it owns.
"""

from __future__ import annotations

import inspect

import pytest

from app.infra.db import mentor_listing, mentor_status_store

CALLERS = [
    mentor_status_store.remind_returning_mentors,
    mentor_listing.return_reminder_state,
]

#: Conditions only `reminder_eligible` may spell out.
OWNED = ("LIVE", "ApprovalStatus.APPROVED", "paused_by_mentor()", "deleted_at.is_(None)")


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.__name__)
def test_each_side_reads_the_shared_rule(caller: object) -> None:
    source = inspect.getsource(caller)  # type: ignore[arg-type]

    assert "reminder_eligible()" in source
    assert [term for term in OWNED if term in source] == []
