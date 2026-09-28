"""What kind of help a mentor offers — the taxonomy, not their bookable products.

**A service offering is not a session type.** This is the closed six-row
vocabulary from settled decision #53 — "Document Review", "Interview Prep" — and
it is the axis matching joins on: `mentee_goal_needs` and
`mentor_service_offerings` both point at it, and collapsing the legacy
mixed-depth list to those six parents is what makes a mentee's need and a
mentor's offer the same row. A *session type* is one mentor's own bookable
product with a duration and a price. Both are in the domain vocabulary because
the two words are close enough to be swapped by accident.

Extracted here because two profiles read it: the mentor's own, and the public
one. The query was previously inline in `profile_store` and copying it into the
public store would have been one rule in two places — the defect this repository
has now paid for four times, twice in a visibility predicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.infra.db.models.mentoring import (
    MenteeGoalNeed,
    MentorServiceOffering,
    ServiceOffering,
    SessionTypeOffering,
)
from app.infra.db.models.sessions import SessionType

__all__ = [
    "goal_overlap_count",
    "has_live_goals",
    "live_offering_slugs",
    "offerings_for",
    "offerings_for_session_types",
    "offers_any",
    "set_session_type_offerings",
    "shared_offering_count",
]


def _live() -> Any:
    """Whether an offering is still one the platform offers.

    One clause, read by the card, the profile and the filter's validation. A
    retired offering hidden from the card but still accepted by the filter would
    list a mentor for a reason their card cannot show.

    `/catalog/service-offerings` decides the same thing through its own generic
    `is_active` spec. The two are pinned by
    `test_every_catalogue_offering_is_one_the_filter_accepts`.
    """
    return ServiceOffering.is_active.is_(True)


def _gives(mentor_user_id: Any, slugs: Sequence[str]) -> list[Any]:
    """This mentor gives one of `slugs` — the clauses `offers_any` and
    `shared_offering_count` both read, so *whether* and *how many* cannot come
    to mean different things."""
    return [
        MentorServiceOffering.mentor_user_id == mentor_user_id,
        ServiceOffering.id == MentorServiceOffering.service_offering_id,
        ServiceOffering.slug.in_(slugs),
    ]


def offers_any(mentor_user_id: Any, slugs: Sequence[str]) -> Any:
    """`EXISTS`: this mentor gives at least one of the offerings `slugs`.

    An `EXISTS` rather than a join, because `mentor_service_offerings` is
    one-to-many and a join lists a mentor once for every slug they match — the
    same reason the search module gives for reading education this way.

    **No `_live()` here, deliberately.** `slugs` arrive already checked by
    `live_offering_slugs`, and a second copy of that check would be a guard no
    test can reach — the retired case is refused before this runs.
    """
    return exists().where(*_gives(mentor_user_id, slugs))


def shared_offering_count(mentor_user_id: Any, slugs: Sequence[str]) -> Any:
    """How many of the offerings `slugs` this mentor gives — a scalar subquery.

    `offers_any` asks *whether*; this asks *how many*, for ranking similar
    mentors. The same clauses (`_gives`), and the same contract on `slugs`: they
    arrive live, so no second `_live()` here.
    """
    return (
        select(func.count())
        .select_from(MentorServiceOffering, ServiceOffering)
        .where(*_gives(mentor_user_id, slugs))
        .scalar_subquery()
    )


def _live_goals(mentee: UUID) -> list[Any]:
    """This mentee's goal needs that name an offering the platform still offers.

    **`_live()` applies here, unlike the filter**, because goal needs are stored
    rows: a mentee can hold a goal for an offering retired after they chose it.
    Counting it would rank a mentor up for a reason their card — which drops
    retired offerings — can no longer show. Shared by the overlap and
    `has_live_goals`, so a mentee whose only goals are retired browses newest
    first rather than getting a shuffle where every mentor scores zero.
    """
    return [
        MenteeGoalNeed.user_id == mentee,
        ServiceOffering.id == MenteeGoalNeed.service_offering_id,
        _live(),
    ]


def goal_overlap_count(mentor_user_id: Any, mentee: UUID) -> Any:
    """How many of this mentee's live goal needs the mentor gives — a scalar subquery.

    The join `MenteeGoalNeed`'s own docstring describes: both sides were
    collapsed to the same six parent offerings, so an overlap is an equality on
    `service_offering_id` and needs no fuzzy matching. Beside
    `shared_offering_count` because it is the same question asked of a mentee's
    goals instead of another mentor's offerings.
    """
    return (
        select(func.count())
        .select_from(MenteeGoalNeed, ServiceOffering)
        .join(
            MentorServiceOffering,
            MentorServiceOffering.service_offering_id == MenteeGoalNeed.service_offering_id,
        )
        .where(*_live_goals(mentee), MentorServiceOffering.mentor_user_id == mentor_user_id)
        .scalar_subquery()
    )


async def has_live_goals(session: AsyncSession, mentee: UUID) -> bool:
    """Whether this mentee has any live goal — what turns browse into the goal
    ranking. An empty goal row, or one naming only retired offerings, is no goal."""
    statement = select(exists().where(*_live_goals(mentee)))
    return bool((await session.execute(statement)).scalar_one())


async def live_offering_slugs(session: AsyncSession, slugs: Sequence[str]) -> set[str]:
    """Which of `slugs` name an offering the platform still offers."""
    if not slugs:
        return set()
    result = await session.execute(
        select(ServiceOffering.slug).where(ServiceOffering.slug.in_(slugs), _live())
    )
    return set(result.scalars())


async def offerings_for(
    session: AsyncSession, user_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Offerings for several mentors at once, keyed by mentor.

    **Plural because the list endpoint made it N+1.** A profile reads one
    mentor's offerings; discovery reads twenty. Written per-mentor it was twenty
    round trips per page, and the obvious fix — a second batched function beside
    the single one — is two queries of one rule, which is how the `is_active`
    filter ends up on one of them. So there is one query, and the profile passes
    a list of one.

    Mentors with no offerings are **absent from the mapping** rather than present
    with an empty list. The caller supplies the default, which keeps this function
    from deciding what a missing row means.

    **`is_active` is filtered, which the inline version this replaced did not
    do.** No row is inactive today — all six ship active from the M2 lookups
    migration and #53 closes the list to users — so it changes nothing
    observable now. It matters the day the platform retires one: a retired
    offering should stop appearing on a mentor's public profile *and* on their
    own, and one query means it cannot be filtered in half the places.

    `sort_order` rather than name: the platform decides how these read, and
    alphabetical would put "Document Review" above "Test Preparation" for no
    reason anybody chose.
    """
    if not user_ids:
        return {}

    result = await session.execute(
        select(
            MentorServiceOffering.mentor_user_id,
            ServiceOffering.slug,
            ServiceOffering.display_name,
        )
        .select_from(MentorServiceOffering)
        .join(ServiceOffering, ServiceOffering.id == MentorServiceOffering.service_offering_id)
        .where(
            MentorServiceOffering.mentor_user_id.in_(user_ids),
            _live(),
        )
        .order_by(ServiceOffering.sort_order)
    )

    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in result.mappings():
        grouped.setdefault(row["mentor_user_id"], []).append(
            {"slug": row["slug"], "display_name": row["display_name"]}
        )
    return grouped


