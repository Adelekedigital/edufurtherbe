"""A user's own attributes, over the wire.

Every model here is built by a `from_row` classmethod rather than by
`model_validate` on a query row. The stores return flat columns, because a join
produces columns and not objects, and nesting them in one place is what stops
`GET /users/{id}/education` and the copy embedded in `GET /me` from drifting
into two shapes.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.api.schemas.catalogue import CountryRef, InstitutionRead


class LookupRef(BaseModel):
    """A resolved lookup value: the stable code, and what to show."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    display_name: str

    @classmethod
    def maybe(cls, code: Any, display_name: Any) -> LookupRef | None:
        """Both columns or neither — an outer join gives two nulls together."""
        if code is None or display_name is None:
            return None
        return cls(code=str(code), display_name=str(display_name))


class EducationRead(BaseModel):
    """One degree.

    **`school_name_raw` is always present; `institution` may be null.** That pair
    is the whole reason an incomplete catalogue is survivable rather than lossy:
    an entry nothing matched still displays what the user typed (ADR 0008 point
    5), and can be linked later without them re-entering anything.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    school_name_raw: str
    institution: InstitutionRead | None = None
    degree_level: LookupRef | None = None
    #: The unmapped legacy string, kept beside the resolved level for the same
    #: reason `school_name_raw` is kept beside the institution.
    degree_category: str | None = None
    study_course: str | None = None
    study_program: str | None = None
    date_start: date | None = None
    date_end: date | None = None
    is_most_recent: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EducationRead:
        institution = None
        if row.get("institution_id") is not None:
            institution = InstitutionRead.from_row(
                {
                    "id": row["institution_id"],
                    "name": row["institution_name"],
                    "web_page": row["institution_web_page"],
                    "country_code": row.get("country_code"),
                    "country_name": row.get("country_name"),
                }
            )
        return cls(
            id=row["id"],
            school_name_raw=row["school_name_raw"],
            institution=institution,
            degree_level=LookupRef.maybe(
                row.get("degree_level_slug"), row.get("degree_level_name")
            ),
            degree_category=row.get("degree_category"),
            study_course=row.get("study_course"),
            study_program=row.get("study_program"),
            date_start=row.get("date_start"),
            date_end=row.get("date_end"),
            is_most_recent=bool(row.get("is_most_recent")),
        )


class GoalCountryRead(CountryRef):
    """A country the user is aiming for, and how highly."""

    priority: int


class GoalRead(BaseModel):
    """What a mentee is trying to do.

    `countries` and `needs` key on the **user**, not on the goal — the legacy
    shape, kept deliberately — so a user with two goals sees the same lists on
    both. Nesting them per goal would claim a relationship the data does not
    carry.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    degree_goal: LookupRef | None = None
    degree_goal_raw: str | None = None
    target_start_term: str | None = None
    notes: str | None = None
    countries: list[GoalCountryRead] = []
    needs: list[LookupRef] = []

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> GoalRead:
        return cls(
            id=row["id"],
            degree_goal=LookupRef.maybe(row.get("degree_goal_slug"), row.get("degree_goal_name")),
            degree_goal_raw=row.get("degree_goal_raw"),
            target_start_term=row.get("target_start_term"),
            notes=row.get("notes"),
            countries=[
                GoalCountryRead(
                    code=c["code"], display_name=c["display_name"], priority=c["priority"]
                )
                for c in row.get("countries", [])
            ],
            needs=[
                LookupRef(code=n["slug"], display_name=n["display_name"])
                for n in row.get("needs", [])
            ],
        )


class AwardRead(BaseModel):
    """A scholarship or award the user holds.

    `institution` here is free text, not a foreign key — legacy
    `Scholarship-Awards` carried the awarding body as a string and it is not the
    same thing as the university someone attended.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    institution: str
    programme_name: str | None = None
    year: int | None = None
    verification_status: str
    evidence_url: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AwardRead:
        return cls(
            id=row["id"],
            title=row["title"],
            institution=row["institution"],
            programme_name=row.get("programme_name"),
            year=row.get("year"),
            verification_status=str(row["verification_status"]),
            evidence_url=row.get("evidence_url"),
        )


class MentorProfileRead(BaseModel):
    """A mentor's profile.

    `approval_status` and `listing_status` are included because they answer the
    question the owner actually has — *why am I not showing up?* — and this
    endpoint only ever serves the owner or an admin. They are **not** an
    authorization claim: what a mentor may do follows from this row existing, not
    from the value of a field on it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    headline: str | None = None
    years_of_experience: int | None = None
    approval_status: str
    listing_status: str
    requires_booking_confirmation: bool
    default_meeting_venue: str
    primary_study_program: str | None = None
    primary_study_country: CountryRef | None = None
    offerings: list[LookupRef] = []

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MentorProfileRead:
        country = None
        if row.get("primary_study_country_code") is not None:
            country = CountryRef(
                code=str(row["primary_study_country_code"]),
                display_name=str(row["primary_study_country_name"]),
            )
        return cls(
            id=row["id"],
            headline=row.get("headline"),
            years_of_experience=row.get("years_of_experience"),
            approval_status=str(row["approval_status"]),
            listing_status=str(row["listing_status"]),
            requires_booking_confirmation=bool(row["requires_booking_confirmation"]),
            default_meeting_venue=str(row["default_meeting_venue"]),
            primary_study_program=row.get("primary_study_program"),
            primary_study_country=country,
            offerings=[
                LookupRef(code=o["slug"], display_name=o["display_name"])
                for o in row.get("offerings", [])
            ],
        )
