"""Which halves of `/me` a caller gets: the mentee half or not.

**A mentee is anyone with a goal, or anyone who is not a mentor.** Booking
requires no goal, so an account that never finished onboarding can still book
and spend a credit; gating its credits and request counts on the goal hid both
from the person they describe. A mentor without a goal is a mentor only; a
mentor with one is both.

Display only. Grants (`credit_grants`) still require a goal, and nothing here
authorizes an action.
"""

from __future__ import annotations

__all__ = ["is_mentee"]


def is_mentee(*, has_goal: bool, has_mentor_profile: bool) -> bool:
    """True when the caller gets the mentee half: credits and `as_mentee`."""
    return has_goal or not has_mentor_profile
