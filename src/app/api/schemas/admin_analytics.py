"""The admin Analytics page's responses (PRD 31, decision 324).

Shapes agreed with the frontend (edufurtherfe-76, 2026-11-09). Every section
carries `window` and `alerts`; a rate is a percentage 0-100 with one decimal,
a count an integer, and a figure that compares is a `Metric`.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.analytics import (
    CREDIT_GROUPS,
    Alert,
    AlertKey,
    AnalyticsTab,
    CreditFigures,
    CreditGroup,
    CreditsTab,
    FeatureKey,
    Metric,
    OverviewTab,
    Period,
    SeriesUnit,
    SessionsTab,
)
from app.domain.enums import MissedReason


class WindowRead(BaseModel):
    """The window read, and the previous period it compares with: UTC dates,
    both ends inclusive."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    from_: date = Field(alias="from")
    to: date
    previous_from: date
    previous_to: date

    @classmethod
    def of(cls, period: Period) -> WindowRead:
        before = period.previous()
        return cls.model_validate(
            {
                "from": period.first,
                "to": period.last,
                "previous_from": before.first,
                "previous_to": before.last,
            }
        )


class AlertLinkRead(BaseModel):
    """Where the banner's link goes: a tab, and on Behaviour a funnel and its
    breakdown."""

    tab: AnalyticsTab
    funnel: str | None = None
    by: str | None = None


class AlertRead(BaseModel):
    """One PRD 30 alert that is firing. `title` and `body` are plain text."""

    key: AlertKey
    title: str
    body: str
    link: AlertLinkRead | None
    raised_at: datetime

    @classmethod
    def of(cls, alert: Alert) -> AlertRead:
        link = alert.link
        return cls(
            key=alert.key,
            title=alert.title,
            body=alert.body,
            link=None
            if link is None
            else AlertLinkRead(tab=link.tab, funnel=link.funnel, by=link.by),
            raised_at=alert.raised_at,
        )


class SectionRead(BaseModel):
    """What every section response carries."""

    window: WindowRead
    alerts: list[AlertRead] = Field(description="Alerts firing now, newest first.")


class CreditFiguresRead(BaseModel):
    granted: int
    spent: int
    refunded: int
    expired: int

    @classmethod
    def of(cls, figures: CreditFigures) -> CreditFiguresRead:
        return cls(
            granted=figures.granted,
            spent=figures.spent,
            refunded=figures.refunded,
            expired=figures.expired,
        )


class CreditSourceRead(CreditFiguresRead):
    source: CreditGroup


class AnalyticsCreditsRead(SectionRead):
    """The Credits tab."""

    flow: CreditFiguresRead = Field(
        description=(
            "Credits in the window: `granted` (refunds left out), `spent` on bookings, "
            "`refunded`, and `expired` unused."
        )
    )
    by_source: list[CreditSourceRead] = Field(
        description=(
            "One row per source, always all five in this order: `monthly` (the 1st's "
            "grant), `level_up` (a mid-month rise: finishing onboarding or a qualifying "
            "invite), `admin`, `refund` (its `granted` is credits given back) and "
            "`carried_over` (the old app's balances, starter and invite credits). "
            "`spent` and `expired` come from lots of that source; `refunded` counts "
            "refunds of bookings that source paid for."
        )
    )

    @classmethod
    def of(cls, period: Period, alerts: list[Alert], tab: CreditsTab) -> AnalyticsCreditsRead:
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            flow=CreditFiguresRead.of(tab.flow),
            by_source=[
                CreditSourceRead(
                    source=group, **CreditFiguresRead.of(tab.by_source[group]).model_dump()
                )
                for group in CREDIT_GROUPS
            ],
        )


class MetricRead(BaseModel):
    """A figure and the same measure over the previous period. `previous` is
    null when `compare=false`, and for a figure that exists only now."""

    value: int | float | None
    previous: int | float | None

    @classmethod
    def of(cls, metric: Metric) -> MetricRead:
        return cls(value=metric.value, previous=metric.previous)


class SeriesPointRead(BaseModel):
    start: date = Field(description="The bucket's first day.")
    value: int
    previous: int | None


