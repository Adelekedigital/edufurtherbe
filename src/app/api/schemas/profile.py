"""A user's own attributes, over the wire.

Every model here is built by a `from_row` classmethod rather than by
`model_validate` on a query row. The stores return flat columns, because a join
produces columns and not objects, and nesting them in one place is what stops
`GET /users/{id}/education` and the copy embedded in `GET /me` from drifting
into two shapes.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.api.schemas.availability import validated_zone
from app.api.schemas.catalogue import CountryRef, InstitutionRead
from app.api.schemas.common import (
    AvatarFocusRead,
    AvatarFocusWrite,
    LinkedInWrite,
    Normalised,
    publish_window_minimum,
    refuse_repeats,
)
from app.core.config import BOOKING_WINDOW_CEILING, MIN_BOOKING_WINDOW_DAYS
from app.domain.availability import (
    BREAK_AFTER_MINUTES,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MIN_NOTICE_MINUTES,
    MIN_NOTICE_MINUTES,
    SESSION_DURATION_MINUTES,
    BookingWindow,
)
from app.domain.enums import AwardFunding, CoverArt, CoverColor, LanguageProficiency, PrimaryRole
from app.domain.goals import FIELD_OF_STUDY_MAX_LENGTH, MAX_GOAL_SCHOOLS, SCHOOL_NAME_MAX_LENGTH
from app.domain.guide_countries import MAX_GUIDE_COUNTRIES
from app.domain.names import split_full_name
from app.domain.profile_text import ABOUT_ME_MAX_LENGTH, PROMPT_VERSION_MAX_LENGTH
from app.domain.text import has_letter, hidden_characters


class LookupRef(BaseModel):
    """A resolved lookup value: its id, the stable code, and what to show.

    `id` is what a write takes (`degree_level_id`, `service_offering_ids`), so a
    form can send back what it was read without joining a catalogue first.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    display_name: str

    @classmethod
    def maybe(cls, id_: Any, code: Any, display_name: Any) -> LookupRef | None:
        """All three columns or none — an outer join gives nulls together."""
        if id_ is None or code is None or display_name is None:
            return None
        return cls(id=id_, code=str(code), display_name=str(display_name))


#: The longest degree abbreviation a user may enter ("MSc", "MPhil", "Ph.D").
MAX_DEGREE_ABBREVIATION = 20


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
    #: The user's own abbreviation ("MSc"); the public profile shows it before the
    #: level's short name.
    degree_abbreviation: str | None = None
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
                row.get("degree_level_ref_id"),
                row.get("degree_level_slug"),
                row.get("degree_level_name"),
            ),
            degree_abbreviation=row.get("degree_abbreviation"),
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


class GoalSchoolRead(BaseModel):
    """A school the goal names, in the mentee's order.

    `school_name_raw` is always what was typed; `institution_id` and
    `institution_name` are the catalogue row it links to, or both null when
    nothing matched. The link is followed whatever the row's review state
    (decision 63), so a school the mentee typed shows by its own name.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    institution_id: UUID | None = None
    institution_name: str | None = None
    school_name_raw: str
    #: 0-based, in the order the list was sent.
    position: int


class GoalRead(BaseModel):
    """What a mentee is trying to do.

    `countries` and `needs` key on the **user**, not on the goal — the legacy
    shape, kept deliberately — so a user with two goals sees the same lists on
    both. Nesting them per goal would claim a relationship the data does not
    carry. `schools` is the goal's own and goes with it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    degree_goal: LookupRef | None = None
    degree_goal_raw: str | None = None
    field_of_study: str | None = Field(
        default=None, description="What the mentee wants to study, as they picked or typed it."
    )
    target_start_term: str | None = None
    notes: str | None = None
    countries: list[GoalCountryRead] = []
    needs: list[LookupRef] = []
    schools: list[GoalSchoolRead] = Field(
        default=[], description="The schools this goal names, in the mentee's order."
    )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> GoalRead:
        return cls(
            id=row["id"],
            degree_goal=LookupRef.maybe(
                row.get("degree_goal_ref_id"),
                row.get("degree_goal_slug"),
                row.get("degree_goal_name"),
            ),
            degree_goal_raw=row.get("degree_goal_raw"),
            field_of_study=row.get("field_of_study"),
            target_start_term=row.get("target_start_term"),
            notes=row.get("notes"),
            countries=[
                GoalCountryRead(
                    code=c["code"], display_name=c["display_name"], priority=c["priority"]
                )
                for c in row.get("countries", [])
            ],
            needs=[
                LookupRef(id=n["id"], code=n["slug"], display_name=n["display_name"])
                for n in row.get("needs", [])
            ],
            schools=[GoalSchoolRead.model_validate(s) for s in row.get("schools", [])],
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
    funding: AwardFunding | None = Field(
        default=None,
        description=(
            "`full` or `partial`, as the holder says. **Null when the mentor hasn't said**, "
            'which is most awards: show "fully funded" only when this is `full`.'
        ),
    )
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
            funding=row.get("funding"),
            verification_status=str(row["verification_status"]),
            evidence_url=row.get("evidence_url"),
        )


