"""The settled-decisions table is cited by number, so its numbers must be unique.

**This is a collision the branch model produces on its own.** Every parallel
branch appends a row and picks *the next number* from the table as it stood when
that branch was cut, so two branches in flight both pick the same one. Neither
conflicts — they append in different places, or the same place and both sides are
kept — and the result is two decisions answering to one number in a document
whose whole purpose is to be referenced by it.

It had happened three times before this test existed: 104, 136 and 137. Two of
those were live at once, which is what makes the count worth asserting rather
than the diff worth reading — nobody notices a duplicate in a 155-row table.

The renumbering that came with this test moved the *uncited* occurrence in each
pair. `#104` is cited from `api/schemas/session_types.py` and from
`test_api_me_session_type_writes.py`, both meaning the booking-notice row, so
that one kept its number and `str_enum()` moved to 153.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

TABLE = (
    Path(__file__).resolve().parents[2] / ".claude" / "skills" / "project-conventions" / "SKILL.md"
)

#: A settled-decision row: a leading pipe, the number, then the claim.
ROW = re.compile(r"^\| (\d+) \|", re.MULTILINE)


def numbers() -> list[int]:
    return [int(n) for n in ROW.findall(TABLE.read_text(encoding="utf-8"))]


def test_there_are_rows_to_check() -> None:
    """A pattern that matches nothing passes every assertion below it.

    The same guard the ADR structure test opens with, and for the same reason:
    a table this is pointed at by a wrong path is a table with no duplicates.
    """
    assert len(numbers()) > 100


def test_every_decision_has_its_own_number() -> None:
    """No two rows answer to the same reference.

    Reported as the offending numbers rather than as a count, because the fix is
    to renumber a specific row and the failure should say which.
    """
    repeated = sorted(n for n, count in Counter(numbers()).items() if count > 1)
    assert repeated == [], (
        f"settled decisions sharing a number: {repeated}. Two branches in flight "
        "each appended the next free row. Renumber the occurrence nothing cites — "
        "grep the repository for the number first, because a cited row keeps it."
    )
