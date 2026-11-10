"""The admin Analytics Growth tab (PRD 31 PR 4).

* **signups**: accounts made in the window, `primary_role` splitting mentees
  from mentors; a migrated account is not a sign-up
  (`analytics_features.sign_ups`).
* **invites**: the invites made in the window by a counted inviter
  (`analytics_features.invites_made`): all of them, those whose invitee
  signed up (`joined`), finished their profile (`qualified`), has any session
  as a mentee (`first_booking`) or a completed one (`first_completed`). The
  top inviter is the live inviter with the most `joined` among them, ties by
  name.
* **months**: sign-ups in each of the last `GROWTH_MONTHS` UTC months, oldest
  first, the current one included, whatever the window.
* **migrated**: accounts carried over from the old app, all time; null with
  none. No cutover date is recorded, so `on` is null.
* **completed_total**, **by_topic**, **by_stage**: completed sessions starting
  in the window (`analytics_sessions.is_completed_in`); by each service
  offering the session's type covers, and by each application stage it is
  aimed at. A session counts under every topic or stage its type names; one
  whose type names no stage, or with no type, is `not_set`: PRD 31 defines the
  bucket as "Not set = the mentor didn't choose one". Decision 215's "an empty
  set means any stage" is about which mentees a type suits when booking; spread
  over all six here, it would inflate every stage and hide that none was chosen.

Everyone `analytics_scope` leaves out is left out.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics import (
    GROWTH_MONTHS,
    NOT_SET,
    GrowthTab,
    InviteCohort,
    Migrated,
    MonthCount,
    Period,
    SignUps,
    StageCount,
    TopicCount,
    TopInviter,
    compared,
)
from app.domain.enums import ApplicationStage, PrimaryRole
from app.domain.mentor_stats import Window, month_windows
from app.domain.names import joined_name
from app.infra.db import analytics_features as f
from app.infra.db.analytics_scope import counted_session, counted_user, per_window
from app.infra.db.analytics_sessions import completed_in, is_completed_in
from app.infra.db.models.mentoring import ServiceOffering, SessionTypeOffering
from app.infra.db.models.referrals import Referral
from app.infra.db.models.sessions import Session, SessionTypeStage
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE, during


def _joined() -> Any:
    """The invitee signed up."""
    return Referral.signed_up_at.is_not(None)


async def sign_ups(session: AsyncSession, period: Period, compare: bool) -> SignUps:
    windows = [period.window, period.previous().window]
    everyone = await per_window(session, windows, f.sign_ups)
    mentees = await per_window(session, windows, lambda w: f.sign_ups(w, role=PrimaryRole.MENTEE))
    mentors = await per_window(session, windows, lambda w: f.sign_ups(w, role=PrimaryRole.MENTOR))
    return SignUps(
        everyone=compared(int(everyone[0]), int(everyone[1]), compare),
        mentees=compared(int(mentees[0]), int(mentees[1]), compare),
        mentors=compared(int(mentors[0]), int(mentors[1]), compare),
    )


async def _top_inviter(session: AsyncSession, window: Window) -> TopInviter | None:
    """The live inviter with the most invitees who joined from invites made
    in ``window``, ties by name; ``None`` when nobody joined."""
    joined = func.count().label("joined")
    per_inviter = (
        select(User.id, User.first_name, User.last_name, joined)
        .select_from(Referral)
        .join(User, User.id == Referral.referrer_id)
        .where(
            during(Referral.created_at, window),
            counted_user(Referral.referrer_id),
            _joined(),
            LIVE,
        )
        .group_by(User.id)
        .subquery()
    )
    most = select(func.max(per_inviter.c.joined)).scalar_subquery()
    tied = (await session.execute(select(per_inviter).where(per_inviter.c.joined == most))).all()
    if not tied:
        return None
    top = min(tied, key=lambda row: (joined_name(row.first_name, row.last_name), str(row.id)))
    return TopInviter(name=joined_name(top.first_name, top.last_name), joined=int(top.joined))


async def invite_cohort(session: AsyncSession, window: Window) -> InviteCohort:
    invited, joined, qualified, booked, met = (
        await session.execute(
            select(
                f.invites_made(window),
                f.invites_made(window, extra=_joined()),
                f.invites_made(window, extra=f.qualified()),
                f.invites_made(window, extra=f.invitee_had(counted_session())),
                f.invitees_who_met(window),
            )
        )
    ).one()
    return InviteCohort(
        invited=int(invited),
        joined=int(joined),
        qualified=int(qualified),
        first_booking=int(booked),
        first_completed=int(met),
        top_inviter=await _top_inviter(session, window),
    )


async def months(session: AsyncSession, now: dt.datetime) -> list[MonthCount]:
    windows = month_windows(now, GROWTH_MONTHS)
    counts = await per_window(session, windows, f.sign_ups)
    return [MonthCount(month=w.month, count=int(n)) for w, n in zip(windows, counts, strict=True)]


async def migrated(session: AsyncSession) -> Migrated | None:
    """Accounts carried over from the old app; ``None`` with none."""
    count = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.legacy_bubble_id.is_not(None), counted_user(User.id))
    )
    return Migrated(count=int(count), on=None) if count else None


async def by_topic(session: AsyncSession, window: Window) -> list[TopicCount]:
    """Every service offering, in the platform's order, with the completed
    sessions whose type covers it."""
    rows = await session.execute(
        select(ServiceOffering.slug, ServiceOffering.display_name, func.count(Session.id))
        .select_from(ServiceOffering)
        .outerjoin(
            SessionTypeOffering, SessionTypeOffering.service_offering_id == ServiceOffering.id
        )
        .outerjoin(
            Session,
            and_(
                Session.session_type_id == SessionTypeOffering.session_type_id,
                is_completed_in(window),
            ),
        )
        .group_by(ServiceOffering.id)
        .order_by(ServiceOffering.sort_order, ServiceOffering.display_name)
    )
    return [TopicCount(key=slug, name=name, count=int(n)) for slug, name, n in rows.all()]


def _aimed_at(window: Window, stage: ApplicationStage | None) -> Any:
    """Completed sessions whose type is aimed at ``stage``; with ``None``,
    those whose type names no stage, or that have no type."""
    named = select(SessionTypeStage.id).where(
        SessionTypeStage.session_type_id == Session.session_type_id
    )
    if stage is not None:
        aimed: Any = named.where(SessionTypeStage.stage == stage).exists()
    else:
        aimed = ~named.exists()
    return (
        select(func.count())
        .select_from(Session)
        .where(is_completed_in(window), aimed)
        .scalar_subquery()
    )


async def by_stage(session: AsyncSession, window: Window) -> list[StageCount]:
    stages: list[ApplicationStage | None] = [*ApplicationStage, None]
    counts = (await session.execute(select(*(_aimed_at(window, s) for s in stages)))).one()
    return [
        StageCount(key=NOT_SET if s is None else s, count=int(n))
        for s, n in zip(stages, counts, strict=True)
    ]


async def completed_total(session: AsyncSession, window: Window) -> int:
    (total,) = await per_window(session, [window], completed_in)
    return int(total)


async def growth_tab(
    session: AsyncSession, period: Period, compare: bool, now: dt.datetime
) -> GrowthTab:
    window = period.window
    return GrowthTab(
        signups=await sign_ups(session, period, compare),
        invites=await invite_cohort(session, window),
        months=await months(session, now),
        migrated=await migrated(session),
        completed_total=await completed_total(session, window),
        by_topic=await by_topic(session, window),
        by_stage=await by_stage(session, window),
    )