class PauseRequest(Normalised):
    """An optional return date when a mentor pauses themselves."""

    model_config = ConfigDict(extra="forbid")

    return_on: date | None = Field(
        default=None,
        description=(
            "When you expect to be back, a date in your own zone after today; null "
            "or omitted means not sure yet. Sending it again while paused changes it."
        ),
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
    # Back to a plain bool, and back off the primary offering. `mentor_profiles`
    # holds it `NOT NULL` and a mentor row always exists, so there is no mentor
    # for whom this is unknown — which is the difference between inheriting from
    # a row that must exist and one that need not.
    #
    # Narrowing rather than breaking: a client that handled the old `null` still
    # validates a bool, and the field never stops being present.
    requires_booking_confirmation: bool
    #: Your defaults for every offering that does not set its own (#204); null
    #: means the platform's: `default_booking_window_days` ahead, no break.
    booking_window_days: int | None = None
    break_after_minutes: int | None = None
    #: Your default length and notice for every offering that does not set its
    #: own (#216); null means the platform's.
    default_duration_minutes: int | None = Field(
        default=None,
        description=(
            "Minutes a session runs on every offering that does not set its own; "
            f"null means the platform's {DEFAULT_DURATION_MINUTES}."
        ),
    )
    default_min_notice_minutes: int | None = Field(
        default=None,
        description=(
            "How far ahead, in minutes, every offering that does not set its own "
            f"must be booked; null means the platform's {DEFAULT_MIN_NOTICE_MINUTES} "
            "(24 hours)."
        ),
    )
    # `default_meeting_venue` is **removed**, not made optional. It was read
    # through `primary_session_type_id`, which is dropped, and venue has no
    # mentor-level home to fall back to. Keeping the key with a permanent `null`
    # would assert "this mentor has no venue" — a claim about the mentor, when
    # venue is a property of an offering. The owner reads it per offering in
    # `/me/session-types`. This removes a field from a shipped response and is
    # breaking, deliberately.
    primary_study_program: str | None = None
    primary_study_country: CountryRef | None = None
    offerings: list[LookupRef] = []
    guide_countries: list[LookupRef] = Field(
        default_factory=list,
        description=(
            "Countries you can guide mentees towards, in your order; empty when you "
            "have named none. Write them as `guide_country_ids`."
        ),
    )
    #: True only while you are unlisted **by your own pause** — an admin's
    #: unlisting reads `listing_status: unlisted` with this false (#75).
    paused_by_mentor: bool = Field(
        description="You paused yourself; only then can you resume (Busy, I'm back)."
    )
    return_on: date | None = Field(
        default=None,
        description=(
            "The date you expect to be back, in your own zone, or null. A reminder "
            "only: you are emailed that morning and stay paused until you resume."
        ),
    )
    #: The platform's window, so the "Bookable up to" options come from it
    #: rather than a hard-coded list (Round 5). Configuration, not the mentor's.
    max_booking_window_days: int = Field(
        description="The most days ahead any offering may be booked on this platform."
    )
    default_booking_window_days: int = Field(
        description="What an offering uses when neither it nor you set a window."
    )
    #: The platform's response window (decision 278), so the Booking tab's line
    #: reads it rather than a hard-coded number. Configuration, not the mentor's.
    response_window_hours: int | None = Field(
        default=None,
        description=(
            "How many hours before a session a request must be answered; "
            "unanswered by then, it expires and the mentee can book another time. "
            "Each request's own deadline is its `respond_by`."
        ),
    )
    # Mentor Onboarding (B2). The owner's own view: an admin reading this
    # profile gets null in all four; the decision log is where they read notes.
    submitted_at: datetime | None = Field(
        default=None,
        description="When you submitted your application; null until you do. Owner only.",
    )
    approved_seen_at: datetime | None = Field(
        default=None,
        description="When you first saw the approved screen; null until then. Owner only.",
    )
    review_note: str | None = Field(
        default=None,
        description=(
            "The reviewer's note on the latest decline, **only while declined**; "
            "null otherwise, or when the reviewer left none. Owner only."
        ),
    )
    reviewed_at: datetime | None = Field(
        default=None,
        description="When that decline was made, only while declined. Owner only.",
    )

    @classmethod
    def from_row(
        cls,
        row: dict[str, Any],
        window: BookingWindow,
        *,
        owner: bool,
        response_window_hours: int,
    ) -> MentorProfileRead:
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
            booking_window_days=row.get("booking_window_days"),
            break_after_minutes=row.get("break_after_minutes"),
            default_duration_minutes=row.get("default_duration_minutes"),
            default_min_notice_minutes=row.get("default_min_notice_minutes"),
            primary_study_program=row.get("primary_study_program"),
            primary_study_country=country,
            offerings=[
                LookupRef(id=o["id"], code=o["slug"], display_name=o["display_name"])
                for o in row.get("offerings", [])
            ],
            guide_countries=[LookupRef.model_validate(c) for c in row["guide_countries"]],
            paused_by_mentor=bool(row.get("paused_by_mentor")),
            return_on=row.get("return_on"),
            max_booking_window_days=window.max_days,
            default_booking_window_days=window.default_days,
            response_window_hours=response_window_hours,
            submitted_at=row.get("submitted_at") if owner else None,
            approved_seen_at=row.get("approved_seen_at") if owner else None,
            review_note=row.get("review_note") if owner else None,
            reviewed_at=row.get("reviewed_at") if owner else None,
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
    degree_abbreviation: str | None = Field(default=None, max_length=MAX_DEGREE_ABBREVIATION)
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


class GoalSchoolWrite(Normalised):
    """A school the goal names: picked from the catalogue, or typed.

    The education entry's rule (`EducationWrite`): the name is always kept, and
    the server resolves the link — a supplied `institution_id` is validated, a
    name without one is matched, and an unmatched name joins the review queue.
    """

    institution_id: UUID | None = None
    school_name_raw: str = Field(min_length=1, max_length=SCHOOL_NAME_MAX_LENGTH)


class GoalWrite(Normalised):
    """A study goal, with the countries, needs and schools that go with it.

    Countries and needs key on the **user** rather than this goal, so submitting
    them replaces what the user has — see the route description, because that is
    not what "editing this goal" implies. Schools are the goal's own.
    """

    degree_goal_id: UUID | None = None
    degree_goal_raw: str | None = Field(default=None, max_length=100)
    #: Decision 259. `null` or a blank clears it; omitted leaves it. Composed
    #: and measured after composing, like a name; the raw bound only caps the
    #: request.
    field_of_study: str | None = Field(
        default=None,
        max_length=4 * FIELD_OF_STUDY_MAX_LENGTH,
        description=(
            "What you want to study: a suggestion or your own words, at most "
            f"{FIELD_OF_STUDY_MAX_LENGTH} characters. `null` clears it."
        ),
        examples=["Computer Science"],
    )
    target_start_term: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    country_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "`/catalog/countries` ids, highest priority first; the list replaces the saved "
            "one. A country twice is a `422` at `/country_ids`; an id that names no "
            "country is a `422` at `/country_ids/{index}`. Nothing is saved on a `422`."
        ),
    )
    need_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "`/catalog/service-offerings` ids; the list replaces the saved one. A need "
            "twice is a `422` at `/need_ids`; an id that names no offering is a `422` "
            "at `/need_ids/{index}`. Nothing is saved on a `422`."
        ),
    )
    #: Omitted or `null` leaves the list, as `country_ids` does; `[]` clears it.
    schools: list[GoalSchoolWrite] | None = Field(
        default=None,
        max_length=MAX_GOAL_SCHOOLS,
        description=(
            f"Up to {MAX_GOAL_SCHOOLS} schools, in your order, replacing the whole list. "
            "Omit it (or send `null`) to leave the list as it is; `[]` clears it."
        ),
    )

    @field_validator("field_of_study")
    @classmethod
    def _visible_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _composed_visible(value, "a field of study", FIELD_OF_STUDY_MAX_LENGTH)

    @field_validator("country_ids")
    @classmethod
    def _each_country_once(cls, value: list[UUID] | None) -> list[UUID] | None:
        refuse_repeats(value, "a country appears twice")
        return value

    @field_validator("need_ids")
    @classmethod
    def _each_need_once(cls, value: list[UUID] | None) -> list[UUID] | None:
        refuse_repeats(value, "a need appears twice")
        return value


