"""The admin Mentors page: summary cards, tab rows and one mentor's drawer
(Admin PRD, 2026-10-26; decision 297).

Admin-only, like `schemas/admin.py`. The drawer reuses the shapes `/me` and the
public profile already publish for the same facts (education, help areas,
weekly hours), so one client mapper reads them everywhere.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.availability import AvailabilityRuleRead
from app.api.schemas.common import LinkedInRead
from app.api.schemas.profile import EducationRead, LookupRef
from app.domain.admin_mentors import (
    AdminMentorTab,
    BookingOutcome,
    PausedBy,
    ShowUpTier,
    booking_outcome,
    show_up_tier,
    waiting_days,
)
from app.domain.enums import CoverColor, SessionStatus


class AdminTabCountsRead(BaseModel):
    """How many mentors each tab holds."""

    applications: int
    approved: int
    paused: int
    not_approved: int


class AdminMentorsSummaryRead(BaseModel):
    """The three summary cards, the tab counts and the nav badge."""

    counts: AdminTabCountsRead
    applications_longest_wait_days: int | None = Field(
        description="Whole days the oldest waiting application has waited; null with none."
    )
    approved_average_show_up: int | None = Field(
        description=(
            "The mean show-up rate of approved, listed mentors who have one, as a "
            "whole percent rounded half up; null when none has a rate."
        )
    )
    at_risk: int = Field(
        description="Approved, listed mentors whose show-up tier is `at_risk` (under 50%)."
    )

    @classmethod
    def of(cls, summary: dict[str, Any], now: dt.datetime) -> AdminMentorsSummaryRead:
        return cls(
            counts=AdminTabCountsRead(**summary["counts"]),
            applications_longest_wait_days=waiting_days(summary["oldest_submitted_at"], now),
            approved_average_show_up=summary["approved_average_show_up"],
            at_risk=summary["at_risk"],
        )


class AdminSchoolRead(BaseModel):
    """The school that represents a mentor: their highest degree's."""

    name: str
    country: str | None = None


