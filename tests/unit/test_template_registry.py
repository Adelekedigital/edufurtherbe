"""The template registry in the docs names every message the code can send.

`docs/loops-template-variables.md` is the one reference for which Loops template
each email uses, what it asks for, and the Railway value to paste (rule 8). A
`Notification` member added without a row is an email nobody knows how to
configure, so this parses the table and fails on the gap — in either direction.
The variables column is also what the message tests build against, so the table
is the only copy of what each live template declares.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.domain.notifications import Notification

DOC = Path(__file__).resolve().parents[2] / "docs" / "loops-template-variables.md"

_ROW = re.compile(r"^\| `([a-z_]+)` \|[^|]*\| ([^|]+) \|[^|]*\| ([^|]+) \|")
_SAME_AS = re.compile(r"^as `([a-z_]+)`$")


@dataclass(frozen=True)
class Row:
    member: str
    template_id: str
    variables: tuple[str, ...]


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("## Template registry") :]
    return section[: section.index("\n### ")]


def rows() -> list[Row]:
    """Every registry row, in order, duplicates kept so they can be caught."""
    raw: list[tuple[str, str, str]] = []
    for line in _section().splitlines():
        found = _ROW.match(line)
        if found:
            raw.append((found.group(1), found.group(2).strip().strip("`*"), found.group(3).strip()))
    declared = {member: cell for member, _, cell in raw}

    def names(cell: str) -> tuple[str, ...]:
        same = _SAME_AS.match(cell)
        if same:
            return names(declared[same.group(1)])
        if cell == "—":
            return ()
        return tuple(name.strip().strip("`") for name in cell.split(","))

    return [Row(member, template_id, names(cell)) for member, template_id, cell in raw]


def live_variables() -> dict[str, tuple[str, ...]]:
    """What each mapped template declares, as the registry records it."""
    return {row.member: row.variables for row in rows() if row.template_id != "NEEDED"}


def test_no_message_has_two_rows() -> None:
    counts = Counter(row.member for row in rows())
    assert [member for member, n in counts.items() if n > 1] == []


def test_every_message_has_exactly_one_row() -> None:
    assert sorted(row.member for row in rows()) == sorted(member.value for member in Notification)


def test_every_mapped_template_lists_its_variables() -> None:
    assert [member for member, names in live_variables().items() if not names] == []


def test_the_paste_ready_value_is_every_mapped_id_and_nothing_else() -> None:
    text = DOC.read_text(encoding="utf-8")
    value = re.search(r"^EMAIL_TEMPLATES=(\{.*\})$", text, re.MULTILINE)
    assert value is not None
    pasted = json.loads(value.group(1))

    mapped = {row.member: row.template_id for row in rows() if row.template_id != "NEEDED"}
    assert pasted == mapped