class AwardWrite(Normalised):
    """A scholarship or award. Self-reported — nothing verifies one yet."""

    title: str = Field(min_length=1, max_length=300)
    institution: str = Field(min_length=1, max_length=300)
    scholarship_program_id: UUID | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    evidence_url: str | None = Field(default=None, max_length=1000)
    funding: AwardFunding | None = Field(
        default=None, description="`full`, `partial`, or null for not said."
    )


class MentorProfileWrite(Normalised):
    """A mentor profile, on create or update.

    `approval_status`, `listing_status` and every audit field are absent: they
    are the review's to set, not the applicant's. A mentor who could approve
    themselves is not a review.
    """

    headline: str | None = Field(default=None, max_length=300)
    years_of_experience: int | None = Field(default=None, ge=0, le=80)
    #: **`bool`, not `bool | None`** — the one field here whose column is
    #: `NOT NULL`. Every route dumps with `exclude_unset=True`, so a field the
    #: client omits never reaches the writer and the default below is never
    #: read; what `| None` bought was not optionality but the right to send an
    #: explicit `null`, which `_sent` forwarded to a `NOT NULL` column as a 500.
    #: A 422 is the correct answer to a value the column cannot hold. The default
    #: is the column's (`false`); none is published, because a spec `default`
    #: makes generated clients type the field as always sent (`test_patch_bodies`).
    requires_booking_confirmation: bool = Field(default=None)  # type: ignore[assignment]
    #: The defaults every offering inherits unless it sets its own (#204); null
    #: means the platform's — the full horizon, and no break.
    booking_window_days: int | None = Field(
        default=None,
        ge=1,
        le=BOOKING_WINDOW_CEILING,
        json_schema_extra=publish_window_minimum,
        description=(
            "Days ahead your offerings can be booked unless one sets its own: "
            f"{MIN_BOOKING_WINDOW_DAYS} to "
            "`max_booking_window_days` (outside that range is a 422, except that a PATCH "
            "resending the value already stored is accepted); null is the platform default."
        ),
    )
    break_after_minutes: int | None = Field(
        default=None, ge=BREAK_AFTER_MINUTES[0], le=BREAK_AFTER_MINUTES[1]
    )
    #: The length and notice every offering inherits unless it sets its own
    #: (#216), in the offering's own ranges. Null means the platform's.
    default_duration_minutes: int | None = Field(
        default=None,
        ge=SESSION_DURATION_MINUTES[0],
        le=SESSION_DURATION_MINUTES[1],
        description=(
            f"Minutes ({SESSION_DURATION_MINUTES[0]}-{SESSION_DURATION_MINUTES[1]}) "
            "for every offering that does not set its own; null is the platform's "
            f"{DEFAULT_DURATION_MINUTES}."
        ),
    )
    default_min_notice_minutes: int | None = Field(
        default=None,
        ge=MIN_NOTICE_MINUTES[0],
        le=MIN_NOTICE_MINUTES[1],
        description=(
            f"Minutes of notice ({MIN_NOTICE_MINUTES[0]}-{MIN_NOTICE_MINUTES[1]}, "
            "24 to 72 hours) for every offering that does not set its own; null is "
            "the platform's 24 hours."
        ),
    )
    primary_study_country_id: UUID | None = Field(
        default=None,
        description=(
            "Where you studied, a `/catalog/countries` id; `null` clears it. One that "
            "names no country is a `422` at this field."
        ),
    )
    primary_study_program: str | None = Field(default=None, max_length=300)
    offering_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "The `/catalog/service-offerings` ids you help with; the list replaces the "
            "saved one, `[]` clears it, omitted or `null` leaves it. An offering twice is "
            "a `422` at `/offering_ids`; one that is unknown or retired is a `422` at "
            "`/offering_ids/{index}`. Nothing is saved on a `422`."
        ),
    )
    #: Decision 276. Absent and `null` alike leave the list, as `offering_ids`
    #: does, so one body carries one absent-value rule.
    guide_country_ids: list[UUID] | None = Field(
        default=None,
        max_length=MAX_GUIDE_COUNTRIES,
        description=(
            f"Up to {MAX_GUIDE_COUNTRIES} `/catalog/countries` ids you can guide mentees "
            "towards, in your order; the list replaces the saved one. Omitted or `null` "
            f"leaves it, `[]` clears it. More than {MAX_GUIDE_COUNTRIES} or a country "
            "twice is a `422` at `/guide_country_ids`; an id that names no country is a "
            "`422` at `/guide_country_ids/{index}`. Nothing is saved on a `422`."
        ),
    )

    @field_validator("guide_country_ids")
    @classmethod
    def _each_country_once(cls, value: list[UUID] | None) -> list[UUID] | None:
        refuse_repeats(value, "a country appears twice")
        return value

    @field_validator("offering_ids")
    @classmethod
    def _each_offering_once(cls, value: list[UUID] | None) -> list[UUID] | None:
        refuse_repeats(value, "an offering appears twice")
        return value