class _AdminMentorBase(BaseModel):
    """What a row and the drawer both carry."""

    user_id: UUID
    handle: str | None = Field(
        description=(
            "Their public handle, for `/mentors/{handle}`; null without a slug, "
            "when `user_id` resolves the profile instead."
        )
    )
    first_name: str | None
    last_name: str | None
    email: str
    avatar_url: str | None
    cover_color: CoverColor | None
    submitted_at: dt.datetime | None = Field(
        description="When they submitted the application; null for a migrated mentor."
    )
    waiting_days: int | None = Field(
        description="Whole UTC days since `submitted_at`, while the application waits."
    )
    approved_at: dt.datetime | None = Field(description="While approved: when.")
    decided_at: dt.datetime | None = Field(description="While declined: when.")
    decline_reason: str | None = Field(description="While declined: the comment sent.")
    paused_at: dt.datetime | None = Field(description="While paused: when they were unlisted.")
    paused_by: PausedBy | None = Field(description="While paused: an admin or the mentor.")
    paused_reason: str | None = Field(
        description="An admin's pause reason, for admins only; null for a mentor's own pause."
    )

    @staticmethod
    def common(row: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        waiting = row["tab"] == AdminMentorTab.APPLICATIONS
        return {
            "user_id": row["user_id"],
            "handle": row["handle"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "email": row["email"],
            "avatar_url": row["avatar_url"],
            "cover_color": row["cover_color"],
            "submitted_at": row["submitted_at"],
            "waiting_days": waiting_days(row["submitted_at"], now) if waiting else None,
            "approved_at": row["approved_at"],
            "decided_at": row["decided_at"],
            "decline_reason": row["decline_reason"],
            "paused_at": row["paused_at"],
            "paused_by": row["paused_by"],
            "paused_reason": row["paused_reason"],
        }


class AdminMentorRowRead(_AdminMentorBase):
    """One mentor in a tab. Fields that do not apply to the tab are null."""

    first_school: AdminSchoolRead | None
    completed_sessions: int
    upcoming_sessions: int
    show_up_rate: int | None = Field(description="Whole percent; null with nothing settled.")
    show_up_tier: ShowUpTier | None

    @classmethod
    def from_row(cls, row: dict[str, Any], now: dt.datetime) -> AdminMentorRowRead:
        return cls(
            **cls.common(row, now),
            first_school=(
                AdminSchoolRead(name=row["school_name"], country=row["school_country"])
                if row["school_name"] is not None
                else None
            ),
            completed_sessions=row["completed_sessions"],
            upcoming_sessions=row["upcoming_sessions"],
            show_up_rate=row["show_up_rate"],
            show_up_tier=show_up_tier(row["show_up_rate"]),
        )


class AdminMentorStatsRead(BaseModel):
    """The drawer's four figures, all time."""

    completed: int
    upcoming: int
    missed_by_mentor: int
    show_up_rate: int | None
    show_up_tier: ShowUpTier | None


class AdminRecentBookingRead(BaseModel):
    """One of the mentor's five latest booked sessions."""

    id: UUID
    starts_at: dt.datetime
    mentee_first_name: str | None = Field(description="Null when the mentee left.")
    mentee_last_name: str | None
    outcome: BookingOutcome


class AdminPausedRead(BaseModel):
    """Why an approved mentor is off Explore."""

    at: dt.datetime | None
    by: PausedBy
    reason: str | None = Field(description="An admin's reason; null for the mentor's own pause.")


class AdminDeclinedRead(BaseModel):
    """The decision on a declined application."""

    at: dt.datetime | None
    reason: str | None


class AdminMentorRead(_AdminMentorBase):
    """One mentor's review drawer."""

    status: AdminMentorTab = Field(description="The tab the mentor is in.")
    linkedin_url: LinkedInRead = None
    timezone: str
    headline: str | None
    about: str | None
    education: list[EducationRead]
    help_areas: list[LookupRef]
    weekly_hours: list[AvailabilityRuleRead]
    stats: AdminMentorStatsRead
    recent_bookings: list[AdminRecentBookingRead]
    paused: AdminPausedRead | None
    declined: AdminDeclinedRead | None

    @classmethod
    def from_row(cls, row: dict[str, Any], now: dt.datetime) -> AdminMentorRead:
        return cls(
            **cls.common(row, now),
            status=row["tab"],
            linkedin_url=row["social_linkedin"],
            timezone=row["timezone"],
            headline=row["headline"],
            about=row["about_me"],
            education=[EducationRead.from_row(entry) for entry in row["education"]],
            help_areas=[LookupRef.from_offering(o) for o in row["help_areas"]],
            weekly_hours=[AvailabilityRuleRead.from_row(rule) for rule in row["weekly_hours"]],
            stats=AdminMentorStatsRead(
                completed=row["completed_sessions"],
                upcoming=row["upcoming_sessions"],
                missed_by_mentor=row["missed_by_mentor"],
                show_up_rate=row["show_up_rate"],
                show_up_tier=show_up_tier(row["show_up_rate"]),
            ),
            recent_bookings=[
                AdminRecentBookingRead(
                    id=booking["id"],
                    starts_at=booking["starts_at"],
                    mentee_first_name=booking["mentee_first_name"],
                    mentee_last_name=booking["mentee_last_name"],
                    outcome=booking_outcome(
                        SessionStatus(booking["status"]),
                        starts_at=booking["starts_at"],
                        now=now,
                        mentor_missed=booking["mentor_missed"],
                        mentee_missed=booking["mentee_missed"],
                    ),
                )
                for booking in row["recent_bookings"]
            ],
            paused=(
                AdminPausedRead(
                    at=row["paused_at"], by=row["paused_by"], reason=row["paused_reason"]
                )
                if row["paused_by"] is not None
                else None
            ),
            declined=(
                AdminDeclinedRead(at=row["decided_at"], reason=row["decline_reason"])
                if row["tab"] == AdminMentorTab.NOT_APPROVED
                else None
            ),
        )
