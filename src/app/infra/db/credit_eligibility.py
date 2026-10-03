"""Who receives the monthly grant: one rule, read by the job and by ``/me``.

The grant job (`credit_grants.grant_monthly_credits`) pays on it, and the card's
``credits.monthly.unlocked`` reports it. Two copies would let the card say
"unlocked" to somebody the job skips, so both read this.

Each half is explained in ``credit_grants``'s module docstring: a mentee goal
(credits buy sessions), an unlock (what a qualifying invite opens), and alive.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select

from app.infra.db.models.mentoring import MenteeGoal
from app.infra.db.models.referrals import ReferralUnlock
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE

__all__ = ["receives_monthly_grant"]


def receives_monthly_grant() -> tuple[Any, ...]:
    """WHERE clauses over ``User`` that hold exactly for the grant's recipients."""
    return (
        LIVE,
        exists(select(MenteeGoal.id).where(MenteeGoal.user_id == User.id)),
        exists(select(ReferralUnlock.id).where(ReferralUnlock.user_id == User.id)),
    )