#: A person's first or last name, trimmed. Generous: legal names run long.
MAX_NAME_LENGTH = 100


def _refuse_hidden(value: str, what: str = "a name") -> None:
    if hidden_characters(value):
        raise ValueError(f"{what} cannot contain control or invisible characters")


def _composed_visible(value: str, what: str, max_length: int) -> str:
    """Typed text as a person would read it: composed (NFC), no hidden
    characters, at least one letter, and at most `max_length` after composing.
    One rule for names and a field of study; `what` names it in the message."""
    composed = unicodedata.normalize("NFC", value)
    _refuse_hidden(composed, what)
    if not has_letter(composed):
        raise ValueError(f"{what} needs at least one letter")
    if len(composed) > max_length:
        raise ValueError(f"{what} can be at most {max_length} characters")
    return composed


def _a_visible_name(value: str) -> str:
    """A name as a person would read it: composed, with no hidden characters.

    **Refused, not stripped.** A name reaches public cards, email variables and
    a call's display name; silently removing a right-to-left override or a
    newline would store something the user did not type. **A joiner between two
    letters is spelling, not hiding** — Persian and Sinhala names need them —
    so `hidden_characters` allows exactly that. Composed (NFC) so an accent
    typed two ways is one spelling, and **measured after composing**, since NFC
    can lengthen a name and the bound is on what is stored. A name must hold a
    letter, in any script — `---` is not one.
    """
    return _composed_visible(value, "a name", MAX_NAME_LENGTH)


