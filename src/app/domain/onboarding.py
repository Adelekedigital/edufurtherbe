"""When a profile counts as finished.

Pure, and in ``domain`` because it is the anti-farming boundary rather than a
query detail. What this predicate guards is a credit, and a credit is
spendable — so it deserves the scrutiny of an authorization rule, not a
condition buried in a ``WHERE`` clause nobody reads twice.

WHY COMPLETION AND NOT SIGNUP
=============================
The starter credit is granted when somebody finishes a profile, never when they
create an account. **Signing up is free; finishing a profile is work.** That
asymmetry is the whole anti-farming property, and it is the one the package's D9
was reaching for without having: D9 gated a flat ladder behind a referral, which
costs an attacker one throwaway address.

THE BAR IS ROW EXISTENCE, AND THAT IS DELIBERATELY TEMPORARY
============================================================
``user_profiles`` and ``mentee_goals`` have **no required columns** — everything
but ``user_id`` is nullable — so "complete" cannot mean "filled in" today
without this module inventing a field policy the rest of the codebase does not
have. It means: a profile row, and a role-appropriate profile beside it.

This is the same shape settled decision 20 takes for a qualifying invite, and it
is recorded the same way: weak on purpose, and **tightening it is a predicate
change here rather than a migration**. When the onboarding flow gains a defined
set of required steps, this is the one function that learns about them.

The accepted risk is named rather than waved at: a determined attacker can post
two nearly-empty rows and collect one non-expiring credit. One credit, once, per
account that also has to pass email verification — against a referral unlock,
which opens a *recurring* grant and is why that gate is the stricter of the two.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ProfileEvidence", "may_complete_onboarding"]


@dataclass(frozen=True, slots=True)
class ProfileEvidence:
    """What the database can say about how far somebody got.

    Booleans rather than rows, so the rule below is testable without a database
    and cannot accidentally start reading a column it was not given.
    """

    has_profile: bool
    has_mentee_goal: bool
    has_mentor_profile: bool


def may_complete_onboarding(evidence: ProfileEvidence) -> bool:
    """Whether this user has done enough to finish onboarding.

    **Both halves, not either.** A profile row *and* a role-appropriate profile:
    the profile is what a mentor reads before accepting, and the goal or mentor
    profile is what makes the account usable at all. A predicate written with
    ``or`` where it meant ``and`` still passes every positive case, which is why
    the incomplete combinations are enumerated in the tests rather than sampled.

    **Role-appropriate rather than mentee-only.** A mentor finishes onboarding
    too, and gating on a mentee goal alone would hand the starter only to the
    half of the platform that spends it — reasonable-sounding until a dual-role
    user is refused for having done the mentor half first.
    """
    return evidence.has_profile and (evidence.has_mentee_goal or evidence.has_mentor_profile)
