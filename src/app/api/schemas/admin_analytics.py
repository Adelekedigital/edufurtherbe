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
    DEFAULT_TZ,
    Alert,
    AlertKey,
    AnalyticsTab,
    CreditFigures,
    CreditGroup,
    CreditsTab,
    FeatureKey,
    GrowthTab,
    Metric,
    OverviewTab,
    Period,
    SeriesUnit,
    SessionsTab,
    SignupChannel,
    SignupSource,
    StageKey,
    SupplyPlace,
    SupplyTab,
)
from app.domain.analytics_behaviour import BehaviourTab, FunnelName, FunnelTab
from app.domain.enums import FunnelFigureUnit, FunnelLayer, MissedReason, ProfileViewSource


class WindowRead(BaseModel):
    """The window read, and the previous period it compares with: dates in
    `tz`, both ends inclusive."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    from_: date = Field(alias="from")
    to: date
    previous_from: date
    previous_to: date
    tz: str = Field(
        default=DEFAULT_TZ,
        description=(
            "The IANA time zone every date and day boundary here was read in: the "
            "request's `tz`, or `UTC` when none was sent."
        ),
        examples=["Africa/Lagos"],
    )

    @classmethod
    def of(cls, period: Period) -> WindowRead:
        before = period.previous()
        return cls.model_validate(
            {
                "from": period.first,
                "to": period.last,
                "previous_from": before.first,
                "previous_to": before.last,
                "tz": period.tz,
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
        description="A percentage. Null only for `explore` when PostHog is not answering."
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
            "where its moves ended). Requests still pending or upcoming, and those the "
            "mentee withdrew, are in no bucket, so the buckets can add up to less than "
            "`requests`."
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


class SignUpsRead(BaseModel):
    all: MetricRead
    mentees: MetricRead
    mentors: MetricRead


class SourceRead(BaseModel):
    key: SignupSource
    members: int
    completed_share: float | None = Field(
        description="The share of these members with a completed session, as a percentage."
    )


class ChannelRead(BaseModel):
    key: SignupChannel
    members: int
    completed_share: float | None = Field(
        description="The share of these members with a completed session, as a percentage."
    )


class TopInviterRead(BaseModel):
    name: str = Field(description="First and last name, joined by a space.")
    joined: int


class InvitesRead(BaseModel):
    invited: int
    joined: int
    qualified: int
    first_booking: int
    first_completed: int
    cost_per_completed: float | None = Field(
        description="Always null: since decision 308 no credit is tied to an invite."
    )
    top_inviter: TopInviterRead | None


class MonthCountRead(BaseModel):
    month: str = Field(description="`YYYY-MM`, a UTC calendar month.")
    count: int


class MigratedRead(BaseModel):
    count: int
    on: date | None = Field(description="Always null: no cutover date is recorded.")


class TopicCountRead(BaseModel):
    key: str = Field(description="The service offering's slug.")
    name: str
    count: int


class StageCountRead(BaseModel):
    key: StageKey
    count: int


class AnalyticsGrowthRead(SectionRead):
    """The Growth tab."""

    signups: SignUpsRead
    by_source: list[SourceRead] = Field(
        description="Always empty until sign-ups record where they came from."
    )
    by_channel: list[ChannelRead] = Field(
        description="Always empty until sign-ups record where they came from."
    )
    invites: InvitesRead
    months: list[MonthCountRead] = Field(
        description="Sign-ups in each of the last 6 UTC months, oldest first, this one included."
    )
    migrated: MigratedRead | None = Field(
        description="Accounts carried over from the old app, all time; null with none."
    )
    completed_total: int
    by_topic: list[TopicCountRead] = Field(
        description="Every service offering, in the platform's order, zeros included."
    )
    by_stage: list[StageCountRead] = Field(
        description="Every application stage in order, then `not_set`, zeros included."
    )

    @classmethod
    def of(cls, period: Period, alerts: list[Alert], tab: GrowthTab) -> AnalyticsGrowthRead:
        cohort, top, moved = tab.invites, tab.invites.top_inviter, tab.migrated
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            signups=SignUpsRead(
                all=MetricRead.of(tab.signups.everyone),
                mentees=MetricRead.of(tab.signups.mentees),
                mentors=MetricRead.of(tab.signups.mentors),
            ),
            by_source=[],
            by_channel=[],
            invites=InvitesRead(
                invited=cohort.invited,
                joined=cohort.joined,
                qualified=cohort.qualified,
                first_booking=cohort.first_booking,
                first_completed=cohort.first_completed,
                cost_per_completed=None,
                top_inviter=None
                if top is None
                else TopInviterRead(name=top.name, joined=top.joined),
            ),
            months=[MonthCountRead(month=m.month, count=m.count) for m in tab.months],
            migrated=None if moved is None else MigratedRead(count=moved.count, on=moved.on),
            completed_total=tab.completed_total,
            by_topic=[TopicCountRead(key=t.key, name=t.name, count=t.count) for t in tab.by_topic],
            by_stage=[StageCountRead(key=s.key, count=s.count) for s in tab.by_stage],
        )


class SupplyCohortRead(BaseModel):
    """Mentor applications submitted in the window, by where they stand now."""

    applied: int = Field(description="Submitted in the window.")
    approved: int = Field(description="Of those, approved now.")
    listed: int = Field(description="Of those, approved and listed now.")
    bookable: int = Field(description="Of those, taking bookings now.")


class SupplyPlaceRead(BaseModel):
    """A country or school mentees aim for, and the mentors who can take them."""

    key: str = Field(description="The country's ISO code, or the school's id.")
    name: str
    mentees: int = Field(description="Mentees aiming for it now.")
    bookable_mentors: int = Field(description="Mentors taking bookings who cover it now.")

    @classmethod
    def of(cls, place: SupplyPlace) -> SupplyPlaceRead:
        return cls(
            key=place.key,
            name=place.name,
            mentees=place.mentees,
            bookable_mentors=place.bookable_mentors,
        )


_PLACES = (
    "Only places with at least one mentee, at most five: those with no bookable "
    "mentor first (by name), then most mentees per mentor, then by name. Now, "
    "whatever the window."
)


class AnalyticsSupplyRead(SectionRead):
    """The Supply tab."""

    cohort: SupplyCohortRead = Field(
        description=(
            "Mentor applications submitted in the window (profile and account not "
            "deleted), and how many of them are approved, listed and taking bookings now."
        )
    )
    median_review_days: float | None = Field(
        description=(
            "For approvals and declines made in the window: the median days from the "
            "application's submission to the decision, one decimal. Null with none."
        )
    )
    unlisted: int = Field(
        description=(
            "Mentors unlisted in the window: paused by themselves or an admin. An "
            "unlisting that comes with a decline or a deleted account is not counted."
        )
    )
    by_country: list[SupplyPlaceRead] = Field(
        description=(
            "Goal countries: mentees aiming for each, and bookable mentors who guide it. " + _PLACES
        )
    )
    by_school: list[SupplyPlaceRead] = Field(
        description=(
            "Goal schools in the catalogue: mentees aiming for each, and bookable "
            "mentors who studied there. " + _PLACES
        )
    )

    @classmethod
    def of(cls, period: Period, alerts: list[Alert], tab: SupplyTab) -> AnalyticsSupplyRead:
        cohort = tab.cohort
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            cohort=SupplyCohortRead(
                applied=cohort.applied,
                approved=cohort.approved,
                listed=cohort.listed,
                bookable=cohort.bookable,
            ),
            median_review_days=tab.median_review_days,
            unlisted=tab.unlisted,
            by_country=[SupplyPlaceRead.of(place) for place in tab.by_country],
            by_school=[SupplyPlaceRead.of(place) for place in tab.by_school],
        )


# --- Behaviour (PostHog) -----------------------------------------------------


class FunnelStepRead(BaseModel):
    key: str
    label: str
    value: int | None = Field(description="People who reached it; null when nothing tracks it.")
    previous: int | None = Field(description="The previous period's; null with `compare=false`.")
    # Always sent; the default only keeps it out of `required` for the frontend's CI.
    layer: FunnelLayer = Field(
        default=FunnelLayer.PRIMARY,
        description=(
            "The row it is drawn in: `primary`, the funnel's path, or `secondary`, "
            "its outcomes. A funnel with one row has every step `primary`."
        ),
    )


class FunnelFigureRead(BaseModel):
    """A figure shown beside a funnel, for the same people and window."""

    key: str
    label: str = Field(description="Shown verbatim.")
    value: float | None = Field(description="Null with nothing to measure.")
    previous: float | None = Field(
        description="The previous period's; null with `compare=false` or nothing to measure."
    )
    unit: FunnelFigureUnit = Field(
        description="`days`, one decimal, or `percent`, 0-100 with one decimal."
    )


class AnalyticsFunnelRead(SectionRead):
    """One Behaviour funnel."""

    name: FunnelName
    by: str = Field(description="The breakdown read; `all` when none was asked.")
    note: str = Field(description="The funnel's footer note.")
    window_days: int = Field(description="Days a person has from the first step to the last.")
    steps: list[FunnelStepRead] = Field(description="Every step, in order.")
    # Always sent; the default only keeps it out of `required` for the frontend's CI.
    figures: list[FunnelFigureRead] = Field(
        default_factory=list,
        description=(
            "Figures beside the funnel, in order; empty for a funnel with none. "
            "`mentor_activation`: `days_to_bookable`, the median days from approval "
            "to the first moment weekly hours and a published session type of their "
            "own stood at the same time, of those who got there; "
            "`calendar_connected`, % of the mentors approved who connected a "
            "calendar before 30 days from approval; `profile_link_shared`, % of the "
            "mentors approved with analytics on who shared their own profile link "
            "within 30 days of approval (PostHog `link_shared` with `own` true, so "
            "counted only since the frontend added that flag), null when none has "
            "analytics on or PostHog does not answer. `mentor_application`: "
            "`median_review_days` (the Supply tab's, for decisions made in the window)."
        ),
    )

    @classmethod
    def of(cls, period: Period, alerts: list[Alert], tab: FunnelTab) -> AnalyticsFunnelRead:
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            name=tab.name,
            by=tab.by,
            note=tab.note,
            window_days=tab.window_days,
            steps=[
                FunnelStepRead(
                    key=step.key,
                    label=step.label,
                    layer=step.layer,
                    value=None if step.count is None else int(step.count.value or 0),
                    previous=None
                    if step.count is None or step.count.previous is None
                    else int(step.count.previous),
                )
                for step in tab.steps
            ],
            figures=[
                FunnelFigureRead(
                    key=f.key,
                    label=f.label,
                    value=f.value.value,
                    previous=f.value.previous,
                    unit=f.unit,
                )
                for f in tab.figures
            ],
        )


class LoginCodesRead(BaseModel):
    attempts: int = Field(description="Log-in codes verified or failed.")
    failed_rate: float | None = Field(description="Failed over attempts, as a percentage.")
    failed_migrated_rate: float | None = Field(description="Migrated users' share of failures, %.")


class SearchRead(BaseModel):
    searches: int
    no_result_rate: float | None = Field(description="Searches with no result, %.")
    profile_views: int = Field(description="Profile views from a search result.")


class SourceShareRead(BaseModel):
    source: ProfileViewSource = Field(
        description="Where the views came from; `other` for the rest."
    )
    share: float | None = Field(description="Its share of profile views, as a percentage.")


class JoiningRead(BaseModel):
    clicked: int
    failed: int
    median_minutes_before: float | None = Field(
        description="Median minutes before the start that Join was clicked; null with none."
    )


class WritingAssistCardRead(BaseModel):
    used: int
    accepted_rate: float | None
    discarded_rate: float | None


class AppCardRead(BaseModel):
    installed: int = Field(description="App installs in the window.")
    push_allowed_rate: float | None = Field(description="Who allowed push over who was active, %.")


class RouteFailuresRead(BaseModel):
    route: str
    count: int


class AnalyticsBehaviourRead(SectionRead):
    """The Behaviour tab's event cards. Now and for the window only: no `previous`."""

    login: LoginCodesRead
    search: SearchRead
    profile_views_by_source: list[SourceShareRead] = Field(description="Most first.")
    joining: JoiningRead
    writing_assist: WritingAssistCardRead
    app: AppCardRead
    errors_by_screen: list[RouteFailuresRead] = Field(description="Top 3 routes by failures.")
    replays_url: str | None = Field(
        description="This environment's PostHog replays; null if unset."
    )

    @classmethod
    def of(
        cls, period: Period, alerts: list[Alert], tab: BehaviourTab, replays_url: str | None
    ) -> AnalyticsBehaviourRead:
        return cls(
            window=WindowRead.of(period),
            alerts=[AlertRead.of(alert) for alert in alerts],
            login=LoginCodesRead.model_validate(tab.login),
            search=SearchRead.model_validate(tab.search),
            profile_views_by_source=[
                SourceShareRead(source=row.key, share=row.share)
                for row in tab.profile_views_by_source
            ],
            joining=JoiningRead.model_validate(tab.joining),
            writing_assist=WritingAssistCardRead.model_validate(tab.writing_assist),
            app=AppCardRead.model_validate(tab.app),
            errors_by_screen=[
                RouteFailuresRead(route=row.key, count=row.count) for row in tab.errors_by_screen
            ],
            replays_url=replays_url,
        )