class UserProfileWrite(Normalised):
    """The profile fields a user may set about themselves.

    **`avatar_url` and `banner_url` are absent, deliberately.** Images are
    content-addressed in Supabase Storage (ADR 0019); accepting a URL here would
    let a profile point at any host and would bypass that scheme entirely.
    Upload is its own build.

    `email_provider_contact_id` and `legacy_bubble_id` are absent because another
    system owns them.

    **`current_country_id` is writable because it is now read back.** It was
    once removed from here: writable and read by nothing, it looked recorded and
    was not. The Mentee Settings PRD made it a field the owner sees, so `GET /me`
    returns it as `profile.current_country`, and it is written here again. It is
    the owner's alone: no public or mentor-facing response carries it.

    A country id that names no country, here or in `origin_country_id`, is a
    `422` at that field, never the foreign-key 500.
    """

    #: **`str`, not `str | None`**, so a name can be changed and never cleared:
    #: an explicit `null` — or a blank, which `Normalised` turns into one — is a
    #: `422`, the rule `timezone` follows. Omitted leaves it alone. On `users`,
    #: written in the same transaction; the slug is never derived from it, so a
    #: rename keeps every shared profile link (#213).
    #:
    #: The `None` default is never validated or written — the writer takes
    #: `exclude_unset` — and it is what lets the spec say "a string, optional"
    #: rather than "nullable", which is the contract. Hence these ignores.
    first_name: str = Field(  # type: ignore[assignment]
        default=None,
        max_length=MAX_NAME_LENGTH,
        description="Your first name, as shown on your profile. Cannot be cleared.",
    )
    last_name: str = Field(  # type: ignore[assignment]
        default=None,
        max_length=MAX_NAME_LENGTH,
        description="Your last name. Cannot be cleared.",
    )
    #: Both names as one string (decision 258), declared after them so the
    #: validator can see whether either was sent too. Not a column: the model
    #: validator writes its parts to the two names, and `exclude` keeps it out
    #: of the writer's payload. The length bound only caps the request; the
    #: rule is each part's, after composing, since spaces and decomposed
    #: accents lengthen what is sent and not what is stored.
    full_name: str = Field(  # type: ignore[assignment]
        default=None,
        max_length=10 * MAX_NAME_LENGTH,
        exclude=True,
        description=(
            "Your first and last name as one string, split on the first word: "
            '"Damilola Ade Ogunleye" saves first name `Damilola` and last name '
            "`Ade Ogunleye`. Each part follows the `first_name` rule. A single "
            "word, invisible characters, or sending it with `first_name` or "
            "`last_name` is a `422` at `/full_name`."
        ),
        examples=["Damilola Ade Ogunleye"],
    )
    about_me: str | None = Field(
        default=None,
        max_length=ABOUT_ME_MAX_LENGTH,
        description=f"Your bio, at most {ABOUT_ME_MAX_LENGTH} characters. `null` clears it.",
    )
    #: Where the bio came from (decision 267). **No `null`** (a `422`): say
    #: `false`. The server stamps the time; a client never sends one. The
    #: writer turns both into columns; the rules are `provenance_problem`'s.
    about_me_drafted: bool = Field(  # type: ignore[assignment]
        default=None,
        description=(
            "`true` when the `about_me` sent with it is a writing-assist suggestion "
            "(as returned, or edited): send `about_me` and `about_me_prompt_version` "
            "too, and the server records when. `false` says the bio is your own and "
            "clears the record. Omitted, the record stays while `about_me` is "
            "unchanged and is cleared when it changes."
        ),
    )
    about_me_prompt_version: str = Field(  # type: ignore[assignment]
        default=None,
        max_length=PROMPT_VERSION_MAX_LENGTH,
        description=(
            "The `prompt_version` `POST /me/writing-assist` returned, e.g. "
            "`bio_draft-v1`. Only with `about_me_drafted: true`; anything else, or "
            "a version that is not a bio prompt's, is a `422` at this field."
        ),
        examples=["bio_draft-v1"],
    )
    gender: str | None = Field(default=None, max_length=50)
    origin_country_id: UUID | None = None
    current_country_id: UUID | None = Field(
        default=None,
        description=(
            "Where you live now, a `/catalog/countries` id; `null` clears it. Read back "
            "on `GET /me` as `profile.current_country`, and shown to nobody else."
        ),
    )
    #: A mentor's own crop (#214): overrides the detected face and survives the
    #: backfill. Needs a photo — there is nothing to centre without one — and a
    #: new photo replaces it, since a crop of the old picture means nothing on
    #: the new one. `null` is a `422`: a choice is replaced, not unset.
    avatar_focus: AvatarFocusWrite = Field(  # type: ignore[assignment]
        default=None,
        description=(
            "Where to centre your photo, as 0..1 fractions of the image — the same "
            "shape `avatar_focus` is read in. Needs a photo; a new photo replaces it."
        ),
    )
    cover_color: CoverColor | None = Field(
        default=None,
        description=(
            "The cover colour when there is no banner image, one of the design's "
            "twelve keys. `null` means automatic: the client derives a colour "
            "from the mentor's id over the same list, in the same order."
        ),
    )
    #: **`CoverArt`, not `CoverArt | None`**, for the reason
    #: `requires_booking_confirmation` gives: the column is `NOT NULL`, so an
    #: explicit `null` is a `422` rather than a 500. Omitted leaves it alone.
    #: Its default is the column's; published here as `None`, like the names,
    #: because a spec `default` makes generated clients type the field as always
    #: sent — a PATCH body must publish none (`test_patch_bodies`).
    cover_art: CoverArt = Field(  # type: ignore[assignment]
        default=None,
        description="What is drawn over the cover colour. `none` for a new profile.",
    )
    #: **The viewer's own zone** (booking request item 7, #206), on `users` rather than
    #: `user_profiles` — the writer updates it in the same transaction. Validated
    #: by the same check availability rules use (#36); an explicit `null` is a
    #: `422`, because the column is `NOT NULL` — the rule
    #: `requires_booking_confirmation` follows. Omitted leaves it alone; the
    #: column's own default (`UTC`) is what a new account starts with.
    timezone: str = Field(  # type: ignore[assignment]
        default=None,
        description=(
            "Your IANA timezone, e.g. `Africa/Lagos` — used to show you times in "
            "your own zone. Not an offset: an offset goes stale twice a year."
        ),
        examples=["Africa/Lagos"],
    )

    #: Which dashboard you land on (decision 17: a hint, never an authorization
    #: claim). "Continue as a mentee" sets `mentee` (Mentor Onboarding B3).
    #: An explicit `null` is a `422`, as for `timezone`; omitted leaves it.
    primary_role: PrimaryRole = Field(  # type: ignore[assignment]
        default=None,
        description=(
            "Which dashboard you land on: `mentee` or `mentor`. A hint only; it "
            "grants nothing, and a mentor application is kept whichever you pick."
        ),
    )

    _names = field_validator("first_name", "last_name")(_a_visible_name)

    @field_validator("full_name")
    @classmethod
    def _full_name(cls, value: str, info: ValidationInfo) -> str:
        """Checked whole before it is split, so a line break is refused rather
        than read as the space between two names."""
        _refuse_hidden(value)
        # A name sent and valid is in `info.data`; one not sent is its `None`
        # default, and one sent and invalid is absent and already a `422`.
        if info.data.get("first_name") is not None or info.data.get("last_name") is not None:
            raise ValueError("send `full_name` or `first_name` and `last_name`, not both")
        return " ".join(_a_visible_name(part) for part in split_full_name(value))

    @model_validator(mode="after")
    def _full_name_to_parts(self) -> UserProfileWrite:
        # Split again by the same rule, which gives back the validated parts.
        # Assigning marks both names as set, so `exclude_unset` sends them on.
        if "full_name" in self.model_fields_set:
            self.first_name, self.last_name = split_full_name(self.full_name)
        return self

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        return validated_zone(value)

    social_linkedin: LinkedInWrite = Field(
        default=None,
        description=(
            "`ada-lovelace` or a linkedin.com/in/ (or /company/) link, stored as "
            "`https://www.linkedin.com/in/ada-lovelace`. Anything else is a `422`."
        ),
    )


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
    degree_abbreviation: str | None = Field(default=None, max_length=MAX_DEGREE_ABBREVIATION)
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
    funding: AwardFunding | None = Field(
        default=None,
        description="`full`, `partial`, or null to clear. Absent leaves it unchanged.",
    )


