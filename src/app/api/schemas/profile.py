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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.schemas.catalogue import CountryRef, InstitutionRead
from app.api.schemas.common import Normalised
from app.domain.enums import LanguageProficiency


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


# --------------------------------------------------------------------------
# Writes
#
# Each write model sits beside the read it mirrors, so a field added to one and
# forgotten on the other is visible in a single file rather than across two.
#
# **`user_id` and `created_by` appear on none of them.** Whose row this is comes
# from the dependency that resolved the URL; a body field would be a second
# answer to a question that already has one, and the wrong one would be the
# caller's to choose.
# --------------------------------------------------------------------------


class EducationWrite(Normalised):
    """A degree, as submitted.

    ``school_name_raw`` is required and ``institution_id`` is not: a user typing
    a school we do not hold must still be able to save, which is the whole point
    of keeping the raw name (ADR 0008 point 5). When no id is given the server
    matches, and creates a `pending_review` institution if nothing matches
    unambiguously.
    """

    school_name_raw: str = Field(min_length=1, max_length=300)
    institution_id: UUID | None = None
    degree_level_id: UUID | None = None
    degree_category: str | None = Field(default=None, max_length=100)
    study_course: str | None = Field(default=None, max_length=300)
    study_program: str | None = Field(default=None, max_length=300)
    date_start: date | None = None
    date_end: date | None = None
    is_most_recent: bool = False

    @model_validator(mode="after")
    def _dates_ordered(self) -> EducationWrite:
        """The `dates_ordered` CHECK already refuses this in the database.

        Both, deliberately: the boundary gives a 422 naming the field, and the
        constraint is what fails loudly if a future writer forgets — ADR 0016
        point 3's "the database constrains rather than transforms".
        """
        if self.date_start and self.date_end and self.date_end < self.date_start:
            raise ValueError("date_end is before date_start")
        return self


class GoalWrite(Normalised):
    """A study goal, with the countries and needs that go with it.

    Both lists key on the **user** rather than this goal, so submitting them
    replaces what the user has — see the route description, because that is not
    what "editing this goal" implies.
    """

    degree_goal_id: UUID | None = None
    degree_goal_raw: str | None = Field(default=None, max_length=100)
    target_start_term: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    country_ids: list[UUID] | None = None
    need_ids: list[UUID] | None = None


class AwardWrite(Normalised):
    """A scholarship or award. Self-reported — nothing verifies one yet."""

    title: str = Field(min_length=1, max_length=300)
    institution: str = Field(min_length=1, max_length=300)
    scholarship_program_id: UUID | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    evidence_url: str | None = Field(default=None, max_length=1000)


class MentorProfileWrite(Normalised):
    """A mentor profile, on create or update.

    `approval_status`, `listing_status` and every audit field are absent: they
    are the review's to set, not the applicant's. A mentor who could approve
    themselves is not a review.
    """

    headline: str | None = Field(default=None, max_length=300)
    years_of_experience: int | None = Field(default=None, ge=0, le=80)
    requires_booking_confirmation: bool | None = None
    primary_study_country_id: UUID | None = None
    primary_study_program: str | None = Field(default=None, max_length=300)
    offering_ids: list[UUID] | None = None


class UserProfileWrite(Normalised):
    """The profile fields a user may set about themselves.

    **`avatar_url` and `banner_url` are absent, deliberately.** Images are
    content-addressed in Supabase Storage (ADR 0019); accepting a URL here would
    let a profile point at any host and would bypass that scheme entirely.
    Upload is its own build.

    `email_provider_contact_id` and `legacy_bubble_id` are absent because another
    system owns them.

    **`current_country_id` is absent too, and that one was a defect.** The column
    was writable here and read by *nothing* — not the owner's own profile, not the
    admin queue, not the public profile, not the ETL, which never populates it. A
    field a user can PUT and never GET back is worse than an absent one: it looks
    like it was recorded. The column itself survives this change and is a
    candidate for removal alongside the enum conversion.
    """

    about_me: str | None = Field(default=None, max_length=5000)
    gender: str | None = Field(default=None, max_length=50)
    origin_country_id: UUID | None = None
    social_linkedin: str | None = Field(default=None, max_length=500)
    social_twitter: str | None = Field(default=None, max_length=500)
    social_youtube: str | None = Field(default=None, max_length=500)


class EducationPatch(Normalised):
    """A partial education edit.

    **Separate from `EducationWrite` because `PATCH` and `POST` disagree about
    what is required.** Reusing the create model makes `school_name_raw`
    mandatory on every edit, so changing one field means resending fields the
    client may not hold — the first version of this shipped exactly that, and a
    test where a one-field patch silently changed nothing is what found it.

    Every field optional; `exclude_unset` on the way out is what separates "not
    sent" from "set to null".
    """

    school_name_raw: str | None = Field(default=None, min_length=1, max_length=300)
    institution_id: UUID | None = None
    degree_level_id: UUID | None = None
    degree_category: str | None = Field(default=None, max_length=100)
    study_course: str | None = Field(default=None, max_length=300)
    study_program: str | None = Field(default=None, max_length=300)
    date_start: date | None = None
    date_end: date | None = None
    is_most_recent: bool | None = None

    @model_validator(mode="after")
    def _patch_dates_ordered(self) -> EducationPatch:
        if self.date_start and self.date_end and self.date_end < self.date_start:
            raise ValueError("date_end is before date_start")
        return self


class AwardPatch(Normalised):
    """A partial award edit, for the same reason."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    institution: str | None = Field(default=None, min_length=1, max_length=300)
    scholarship_program_id: UUID | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    evidence_url: str | None = Field(default=None, max_length=1000)


class UserLanguageWrite(Normalised):
    """One language a user speaks, and how well."""

    language_id: UUID
    proficiency: LanguageProficiency = LanguageProficiency.FLUENT
    is_primary: bool = False


class UserLanguagesWrite(Normalised):
    """The user's whole language list, replacing whatever is there.

    **At most one may be primary**, and that is enforced by
    `ix_user_languages_one_primary`, a unique partial index — so a second
    primary is an `IntegrityError` rather than a silent duplicate. Refusing it
    here gives the client a 422 naming the problem instead of a 500.

    A language may appear once: `ix_user_languages_user_language` is unique on
    the pair, and a list containing the same language twice is a client bug
    worth naming rather than a constraint violation to decode.
    """

    languages: list[UserLanguageWrite] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_primary_and_no_duplicates(self) -> UserLanguagesWrite:
        primaries = [entry for entry in self.languages if entry.is_primary]
        if len(primaries) > 1:
            raise ValueError("only one language may be primary")
        ids = [entry.language_id for entry in self.languages]
        if len(ids) != len(set(ids)):
            raise ValueError("a language may appear only once")
        return self
