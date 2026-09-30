"""How complete a mentor's profile is, and what they should do next (#223).

**One rule, in one place**, so the owner's Profile strength card, `setup_needed`
and any later nudge email or admin view can never disagree about what counts.
A caller passes the plain facts of a profile (`ProfileFacts`); what makes each
step done lives here and nowhere else. `COMPLETENESS_ORDER` is both the list of
steps and their priority: the two that stop anyone booking the mentor come
first, so a card showing the first two missing steps always leads with those.
Every step weighs the same.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BOOKABILITY", "COMPLETENESS_ORDER", "Completeness", "ProfileFacts", "completeness"]

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
class ProfileFacts:
    """What a mentor's profile holds, as counts and raw values — no judgements.

    Counts are of live rows only; the caller reads them from the same lists the
    profile renders.
    """

    has_session_type: bool
    has_weekly_hours: bool
    photo_url: str | None
    headline: str | None
    about: str | None
    topic_count: int
    has_origin_country: bool
    has_study_country: bool
    language_count: int
    education_count: int
    award_count: int


@dataclass(frozen=True, slots=True)
class Completeness:
    percent: int
    #: The steps not done, in `COMPLETENESS_ORDER`.
    missing: tuple[str, ...]

    @property
    def setup_needed(self) -> list[str]:
        """The bookability steps still missing, in order."""
        return [code for code in self.missing if code in BOOKABILITY]


def completeness(facts: ProfileFacts) -> Completeness:
    """The share of steps done, rounded to a whole percent, and what is left.

    With nine equal steps the share is never exactly half a percent, so rounding
    has no tie to break.
    """
    done = _steps(facts)
    missing = tuple(code for code in COMPLETENESS_ORDER if not done[code])
    finished = len(COMPLETENESS_ORDER) - len(missing)
    return Completeness(percent=round(finished * 100 / len(COMPLETENESS_ORDER)), missing=missing)


def _steps(facts: ProfileFacts) -> dict[str, bool]:
    """Whether each step is done — the one statement of what "done" means.

    `background` is where the mentor comes from, where they studied, and a
    language they mentor in: all three, since the card asks for them together.
    """
    return {
        "session_type": facts.has_session_type,
        "weekly_hours": facts.has_weekly_hours,
        "photo": _filled(facts.photo_url),
        "headline": _filled(facts.headline),
        "about": _filled(facts.about),
        "topics": facts.topic_count > 0,
        "background": (
            facts.has_origin_country and facts.has_study_country and facts.language_count > 0
        ),
        "education": facts.education_count > 0,
        "award": facts.award_count > 0,
    }


def _filled(value: str | None) -> bool:
    """Set and not blank. Writes store a blank as null, but migrated text may not."""
    return value is not None and bool(value.strip())
