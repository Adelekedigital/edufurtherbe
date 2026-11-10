"""The admin Analytics Overview and Sessions tabs, assembled (PRD 31,
decision 324). The figures are `analytics_sessions` and `analytics_features`;
this module reads them for the right windows and turns counts into rates."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics import (
    FEATURES,
    Feature,
    FeatureKey,
    Metric,
    NorthStar,
    OverviewTab,
    Period,
    SeriesPoint,
    SessionsTab,
    SubTracks,
    TopReason,
    compared,
    rate,
    ratio,
    rounded,
    series_buckets,
    series_unit,
    spark_buckets,
)
from app.domain.availability import BookingWindow
from app.domain.enums import MissedReason, SessionStatus
from app.domain.mentor_stats import Window
from app.infra.db import analytics_features as f
from app.infra.db import analytics_sessions as s
from app.infra.db.analytics_credits import credits_tab
from app.infra.db.analytics_scope import Measure, per_window, per_window_each
from app.infra.db.models.sessions import Session

#: The repeat-booking look-back, ending at the window's end.
REPEAT_DAYS = 60


def _shift(window: Window, days: int) -> Window:
    step = dt.timedelta(days=days)
    return Window(since=window.since - step, until=window.until - step)


async def _rates(
    session: AsyncSession, windows: list[Window], num: Measure, den: Measure
) -> list[float | None]:
    tops, bottoms = await per_window_each(session, windows, num, den)
    return [rate(int(t or 0), int(b or 0)) for t, b in zip(tops, bottoms, strict=True)]


async def _feature(
    session: AsyncSession,
    key: FeatureKey,
    measures: tuple[Measure, Measure],
    period: Period,
    compare: bool,
    contribution: Any = None,
) -> Feature:
    current, previous = period.window, period.previous().window
    values = await _rates(session, [current, previous, *spark_buckets(current)], *measures)
    return Feature(
        key=key,
        value=compared(values[0], values[1], compare),
        spark=values[2:],
        contribution=contribution,
    )


async def overview_tab(
    session: AsyncSession, period: Period, compare: bool, booking_window: BookingWindow
) -> OverviewTab:
    current, previous = period.window, period.previous().window
    buckets = series_buckets(period)
    done = await per_window(
        session,
        [current, previous, *buckets, *(_shift(b, period.days) for b in buckets)],
        s.completed_in,
    )
    mentees, mentors = await per_window_each(
        session,
        [current, previous],
        lambda w: s.completed_in(w, distinct=Session.mentee_id),
        lambda w: s.completed_in(w, distinct=Session.mentor_id),
    )
    count = len(buckets)
    series = [
        SeriesPoint(
            start=bucket.since.date(),
            value=int(done[2 + i]),
            previous=int(done[2 + count + i]) if compare else None,
        )
        for i, bucket in enumerate(buckets)
    ]
    north_star = NorthStar(
        completed=compared(int(done[0]), int(done[1]), compare),
        unit=series_unit(period),
        series=series,
    )
    sub_tracks = SubTracks(
        unique_mentees=compared(int(mentees[0]), int(mentees[1]), compare),
        mentors_delivered=compared(int(mentors[0]), int(mentors[1]), compare),
        sessions_per_mentee=compared(
            ratio(int(done[0]), int(mentees[0])), ratio(int(done[1]), int(mentees[1])), compare
        ),
    )

    supplied, listed = (await session.execute(select(*f.mentor_supply(booking_window)))).one()
    messaged = (await _rates(session, [current], *f.messaged_first()))[0]
    invited_met, mentors_held = (
        await session.execute(select(f.invitees_who_met(current), f.mentors_who_held(current)))
    ).one()
    features = {
        "booking": await _feature(session, "booking", f.booking(), period, compare),
        "explore": Feature(key="explore", value=None, spark=None, contribution=None),
        "messaging": await _feature(
            session, "messaging", f.messaging(), period, compare, contribution=messaged
        ),
        "mentor_supply": Feature(
            key="mentor_supply",
            value=Metric(value=rate(int(supplied), int(listed)), previous=None),
            spark=None,
            contribution=None,
        ),
        "invites": await _feature(
            session, "invites", f.invites(), period, compare, contribution=int(invited_met)
        ),
        "mentee_activation": await _feature(
            session, "mentee_activation", f.mentee_activation(), period, compare
        ),
        "mentor_activation": await _feature(
            session,
            "mentor_activation",
            f.mentor_activation(),
            period,
            compare,
            contribution=int(mentors_held),
        ),
    }
    return OverviewTab(
        north_star=north_star,
        sub_tracks=sub_tracks,
        features=[features[key] for key in FEATURES],
    )


async def sessions_tab(session: AsyncSession, period: Period, compare: bool) -> SessionsTab:
    windows = [period.window, period.previous().window]
    repeat = [
        Window(since=w.until - dt.timedelta(days=REPEAT_DAYS), until=w.until) for w in windows
    ]

    (
        asked,
        accepted,
        decided,
        expired,
        reply,
        held,
        missed,
        stars,
        written,
        reported,
    ) = await per_window_each(
        session,
        windows,
        s.requests,
        lambda w: s.decided(w, (SessionStatus.CONFIRMED,)),
        s.decided,
        lambda w: s.decided(w, (SessionStatus.EXPIRED,)),
        s.median_reply_hours,
        s.completed_in,
        s.no_shows_in,
        s.average_rating,
        s.reviews_written,
        s.reports,
    )
    twice, once = await per_window_each(
        session,
        repeat,
        lambda w: s.repeat_mentees(w, at_least=2),
        lambda w: s.repeat_mentees(w, at_least=1),
    )
    ledgers = [await credits_tab(session, w) for w in windows]

    def both(values: list[Any]) -> Metric:
        return compared(values[0], values[1], compare)

    def rates(top: list[Any], bottom: list[Any]) -> Metric:
        return both([rate(int(t or 0), int(b or 0)) for t, b in zip(top, bottom, strict=True)])

    settled = [int(h) + int(m) for h, m in zip(held, missed, strict=True)]
    kpis = {
        "requests": both([int(x) for x in asked]),
        "acceptance_rate": rates(accepted, decided),
        "median_reply_hours": both([rounded(x, 1) for x in reply]),
        "show_up_rate": rates(held, settled),
        "repeat_booking_rate": rates(twice, once),
        "average_rating": both([rounded(x, 1) for x in stars]),
        "review_rate": rates(written, held),
        "reports": both([int(x) for x in reported]),
    }
    guardrails = {
        "no_show_rate": rates(missed, settled),
        "request_expiry_rate": rates(expired, decided),
        "credit_expiry_rate": both([rate(c.flow.expired, c.flow.granted) for c in ledgers]),
    }
    outcomes = {
        str(k): int(v) for k, v in (await session.execute(s.outcome_counts(windows[0]))).all()
    }
    reasons = (await session.execute(s.missed_reasons(windows[0]))).all()
    total = sum(int(n) for _, n in reasons)
    top = None
    if reasons:
        reason, n = reasons[0]
        top = TopReason(reason=MissedReason(reason), share=rate(int(n), total) or 0.0)
    return SessionsTab(
        kpis=kpis,
        outcomes={
            "requests": int(asked[0]),
            **{str(status): outcomes.get(str(status), 0) for status in s.OUTCOMES},
        },
        top_no_show_reason=top,
        guardrails=guardrails,
    )
