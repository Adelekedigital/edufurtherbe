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

from app.core.errors import MentorApplicationDeclinedError, MentorApplicationInReviewError
from app.domain.mentor_application import (
    BOOKING_RULE_FIELDS,
    FROZEN_FIELDS,
    FrozenRecord,
    frozen_changes,
)
from app.infra.db.mentor_status_store import lock_mentor
from app.infra.db.models.mentoring import (
    MentorGuideCountry,
    MentorProfile,
    MentorServiceOffering,
    ServiceOffering,
)
from app.infra.db.models.user import UserProfile
from app.infra.db.offerings import live_offering
from app.infra.db.predicates import application_declined, application_in_review
from app.infra.db.profile_writer import held_languages, resolved_languages

__all__ = ["language_rows_unless_frozen", "refuse_frozen_changes", "refuse_while_in_review"]

#: Stored text compared in the form a write leaves it: trimmed, blank as null.
#: A legacy blank link is no link, so clearing it is not a change.
_TEXT = ("headline", "about_me", "primary_study_program", "social_linkedin")


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
    # **Live offerings only**, the set the profile read shows and a write may
    # send: a retired one held is invisible, so counting it would refuse every
    # resend of the list the mentor can see.
    offerings = (
        select(func.coalesce(func.array_agg(MentorServiceOffering.service_offering_id), []))
        .join(ServiceOffering, ServiceOffering.id == MentorServiceOffering.service_offering_id)
        .where(MentorServiceOffering.mentor_user_id == MentorProfile.user_id, live_offering())
        .correlate(MentorProfile)
        .scalar_subquery()
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
    return stored


async def _under_review(session: AsyncSession, user_id: UUID) -> bool:
    """Whether the application is in review: the lock, then one cheap `EXISTS`.

    For a write that does not compare values, so reads none of them.
    """
    if not await lock_mentor(session, user_id):
        return False
    found = await session.scalar(
        select(MentorProfile.id).where(MentorProfile.user_id == user_id, *application_in_review())
    )
    return found is not None


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
    Languages are `language_rows_unless_frozen`'s, since their replace needs
    the held rows too.
    """
    if FROZEN_FIELDS.keys().isdisjoint(sent):
        return
    stored = await _in_review(session, user_id)
    if stored is not None and (changed := frozen_changes(sent, stored)):
        raise _refusal(changed)


async def language_rows_unless_frozen(
    session: AsyncSession, user_id: UUID, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The rows a languages replace stores, refused if they change them in review.

    The held rows are read and locked once, after the mentor lock, and serve
    both the comparison and the resolution the writer stores.
    """
    in_review = await _under_review(session, user_id)
    held = await held_languages(session, user_id)
    rows = resolved_languages(entries, held)
    stored = {"languages": [{"language_id": key, **value} for key, value in held.items()]}
    if in_review and (changed := frozen_changes({"languages": rows}, stored)):
        raise _refusal(changed)
    return rows


async def refuse_while_in_review(
    session: AsyncSession, user_id: UUID, record: FrozenRecord
) -> None:
    """Refuse every write to `record`: each one changes what is being reviewed."""
    if await _under_review(session, user_id):
        raise _refusal((record.value,))


async def refuse_while_declined(
    session: AsyncSession, user_id: UUID, fields: tuple[str, ...] = ()
) -> None:
    """Refuse a write to a declined mentor's booking setup (owner, 2026-10-20).

    No lock: a decision racing the write only moves which side of it the write
    lands, and either side is harmless.
    """
    declined = await session.scalar(
        select(MentorProfile.id).where(MentorProfile.user_id == user_id, *application_declined())
    )
    if declined is not None:
        raise MentorApplicationDeclinedError(
            "Your application was declined, so your booking setup can't change.", fields=fields
        )


async def refuse_declined_booking_rules(
    session: AsyncSession, user_id: UUID, sent: Mapping[str, Any]
) -> None:
    """Refuse a mentor-profile write that sends a booking rule while declined.
    A write sending none is not checked; the rest of the profile stays editable."""
    if fields := tuple(sorted(BOOKING_RULE_FIELDS & sent.keys())):
        await refuse_while_declined(session, user_id, fields)
