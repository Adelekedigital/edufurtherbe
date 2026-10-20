"""Refusing changes to a submitted mentor application's reviewed fields.

From submit to decision the reviewer judges the profile the applicant sent, so
the applicant may not change it (owner, 2026-10-18; decision 273, widened
2026-10-19 to the photo, cover, origin, languages, guide countries and awards).
The rule is `domain.mentor_application`: `frozen_changes` for a field-valued
write, `FrozenRecord` for a write that always changes what is reviewed. This
reads what is stored and refuses. Approve or decline unlocks.

**The mentor row is locked first, with `lock_mentor`**, the lock submit and
every decision take, and only then is the state read. So an edit and a submit
run one after the other whichever starts first, and an edit cannot land on an
application submitted while it was in flight. `FOR UPDATE` rather than a share
lock, because the write that follows updates the same row. The caller writes
in the same transaction, after this returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import MentorApplicationInReviewError
from app.domain.mentor_application import FROZEN_FIELDS, FrozenRecord, frozen_changes
from app.domain.social_links import SocialNetwork, canonical_social
from app.infra.db.mentor_status_store import lock_mentor
from app.infra.db.models.mentoring import (
    MentorGuideCountry,
    MentorProfile,
    MentorServiceOffering,
)
from app.infra.db.models.user import UserProfile
from app.infra.db.predicates import application_in_review
from app.infra.db.profile_writer import held_languages, resolved_languages

__all__ = ["refuse_frozen_changes", "refuse_while_in_review"]

#: Stored text compared in the form a write leaves it: trimmed, blank as null.
_TEXT = ("headline", "about_me", "primary_study_program")


def _cleaned(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _mentor_array(aggregated: Any, owner: Any) -> Any:
    """`aggregated` over the in-review mentor's rows: an array, empty and never null."""
    return (
        select(func.coalesce(aggregated, []))
        .where(owner == MentorProfile.user_id)
        .correlate(MentorProfile)
        .scalar_subquery()
    )


async def _in_review(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    """The stored reviewed fields while the application is in review, else `None`.

    Locks the mentor row first; a caller with no live mentor profile has nothing
    under review and takes no lock.
    """
    if not await lock_mentor(session, user_id):
        return None
    offerings = _mentor_array(
        func.array_agg(MentorServiceOffering.service_offering_id),
        MentorServiceOffering.mentor_user_id,
    )
    guide_countries = _mentor_array(
        func.array_agg(
            aggregate_order_by(MentorGuideCountry.country_id, MentorGuideCountry.position)
        ),
        MentorGuideCountry.mentor_user_id,
    )
    row = (
        (
            await session.execute(
                select(
                    MentorProfile.headline,
                    MentorProfile.primary_study_country_id,
                    MentorProfile.primary_study_program,
                    UserProfile.social_linkedin,
                    UserProfile.about_me,
                    UserProfile.about_me_drafted_at,
                    UserProfile.about_me_prompt_version,
                    UserProfile.avatar_focus_x,
                    UserProfile.avatar_focus_y,
                    UserProfile.cover_color,
                    UserProfile.cover_art,
                    UserProfile.origin_country_id,
                    offerings.label("offering_ids"),
                    guide_countries.label("guide_country_ids"),
                )
                .outerjoin(UserProfile, UserProfile.user_id == MentorProfile.user_id)
                .where(MentorProfile.user_id == user_id, *application_in_review())
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    stored = dict(row) | {field: _cleaned(row[field]) for field in _TEXT}
    stored["avatar_focus"] = (row["avatar_focus_x"], row["avatar_focus_y"])
    # A legacy link is compared in the form a write stores; one that cannot be
    # made canonical is kept as it is, so clearing it still counts as a change.
    link = row["social_linkedin"]
    stored["social_linkedin"] = canonical_social(SocialNetwork.LINKEDIN, link) or link
    return stored


def _refusal(fields: tuple[str, ...]) -> MentorApplicationInReviewError:
    return MentorApplicationInReviewError(
        "Your application is in review, so these can't change until it is decided.",
        fields=fields,
    )


async def refuse_frozen_changes(
    session: AsyncSession, user_id: UUID, sent: Mapping[str, Any]
) -> None:
    """Refuse a write that changes a reviewed field.

    A request sending none of them is not checked at all, and takes no lock.
    `languages` is a replace's entries as sent, compared as the rows the
    replace would store: an omitted field filled from what is held.
    """
    if FROZEN_FIELDS.keys().isdisjoint(sent):
        return
    stored = await _in_review(session, user_id)
    if stored is None:
        return
    if "languages" in sent:
        held = await held_languages(session, user_id)
        sent = {**sent, "languages": resolved_languages(sent["languages"], held)}
        stored["languages"] = [{"language_id": key, **value} for key, value in held.items()]
    if changed := frozen_changes(sent, stored):
        raise _refusal(changed)


async def refuse_while_in_review(
    session: AsyncSession, user_id: UUID, record: FrozenRecord
) -> None:
    """Refuse every write to `record`: each one changes what is being reviewed."""
    if await _in_review(session, user_id) is not None:
        raise _refusal((record.value,))
