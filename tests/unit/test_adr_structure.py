"""Every decision record from 0004 onward carries a `Confirmation` subsection.

**ADR 0021 shipped without one and nothing noticed for three releases**, while
0022, 0023 and 0024 each grew theirs. That is the shape a convention enforced
only by prose always takes — settled decision #23 records the same lesson about
the `updated_at` trigger, and its conclusion applies here: *if the per-record
line is tiresome, the answer is the test, not a reminder.*

The section is where a record says what would catch it being wrong. A decision
with no such section is a decision nobody has to defend.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[2] / "docs" / "adr"

#: 0001 to 0003 predate the requirement. Named rather than derived from a number,
#: so adding the section to one of them does not silently change what is tested.
EXEMPT = {"0001", "0002", "0003"}


def records() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("0*.md") if re.fullmatch(r"\d{4}-.+", p.stem))


def test_there_are_records_to_check() -> None:
    """A glob that matches nothing passes every assertion below it. This is the
    check that makes the rest mean something — the same reason the gate is told
    to fail rather than pass when it scans zero files."""
    assert len(records()) > 20


@pytest.mark.parametrize("record", records(), ids=lambda p: p.stem[:4])
def test_a_record_says_what_would_catch_it_being_wrong(record: Path) -> None:
    if record.stem[:4] in EXEMPT:
        pytest.skip("predates the requirement")

    assert "### Confirmation" in record.read_text(encoding="utf-8"), (
        f"{record.name} has no Confirmation subsection — every record from 0004 "
        "onward states what checks its claims"
    )


@pytest.mark.parametrize("record", records(), ids=lambda p: p.stem[:4])
def test_a_record_declares_a_status(record: Path) -> None:
    """Nygard's format, per ADR 0001. A record with no status is one a reader
    cannot tell from a draft — and this project has superseded two decisions
    already, so the field does real work."""
    assert "## Status" in record.read_text(encoding="utf-8"), record.name


def test_every_record_is_in_the_index() -> None:
    """`README.md` is how anybody finds these. A record absent from it exists
    only for whoever remembers the number."""
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")

    missing = [p.name for p in records() if f"[{p.stem[:4]}]" not in index]

    assert missing == []
