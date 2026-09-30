"""How complete a mentor's profile is, and what they should do next (#223).

**One rule, in one place**, so the owner's Profile strength card, `setup_needed`
and any later nudge email or admin view can never disagree about what counts.
`COMPLETENESS_ORDER` is both the list of steps and their priority: the two that
stop anyone booking the mentor come first, so a card showing the first two
missing steps always leads with those. Every step weighs the same.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["BOOKABILITY", "COMPLETENESS_ORDER", "Completeness", "completeness"]

#: Every step, in the order a mentor should take them.
COMPLETENESS_ORDER: tuple[str, ...] = (
    "session_type",
    "weekly_hours",
    "photo",
    "headline",
    "about",
    "topics",
    "background",
    "education",
    "award",
)

#: The steps without which nobody can book the mentor — what `setup_needed` names.
BOOKABILITY = frozenset({"session_type", "weekly_hours"})


@dataclass(frozen=True, slots=True)
class Completeness:
    percent: int
    #: The steps not done, in `COMPLETENESS_ORDER`.
    missing: tuple[str, ...]

    @property
    def setup_needed(self) -> list[str]:
        """The bookability steps still missing, in order."""
        return [code for code in self.missing if code in BOOKABILITY]


def completeness(done: Mapping[str, bool]) -> Completeness:
    """The share of steps done, rounded to a whole percent, and what is left.

    `done` must name exactly the steps in `COMPLETENESS_ORDER`: a step silently
    absent would count as neither done nor missing. With nine equal steps the
    share is never exactly half a percent, so rounding has no tie to break.
    """
    if set(done) != set(COMPLETENESS_ORDER):
        raise ValueError(f"completeness needs exactly {COMPLETENESS_ORDER}, got {sorted(done)}")
    missing = tuple(code for code in COMPLETENESS_ORDER if not done[code])
    finished = len(COMPLETENESS_ORDER) - len(missing)
    return Completeness(percent=round(finished * 100 / len(COMPLETENESS_ORDER)), missing=missing)
