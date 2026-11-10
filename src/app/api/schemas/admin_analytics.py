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