async def offerings_for_session_types(
    session: AsyncSession, session_type_ids: Sequence[Any]
) -> dict[Any, list[dict[str, str]]]:
    """Each session type's offerings, in the mentor's order, as `code`/`display_name`.

    **One statement for a whole list**, keyed by session type. A type with rows
    in `session_type_offerings` reads them; **a type with none falls back to the
    legacy `service_offering_id`** (#200) — which is what code from before the
    table, the demo seed and the ETL still write, so none of them reads as
    unclassified. Retired offerings are still named: retiring one does not change
    what an existing session type was about.
    """
    if not session_type_ids:
        return {}
    joined = (
        select(
            SessionTypeOffering.session_type_id.label("type_id"),
            ServiceOffering.slug,
            ServiceOffering.display_name,
            SessionTypeOffering.position,
        )
        .join(ServiceOffering, ServiceOffering.id == SessionTypeOffering.service_offering_id)
        .where(SessionTypeOffering.session_type_id.in_(session_type_ids))
    )
    legacy = (
        select(
            SessionType.id.label("type_id"),
            ServiceOffering.slug,
            ServiceOffering.display_name,
            literal(0).label("position"),
        )
        .join(ServiceOffering, ServiceOffering.id == SessionType.service_offering_id)
        .where(
            SessionType.id.in_(session_type_ids),
            ~exists().where(SessionTypeOffering.session_type_id == SessionType.id),
        )
    )
    rows = await session.execute(joined.union_all(legacy).order_by("type_id", "position"))
    result: dict[Any, list[dict[str, str]]] = {}
    for row in rows:
        result.setdefault(row.type_id, []).append(
            {"code": str(row.slug), "display_name": str(row.display_name)}
        )
    return result


async def set_session_type_offerings(
    session: AsyncSession, session_type_id: Any, offering_ids: Sequence[UUID]
) -> None:
    """Replace one session type's set, in order, and dual-write the legacy column.

    **Every id must name an offering the platform still offers** — a `422`
    naming the unknown ones otherwise, rather than the foreign-key 500 an unknown
    id used to produce. The first of the set is written to
    `session_types.service_offering_id` (#200: the expand step), so code reading
    only that column this release still sees the type's main offering.
    """
    wanted = list(offering_ids)
    if wanted:
        live = set(
            (
                await session.execute(
                    select(ServiceOffering.id).where(ServiceOffering.id.in_(wanted), _live())
                )
            ).scalars()
        )
        unknown = [str(value) for value in wanted if value not in live]
        if unknown:
            raise ValidationError(
                f"service_offering_ids: not an offering the platform offers: {', '.join(unknown)}"
            )
    await session.execute(
        delete(SessionTypeOffering).where(SessionTypeOffering.session_type_id == session_type_id)
    )
    if wanted:
        await session.execute(
            insert(SessionTypeOffering),
            [
                {
                    "session_type_id": session_type_id,
                    "service_offering_id": value,
                    "position": index,
                }
                for index, value in enumerate(wanted)
            ],
        )
    await session.execute(
        update(SessionType)
        .where(SessionType.id == session_type_id)
        .values(service_offering_id=wanted[0] if wanted else None)
    )
