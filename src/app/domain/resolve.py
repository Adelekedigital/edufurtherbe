"""Turning legacy display names into reference ids.

Legacy stores countries and languages as **text a human typed or picked** —
``"Nigeria"``, ``"Abkhaz"`` — while the schema stores foreign keys. Something has
to bridge that, and the shape of the bridge decides whether a mismatch is
noticed.

**A name that does not resolve is reported, never substituted.** No nearest
match, no fallback to a default, no dropping the value silently. The alternative
is a migration that quietly files a Ghanaian mentee under Ghana's alphabetical
neighbour, and the only person who could catch it is the one who already
suspects it happened.

Two failure modes are already known, and they are different:

- ``Abkhaz`` is ISO's ``Abkhazian``. A **naming variant**, fixable with an alias.
- ``Avestan`` is a real ISO 639-3 language the M0 seed deliberately excluded by
  filtering to living languages. **No alias fixes that** — it needs a decision
  about the seed, which is exactly why an unresolved name has to reach a human.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeVar
from uuid import UUID

# Hand-checked, one entry per known mismatch, and deliberately small.
#
# This is not a place to accumulate guesses. Each line says "the legacy system
# calls it X, ISO calls it Y", and adding one is a decision somebody made after
# looking at both. A fuzzy matcher would fill this file automatically and remove
# the moment where anybody had to agree.
LANGUAGE_ALIASES: Mapping[str, str] = {
    "Abkhaz": "Abkhazian",
}

COUNTRY_ALIASES: Mapping[str, str] = {}

K = TypeVar("K")


@dataclass(frozen=True, slots=True)
class Resolution:
    """What resolved, and what did not.

    ``unresolved`` carries the *names*, not a count, because the next action is
    always to look at them — decide an alias, widen a seed, or correct the data
    at source. A count tells you there is a problem and nothing about which.
    """

    resolved: dict[str, UUID] = field(default_factory=dict)
    unresolved: tuple[str, ...] = ()

    def __getitem__(self, name: str) -> UUID:
        return self.resolved[name]

    def __contains__(self, name: str) -> bool:
        return name in self.resolved


def resolve_names(
    names: set[str], reference: Mapping[str, UUID], aliases: Mapping[str, str]
) -> Resolution:
    """Match display names against a reference table, three ways, in order.

    Exact, then case-insensitive, then the alias table. Case-insensitivity is
    second rather than first so an exact match always wins: if a reference table
    ever holds two names differing only in case, an exact hit is unambiguous and
    a folded one is a coin toss.

    ``reference`` maps a display name to its id — built by the caller from
    ``countries`` or ``languages``, since ``domain`` cannot query anything.
    """
    folded = {name.casefold(): identifier for name, identifier in reference.items()}

    resolved: dict[str, UUID] = {}
    unresolved: list[str] = []

    for name in sorted(names):
        cleaned = name.strip()
        if not cleaned:
            continue

        if cleaned in reference:
            resolved[name] = reference[cleaned]
        elif cleaned.casefold() in folded:
            resolved[name] = folded[cleaned.casefold()]
        elif (alias := aliases.get(cleaned)) and alias in reference:
            resolved[name] = reference[alias]
        else:
            unresolved.append(name)

    return Resolution(resolved=resolved, unresolved=tuple(unresolved))