class UserLanguageWrite(Normalised):
    """One language a user speaks, and how well.

    **Omitted keeps what is stored**: a language already listed keeps its
    proficiency and primary flag, a new one takes the defaults
    (`profile_writer.NEW_LANGUAGE`). The profile editor only picks languages, so
    a save from it must not reset them. `null` is a `422`: neither column holds
    one. No default is published, so generated clients type both as optional.
    """

    language_id: UUID
    proficiency: LanguageProficiency = Field(default=None)  # type: ignore[assignment]
    is_primary: bool = Field(default=None)  # type: ignore[assignment]


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
        refuse_repeats(
            [entry.language_id for entry in self.languages], "a language may appear only once"
        )
        return self


class AvatarUploadRead(BaseModel):
    """The stored avatar, and where to centre it — so a client can show the new
    photo without fetching `/me` again. `avatar_focus` is what is stored after
    the upload: usually the detected face, but a re-upload of the same photo
    keeps a chosen crop."""

    avatar_url: str
    avatar_focus: AvatarFocusRead | None = None


class EducationCreatedRead(BaseModel):
    """A degree was added."""

    id: UUID = Field(description="The new education entry's id.")
    institution_created: bool = Field(
        description=(
            "True when the school typed was new: it is saved but stays out of "
            "search until an admin has reviewed it."
        )
    )


class ListingPausedRead(BaseModel):
    """A mentor paused their own listing."""

    paused: bool = Field(description="Always true: the listing is paused.")


class ListingResumedRead(BaseModel):
    """A mentor resumed their own listing."""

    resumed: bool = Field(description="Always true: the listing is live again.")


class BannerUploadedRead(BaseModel):
    """A profile banner was stored."""

    banner_url: str = Field(description="Where the stored banner is served from.")
