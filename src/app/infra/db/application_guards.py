"""What a mentor's application state forbids them to write.

A declined mentor's booking setup is read-only (decision 279), and a mentor
approved or in review keeps at least one help area (decisions 292, 296). Nothing
else is locked in review: the reviewer sees the profile as it is now.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import MentorApplicationDeclinedError, ValidationError
from app.domain.mentor_application import BOOKING_RULE_FIELDS
from app.infra.db.mentor_status_store import lock_mentor
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.offerings import claims_a_live_help_area
from app.infra.db.predicates import (
    application_approved,
    application_declined,
    application_in_review,
)

__all__ = [
    "refuse_clearing_help_areas",
    "refuse_declined_booking_rules",
    "refuse_while_declined",
]


async def _profile_where(
    session: AsyncSession, user_id: UUID, predicate: list[Any], *columns: Any
) -> Any:
    """The mentor's profile row if it meets `predicate` (its id, or `columns`),
    else `None`: the one lookup behind the declined lock and the help-area guard."""
    return (
        await session.execute(
            select(*(columns or (MentorProfile.id,))).where(
                MentorProfile.user_id == user_id, *predicate
            )
        )
    ).first()


async def refuse_while_declined(session: AsyncSession, user_id: UUID) -> None:
    """Refuse a write to a declined mentor's booking setup (owner, 2026-10-20).

    No lock: a decision racing the write only moves which side of it the write
    lands, and either side is harmless.
    """
    if await _profile_where(session, user_id, application_declined()) is not None:
        raise _declined(())


async def refuse_declined_booking_rules(
    session: AsyncSession, user_id: UUID, sent: Mapping[str, Any]
) -> None:
    """Refuse a mentor-profile write that **changes** a booking rule while declined.

    Compared with the stored values: a form resending the rules unchanged
    beside a headline edit saves. A write sending no rule is not checked.
    """
    names = sorted(BOOKING_RULE_FIELDS & sent.keys())
    if not names:
        return
    stored = await _profile_where(
        session,
        user_id,
        application_declined(),
        *(getattr(MentorProfile, name) for name in names),
    )
    if stored is None:
        return
    if changed := tuple(n for n, held in zip(names, stored, strict=True) if sent[n] != held):
        raise _declined(changed)


async def refuse_clearing_help_areas(
    session: AsyncSession, user_id: UUID, sent: Mapping[str, Any]
) -> None:
    """Refuse `offering_ids: []` that would leave an approved mentor, or an
    application in review, with no help area (decisions 292, 296). Approval
    does not re-check the application, so one in review keeps what submit
    asked for. A draft may clear the list. One already holding none (an
    offering retired) resends `[]` unchanged."""
    if sent.get("offering_ids") != []:
        return
    if not await lock_mentor(session, user_id):
        return
    holding = [
        or_(and_(*application_approved()), and_(*application_in_review())),
        claims_a_live_help_area(MentorProfile.user_id),
    ]
    if await _profile_where(session, user_id, holding) is not None:
        raise ValidationError(
            "Keep at least one help area.",
            field_errors=(("/offering_ids", "keep at least one help area once you have applied"),),
        )


def _declined(fields: tuple[str, ...]) -> MentorApplicationDeclinedError:
    return MentorApplicationDeclinedError(
        "Your application was declined, so your booking setup can't change.", fields=fields
    )
