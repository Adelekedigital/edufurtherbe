"""What a user has done, as their credit level reads it (decision 307).

**Measured here once, judged once in ``domain.credits.level_allowance``.** The
1st's grant, the mid-month top-up and the ``/me`` card all ask the same
question; three hand-rolled ``EXISTS`` sets would be the duplication
non-negotiable #8 names, and the card could then say a level the grant does not
pay.

Each fact is a mentee's: credits buy sessions, and a user with no mentee goal
books none. Written as *has a goal* rather than *is not a mentor* (settled
decision 15), so a dual-role user is both.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.credits import LevelFacts
from app.infra.db.models.mentoring import MenteeGoal
from app.infra.db.models.referrals import Referral, ReferralUnlock
from app.infra.db.models.user import User, UserOnboarding
from app.infra.db.predicates import LIVE

__all__ = ["NO_FACTS", "level_candidates", "level_facts"]

#: A user the facts query did not return — deleted, or not a user at all. Level 0.
NO_FACTS = LevelFacts(onboarded=False, invited=False, unlocked=False)

_MENTEE = exists(select(MenteeGoal.id).where(MenteeGoal.user_id == User.id))

_ONBOARDED = and_(
    _MENTEE,
    exists(
        select(UserOnboarding.id).where(
            UserOnboarding.user_id == User.id, UserOnboarding.completed_at.is_not(None)
        )
    ),
)

#: An invite of theirs qualified: its invitee finished onboarding.
_INVITED = exists(
    select(Referral.id).where(Referral.referrer_id == User.id, Referral.qualified_at.is_not(None))
)

#: The unlock (decision 295), or a grandfathered one from the migration.
_UNLOCKED = and_(
    _MENTEE, exists(select(ReferralUnlock.id).where(ReferralUnlock.user_id == User.id))
)


def level_candidates() -> tuple[Any, ...]:
    """WHERE clauses over ``User`` for everybody whose level may be above 0:
    alive, and onboarded or unlocked. What the 1st's grant iterates."""
    return (LIVE, or_(_ONBOARDED, _UNLOCKED))


async def level_facts(session: AsyncSession, user_ids: Collection[UUID]) -> dict[UUID, LevelFacts]:
    """The facts for each live user named. A user missing from the result —
    deleted, or unknown — is at level 0 (:data:`NO_FACTS`)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(
            User.id,
            _ONBOARDED.label("onboarded"),
            _INVITED.label("invited"),
            _UNLOCKED.label("unlocked"),
        ).where(User.id.in_(list(user_ids)), LIVE)
    )
    return {
        row.id: LevelFacts(
            onboarded=bool(row.onboarded), invited=bool(row.invited), unlocked=bool(row.unlocked)
        )
        for row in rows
    }