class NorthStarRead(BaseModel):
    completed: MetricRead
    unit: SeriesUnit = Field(
        description="`day` for a window of 14 days or less, else `week` (7-day buckets)."
    )
    series: list[SeriesPointRead] = Field(
        description=(
            "Completed sessions per bucket, oldest first: days, or 7 days counted from "
            "the window's first day (the last may be shorter). `previous` is the same "
            "bucket of the previous period."
        )
    )


class SubTracksRead(BaseModel):
    unique_mentees: MetricRead
    mentors_delivered: MetricRead
    sessions_per_mentee: MetricRead


class FeatureRead(BaseModel):
    key: FeatureKey
    value: MetricRead | None = Field(
        description="A percentage. Null only for `explore` until it reads PostHog."
    )
    spark: list[float | None] | None = Field(
        description=(
            "8 points, the window in equal parts; a point with nothing to divide by is "
            "null. Null with `value`, and for `mentor_supply`, which has no history."
        )
    )
    contribution: int | float | None = Field(
        description=(
            "What the feature feeds sessions: `messaging` the % of sessions confirmed "
            "in the window whose two people messaged first; `invites` the invitees "
            "with a completed session; `mentor_activation` the mentors with a "
            "completed session within 30 days of approval; else null."
        )
    )


class AnalyticsOverviewRead(SectionRead):
    """The Overview tab."""

    north_star: NorthStarRead
    sub_tracks: SubTracksRead
    features: list[FeatureRead] = Field(description="Always all seven, in a fixed order.")

    @classmethod
    def of(cls, period: Period, alerts: list[Alert], tab: OverviewTab) -> AnalyticsOverviewRead:
        star = tab.north_star
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            north_star=NorthStarRead(
                completed=MetricRead.of(star.completed),
                unit=star.unit,
                series=[
                    SeriesPointRead(start=p.start, value=p.value, previous=p.previous)
                    for p in star.series
                ],
            ),
            sub_tracks=SubTracksRead(
                unique_mentees=MetricRead.of(tab.sub_tracks.unique_mentees),
                mentors_delivered=MetricRead.of(tab.sub_tracks.mentors_delivered),
                sessions_per_mentee=MetricRead.of(tab.sub_tracks.sessions_per_mentee),
            ),
            features=[
                FeatureRead(
                    key=feature.key,
                    value=None if feature.value is None else MetricRead.of(feature.value),
                    spark=feature.spark,
                    contribution=feature.contribution,
                )
                for feature in tab.features
            ],
        )


class SessionKpisRead(BaseModel):
    requests: MetricRead
    acceptance_rate: MetricRead
    median_reply_hours: MetricRead
    show_up_rate: MetricRead
    repeat_booking_rate: MetricRead
    average_rating: MetricRead
    review_rate: MetricRead
    reports: MetricRead


class OutcomesRead(BaseModel):
    requests: int
    completed: int
    cancelled: int
    expired: int
    no_show: int
    declined: int


class TopReasonRead(BaseModel):
    reason: MissedReason
    share: float = Field(description="Its share of the reasons given, as a percentage.")


class GuardrailsRead(BaseModel):
    no_show_rate: MetricRead
    request_expiry_rate: MetricRead
    credit_expiry_rate: MetricRead


class AnalyticsSessionsRead(SectionRead):
    """The Sessions tab."""

    kpis: SessionKpisRead
    outcomes: OutcomesRead = Field(
        description=(
            "Requests made in the window, and what they became (a moved request by "
            "where its moves ended). Pending or upcoming ones are in no bucket."
        )
    )
    top_no_show_reason: TopReasonRead | None
    guardrails: GuardrailsRead

    @classmethod
    def of(cls, period: Period, alerts: list[Alert], tab: SessionsTab) -> AnalyticsSessionsRead:
        top = tab.top_no_show_reason
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            kpis=SessionKpisRead(**{k: MetricRead.of(v) for k, v in tab.kpis.items()}),
            outcomes=OutcomesRead(**tab.outcomes),
            top_no_show_reason=None
            if top is None
            else TopReasonRead(reason=top.reason, share=top.share),
            guardrails=GuardrailsRead(**{k: MetricRead.of(v) for k, v in tab.guardrails.items()}),
        )
