"""The template registry in the docs names every message the code can send.

`docs/loops-template-variables.md` is the one reference for which Loops template
each email uses and the Railway value to paste (rule 8). A `Notification` member
added without a row is an email nobody knows how to configure, so this parses
the table and fails on the gap — in either direction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.domain.notifications import Notification

DOC = Path(__file__).resolve().parents[2] / "docs" / "loops-template-variables.md"


def registry() -> dict[str, str]:
    """Member to id (or NEEDED), from the registry table."""
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("## Template registry") :]
    section = section[: section.index("\n### ")]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        found = re.match(r"^\| `([a-z_]+)` \|[^|]*\| ([^|]+) \|", line)
        if found:
            rows[found.group(1)] = found.group(2).strip().strip("`*")
    return rows


def test_every_message_has_exactly_one_row() -> None:
    assert sorted(registry()) == sorted(member.value for member in Notification)


def test_the_paste_ready_value_is_every_mapped_id_and_nothing_else() -> None:
    text = DOC.read_text(encoding="utf-8")
    value = re.search(r"^EMAIL_TEMPLATES=(\{.*\})$", text, re.MULTILINE)
    assert value is not None
    pasted = json.loads(value.group(1))

    mapped = {member: id_ for member, id_ in registry().items() if id_ != "NEEDED"}
    assert pasted == mapped
