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
    Period,
    SupplyPlace,
    SupplyTab,
)


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
