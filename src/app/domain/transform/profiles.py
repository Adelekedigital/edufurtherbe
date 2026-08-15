"""M2 profile Things into the rows the profile tables expect.

Pure: dictionaries in, dataclasses out. Same contract as ``identity``, and the
same reason — every mapping decision here is testable without a database, which
is most of what makes a migration reviewable.

**Ownership is joined from the user side, not from ``Creator``.** ``identity``
established this and the reasoning carries: the link exists in exactly one
direction, and a row nobody points at cannot be attributed to anyone by guessing
from an email address. ``User.📚Education``, ``User.member goal``,
``User.mentor service`` and ``User.Mentor`` are the joins.

``Creator`` is used two ways instead. On the four Things above it is a
**cross-check** — where it disagrees with the link the row is still loaded and
the disagreement reported, because the link is authoritative and a silent
mismatch is worth knowing about. On ``Scholarship-Awards`` it is the **only**
path: that Thing has no user-side link in either direction, so attribution by
creator is the alternative to dropping seventeen rows. That is an attribution
guess and is labelled as one here rather than discovered later.

**Every lookup raises on an unknown value.** A migration that quietly maps an
unrecognised service to nothing produces plausible rows and no error, and the
only person who could catch it is the one who already suspects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any

from app.domain.bubble import (
    CREATED_AT,
    MODIFIED_AT,
    legacy_anchor,
    normalise_list,
    parse_timestamp,
)
from app.domain.enums import ApprovalStatus, ListingStatus, MeetingProvider, UnlistedReason
from app.domain.transform.identity import TransformError

# --------------------------------------------------------------------------
# Field names
#
# Literals rather than inline strings, and read off the export rather than
# transcribed: `studyProgram-O/S ` carries a trailing space and four of the
# mentor fields begin with an emoji. Both are invisible in a diff.
# --------------------------------------------------------------------------

CREATOR_FIELD = "Creator"

EDUCATION_LINK_FIELD = "\U0001f4daEducation"
GOAL_LINK_FIELD = "member goal"
SERVICE_LINK_FIELD = "mentor service"
MENTOR_LINK_FIELD = "Mentor"

SCHOOL_FIELD = "schoolName"
DEGREE_CATEGORY_FIELD = "degreeCategory"
STUDY_COURSE_FIELD = "studyCourse"
STUDY_PROGRAM_FIELD = "studyProgram-O/S "
DATE_START_FIELD = "dateStart"
DATE_END_FIELD = "dateEnd"
MOST_RECENT_FIELD = "mostRecentDegree"
SHORT_FORM_FIELD = "shortForm"

GOAL_DEGREE_FIELD = "degreeGoal(text)"
GOAL_COUNTRIES_FIELD = "Country Goal"
GOAL_NEEDS_FIELD = "Mentorship Goals(Text)"

MENTOR_SERVICES_FIELD = "Mentor Services/Support(text)"

AWARD_INSTITUTION_FIELD = "Award-institution"
AWARD_TITLE_FIELD = "Award-title"
AWARD_YEAR_FIELD = "Award-year"

APPROVED_FIELD = "✅approvedText"
APPROVED_DATE_FIELD = "statusApproved-DeclinedDate"
AVAILABLE_FIELD = "availableStatus"
CONFIRMATION_FIELD = "confirmationRequired"
VENUE_FIELD = "meetingVenueSelection"
MENTOR_SUPPORT_FIELD = "mentorMentorshipSupport(listText)"
MENTOR_STUDY_COUNTRY_FIELD = "\U0001f1f3\U0001f1ec studyCountry"
MENTOR_STUDY_PROGRAM_FIELD = "\U0001f4d3studyProgram"

# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

#: Legacy service text to the six seeded ``service_offerings`` slugs.
#:
#: Bubble held **one** option set used by both sides, stored as text at the
#: moment of selection — so these sixteen strings are snapshots taken at
#: different times and at different depths of one tree. Five are children
#: (`Statement of Purpose`, `Letter of Recommendation`, `Visa Interview`,
#: `Application Interviews`) or renames (`Document Review`, `scholarships &
#: funding guidance`). Collapsing them to the six parents is what makes a
#: mentee's need and a mentor's offer the same row; at mixed depth the join
#: returned nothing and nobody could see why.
SERVICE_OFFERINGS: dict[str, str] = {
    "test preparation": "test-preparation",
    "document preparation": "document-preparation",
    "document review": "document-preparation",
    "statement of purpose": "document-preparation",
    "letter of recommendation": "document-preparation",
    "school selection": "school-selection",
    "program selection": "program-selection",
    "scholarships & financial aid": "scholarships-financial-aid",
    "scholarships & funding guidance": "scholarships-financial-aid",
    "interview preparation": "interview-preparation",
    "application interviews": "interview-preparation",
    "visa interview": "interview-preparation",
}

#: Legacy ``degreeCategory`` to a ``degree_levels`` slug.
#:
#: The slugs are ISCED-aligned: `diploma` (4-5), `bachelors` (6), `masters` (7),
#: `doctorate` (8). `mba` and `postdoc` no longer exist — an MBA *is* a master's
#: degree, and a postdoc is a research post rather than a qualification.
DEGREE_CATEGORIES: dict[str, str] = {
    "bachelors": "bachelors",
    "masters": "masters",
    "doctorates": "doctorate",
    "diploma": "diploma",
}

#: The "Program Interest" option set to a ``degree_levels`` slug.
#:
#: Serves ``mentee_goals.degree_goal_id``. A *goal* of a bachelor's degree is
#: meaningless on a graduate-education platform, but the same list also records
#: what somebody already holds, so the bachelor's rows stay: the filter belongs
#: in the interface, not in this mapping.
#:
#: `mba (...)` maps to `masters` and every doctorate to `doctorate`, because the
#: level is the filterable dimension — "mentors with a doctorate" cannot be
#: answered if EdD and PhD sit on different rows.
PROGRAM_DEGREE_LEVELS: dict[str, str] = {
    "bsc (bachelor of science)": "bachelors",
    "ba (bachelor of art)": "bachelors",
    "llb (bachelor of law)": "bachelors",
    "beng (bachelor of engineering)": "bachelors",
    "bed (bachelor of education)": "bachelors",
    "mbbs (bachelor of medicine and bachelor of surgery)": "bachelors",
    "msc (master of science)": "masters",
    "llm (master of laws)": "masters",
    "meng (master of engineering)": "masters",
    "ma (master of arts)": "masters",
    "med (master of education)": "masters",
    "mba (master of business administration)": "masters",
    "phd (doctor of philosophy)": "doctorate",
    "edd (doctor of education)": "doctorate",
    "dba (doctor of business administration)": "doctorate",
    "dnurs (doctor of nursing)": "doctorate",
    "lld (doctor of laws)": "doctorate",
    "dsc (doctor of science)": "doctorate",
}

#: Legacy venue selection to ``meeting_provider``.
#:
#: A blank is the majority and means the mentor never chose, which is the
#: platform default rather than a missing value. ``meetingVenueLink`` is not
#: mapped at all: selecting a venue auto-created a per-session link that lived on
#: the session record, so what remains on the mentor row is residue.
MEETING_VENUES: dict[str, MeetingProvider] = {
    "edufurther video (recommended)": MeetingProvider.DAILY,
    "external video tool": MeetingProvider.CUSTOM,
    "": MeetingProvider.GOOGLE_MEET,
}

APPROVALS: dict[str, ApprovalStatus] = {
    "yes": ApprovalStatus.APPROVED,
    "no": ApprovalStatus.DECLINED,
    "": ApprovalStatus.PENDING,
}

LISTINGS: dict[str, ListingStatus] = {
    "yes": ListingStatus.LISTED,
    "no": ListingStatus.UNLISTED,
    "": ListingStatus.UNLISTED,
}

#: A blank means "never turned it on", which is `false` — see settled decision 57.
BOOLEANS: dict[str, bool] = {"yes": True, "no": False, "": False}


def _lookup[T](table: dict[str, T], raw: Any, *, field: str, bubble_id: str) -> T:
    """Map a legacy value, or raise naming both the value and where it came from."""
    key = str(raw or "").strip().casefold()
    if key not in table:
        raise TransformError(bubble_id, f"{field}: unmapped value {str(raw)!r}")
    return table[key]


def _text(record: dict[str, Any], field: str) -> str | None:
    value = str(record.get(field) or "").strip()
    return value or None


def _timestamp(
    record: dict[str, Any], field: str, *, assume: tzinfo | None, bubble_id: str
) -> datetime | None:
    raw = record.get(field)
    if not raw:
        return None
    try:
        return parse_timestamp(str(raw), assume=assume)
    except ValueError as exc:
        raise TransformError(bubble_id, f"{field}: {exc}") from exc


def export_date(record: dict[str, Any], field: str, *, zone: tzinfo, bubble_id: str) -> date | None:
    """A ``date`` column, read in the zone Bubble wrote it in.

    **``parse_timestamp`` normalises to UTC**, which is right for a
    ``timestamptz`` column and wrong here: Bubble stores these as a local
    midnight, so taking ``.date()`` off the UTC instant moves any evening value
    forward a day. ``Dec 31, 2023 11:30 pm`` in ``America/New_York`` is
    ``2024-01-01 04:30Z``, and the naive version returns **1 January**.

    Every date in the dev export is exactly ``12:00 am``, whose UTC date happens
    to match — so the whole export agrees with the wrong implementation and the
    bug is invisible to it. This was written wrong first and caught by probing a
    value the data cannot produce, which is the only thing that could have caught
    it.

    ``zone`` is required rather than optional because it is a property of the
    **Bubble application**, not of the transport: the export renders in it and
    the API reports the same instants against it. There is no correct call
    without it.

    One function for both education dates, so the rule cannot be applied to one
    and forgotten on the other.
    """
    moment = _timestamp(record, field, assume=zone, bubble_id=bubble_id)
    return moment.astimezone(zone).date() if moment else None


def service_slugs(raw: Any, *, field: str, bubble_id: str) -> tuple[str, ...]:
    """Legacy service text to distinct slugs, order preserved.

    Deduplicated because one mentor lists ``Document Review`` three times, and
    because five legacy values collapse onto two parents — so duplicates arise
    from the mapping itself, not only from the data. ``ON CONFLICT`` would absorb
    them either way; what dedupe protects is the **reported count**, and a
    reconciliation whose numbers are not true is not worth running.
    """
    seen: dict[str, None] = {}
    for value in normalise_list(raw):
        seen.setdefault(_lookup(SERVICE_OFFERINGS, value, field=field, bubble_id=bubble_id), None)
    return tuple(seen)


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EducationRow:
    legacy_bubble_id: str
    user_bubble_id: str
    created_at: datetime | None
    updated_at: datetime | None
    school_name_raw: str
    degree_category: str | None
    degree_level_slug: str | None
    degree_abbreviation: str | None
    study_course: str | None
    study_program: str | None
    date_start: date | None
    date_end: date | None
    is_most_recent: bool


@dataclass(frozen=True, slots=True)
class MenteeGoalRow:
    legacy_bubble_id: str
    user_bubble_id: str
    created_at: datetime | None
    updated_at: datetime | None
    degree_goal_slug: str | None
    degree_goal_raw: str | None
    country_names: tuple[str, ...]
    service_slugs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MentorProfileRow:
    legacy_bubble_id: str
    user_bubble_id: str
    created_at: datetime | None
    updated_at: datetime | None
    approval_status: ApprovalStatus
    approved_at: datetime | None
    listing_status: ListingStatus
    unlisted_reason: UnlistedReason | None
    requires_booking_confirmation: bool
    default_meeting_venue: MeetingProvider
    primary_study_country_name: str | None
    primary_study_program: str | None
    service_slugs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AwardRow:
    legacy_bubble_id: str
    user_bubble_id: str
    created_at: datetime | None
    updated_at: datetime | None
    institution: str
    title: str
    year: int | None
    #: Set when the source year fell outside the column's CHECK and was nulled.
    year_rejected: str | None


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------


#: Canonical spelling for every abbreviation the dev export holds, keyed on the
#: value with punctuation removed and case folded.
#:
#: **Both folds are real and they are not the same fold.** The export spells one
#: bachelor's degree as ``BSc`` (11 rows) and ``B.sc`` (2), and one master's as
#: ``M.Sc`` (2) and ``MSc`` (1) — so the key has to ignore dots *and* case, which
#: a case-insensitive match alone would not. Dotted is canonical because that is
#: what the product renders and what the legacy card showed.
#:
#: A closed table rather than a rule: nine values, and a regex that inserts a dot
#: in the right place for ``BSc`` but not for ``HND`` or ``MBBS`` is a regex
#: nobody can predict from reading it.
SHORT_FORMS: dict[str, str] = {
    "bsc": "B.Sc",
    "ba": "B.A",
    "beng": "B.Eng",
    "llb": "LL.B",
    "bcom": "B.Com",
    "bed": "B.Ed",
    "mbbs": "MBBS",
    "msc": "M.Sc",
    "ma": "M.A",
    "meng": "M.Eng",
    "med": "M.Ed",
    "mphil": "M.Phil",
    "mba": "MBA",
    "llm": "LL.M",
    "phd": "Ph.D",
    "md": "M.D",
    "jd": "J.D",
    "edd": "Ed.D",
    "dphil": "D.Phil",
    "hnd": "HND",
    "diploma": "Diploma",
    "certificate": "Certificate",
}


def _short_form(record: dict[str, Any]) -> str | None:
    """The abbreviation this user holds, spelled the one way we spell it.

    **Unlisted values are kept verbatim, not folded or dropped.** The menu on
    ``degree_levels.short_forms`` is advisory, so an abbreviation nobody
    anticipated is still the user's own credential: folding ``M.Litt`` to the
    nearest known value invents one, and dropping it loses one. That is the same
    lenient branch ``degree_goal_raw`` already takes, and for the same reason.

    Blank returns ``None``, which means *inherit the level's ``short_name``* —
    the null-means-inherit rule from D21.
    """
    raw = _text(record, SHORT_FORM_FIELD)
    if raw is None:
        return None
    key = "".join(character for character in raw if character.isalnum()).casefold()
    return SHORT_FORMS.get(key, raw)


def to_education(
    record: dict[str, Any], user_bubble_id: str, *, export_timezone: tzinfo
) -> EducationRow:
    """The only transform whose timezone is **required**, because it has dates.

    Elsewhere ``export_timezone`` is optional: it is the offset the export omits,
    and the API path supplies its own. Here it is also the lens a ``date`` is
    read through, and there is no correct answer without it — so the type says
    so, and mypy refuses the call that would have shipped the bug.
    """
    bubble_id = legacy_anchor(record)
    school = _text(record, SCHOOL_FIELD)
    if not school:
        raise TransformError(bubble_id, f"{SCHOOL_FIELD}: required, and always kept")

    category = _text(record, DEGREE_CATEGORY_FIELD)
    return EducationRow(
        legacy_bubble_id=bubble_id,
        user_bubble_id=user_bubble_id,
        created_at=_timestamp(record, CREATED_AT, assume=export_timezone, bubble_id=bubble_id),
        updated_at=_timestamp(record, MODIFIED_AT, assume=export_timezone, bubble_id=bubble_id),
        school_name_raw=school,
        degree_category=category,
        degree_level_slug=(
            _lookup(DEGREE_CATEGORIES, category, field=DEGREE_CATEGORY_FIELD, bubble_id=bubble_id)
            if category
            else None
        ),
        degree_abbreviation=_short_form(record),
        study_course=_text(record, STUDY_COURSE_FIELD),
        study_program=_text(record, STUDY_PROGRAM_FIELD),
        date_start=export_date(record, DATE_START_FIELD, zone=export_timezone, bubble_id=bubble_id),
        date_end=export_date(record, DATE_END_FIELD, zone=export_timezone, bubble_id=bubble_id),
        is_most_recent=_lookup(
            BOOLEANS, record.get(MOST_RECENT_FIELD), field=MOST_RECENT_FIELD, bubble_id=bubble_id
        ),
    )


def to_mentee_goal(
    record: dict[str, Any], user_bubble_id: str, *, export_timezone: tzinfo | None = None
) -> MenteeGoalRow:
    """``degree_goal_raw`` keeps anything the vocabulary does not cover.

    The only branch in this module that maps leniently, and deliberately: the
    package specifies a raw column for exactly this, because 720 production rows
    held "Masters", "masters", "MSc" and "Master's Degree" as free text. An
    unmapped goal is preserved rather than refused; an unmapped *service* still
    raises, because there is no raw column to catch it.
    """
    bubble_id = legacy_anchor(record)
    raw_goal = _text(record, GOAL_DEGREE_FIELD)
    slug = PROGRAM_DEGREE_LEVELS.get((raw_goal or "").strip().casefold())

    return MenteeGoalRow(
        legacy_bubble_id=bubble_id,
        user_bubble_id=user_bubble_id,
        created_at=_timestamp(record, CREATED_AT, assume=export_timezone, bubble_id=bubble_id),
        updated_at=_timestamp(record, MODIFIED_AT, assume=export_timezone, bubble_id=bubble_id),
        degree_goal_slug=slug,
        degree_goal_raw=None if slug else raw_goal,
        country_names=tuple(dict.fromkeys(normalise_list(record.get(GOAL_COUNTRIES_FIELD)))),
        service_slugs=service_slugs(
            record.get(GOAL_NEEDS_FIELD), field=GOAL_NEEDS_FIELD, bubble_id=bubble_id
        ),
    )


def to_mentor_profile(
    record: dict[str, Any],
    user_bubble_id: str,
    *,
    offered: tuple[str, ...] = (),
    export_timezone: tzinfo | None = None,
) -> MentorProfileRow:
    """``unlisted_reason`` is always set, never left to the column default.

    The default is ``never_approved``, which is right for a new signup and wrong
    for every migrated mentor: the unlisted ones in the extract are already
    approved, so they paused themselves. PR 42's migration records this as an
    obligation on the transform precisely because the column cannot express it.
    """
    bubble_id = legacy_anchor(record)
    approval = _lookup(
        APPROVALS, record.get(APPROVED_FIELD), field=APPROVED_FIELD, bubble_id=bubble_id
    )
    listing = _lookup(
        LISTINGS, record.get(AVAILABLE_FIELD), field=AVAILABLE_FIELD, bubble_id=bubble_id
    )

    unlisted_reason = None
    if listing is ListingStatus.UNLISTED:
        unlisted_reason = (
            UnlistedReason.MENTOR_PAUSED
            if approval is ApprovalStatus.APPROVED
            else UnlistedReason.NEVER_APPROVED
        )

    return MentorProfileRow(
        legacy_bubble_id=bubble_id,
        user_bubble_id=user_bubble_id,
        created_at=_timestamp(record, CREATED_AT, assume=export_timezone, bubble_id=bubble_id),
        updated_at=_timestamp(record, MODIFIED_AT, assume=export_timezone, bubble_id=bubble_id),
        approval_status=approval,
        approved_at=_timestamp(
            record, APPROVED_DATE_FIELD, assume=export_timezone, bubble_id=bubble_id
        ),
        listing_status=listing,
        unlisted_reason=unlisted_reason,
        requires_booking_confirmation=_lookup(
            BOOLEANS, record.get(CONFIRMATION_FIELD), field=CONFIRMATION_FIELD, bubble_id=bubble_id
        ),
        default_meeting_venue=_lookup(
            MEETING_VENUES, record.get(VENUE_FIELD), field=VENUE_FIELD, bubble_id=bubble_id
        ),
        primary_study_country_name=_text(record, MENTOR_STUDY_COUNTRY_FIELD),
        primary_study_program=_text(record, MENTOR_STUDY_PROGRAM_FIELD),
        service_slugs=tuple(
            dict.fromkeys(
                (
                    *service_slugs(
                        record.get(MENTOR_SUPPORT_FIELD),
                        field=MENTOR_SUPPORT_FIELD,
                        bubble_id=bubble_id,
                    ),
                    *offered,
                )
            )
        ),
    )


#: The CHECK on ``user_awards.year``. Restated rather than imported because the
#: transform is pure and the constraint lives in the schema — pinned by a test
#: that reads the constraint definition, so the two cannot drift silently.
AWARD_YEAR_FLOOR = 1950


def to_award(
    record: dict[str, Any],
    user_bubble_id: str,
    *,
    this_year: int,
    export_timezone: tzinfo | None = None,
) -> AwardRow:
    """A year outside the CHECK is **nulled and reported**, never dropped.

    The column permits a null year, so keeping the award costs nothing and loses
    nothing that matters: the institution and title still render, and the source
    value survives in the staged snapshot. Refusing the row would discard a real
    credential over one bad field, and refusing the *run* would let one typo
    block a cutover. The same shape as loading a profile without an unresolved
    country rather than dropping the profile.
    """
    bubble_id = legacy_anchor(record)
    institution = _text(record, AWARD_INSTITUTION_FIELD)
    title = _text(record, AWARD_TITLE_FIELD)
    if not institution or not title:
        raise TransformError(bubble_id, "award needs both an institution and a title")

    raw_year = _text(record, AWARD_YEAR_FIELD)
    year: int | None = None
    rejected: str | None = None
    if raw_year:
        try:
            candidate = int(raw_year)
        except ValueError:
            rejected = raw_year
        else:
            if AWARD_YEAR_FLOOR <= candidate <= this_year + 1:
                year = candidate
            else:
                rejected = raw_year

    return AwardRow(
        legacy_bubble_id=bubble_id,
        user_bubble_id=user_bubble_id,
        created_at=_timestamp(record, CREATED_AT, assume=export_timezone, bubble_id=bubble_id),
        updated_at=_timestamp(record, MODIFIED_AT, assume=export_timezone, bubble_id=bubble_id),
        institution=institution,
        title=title,
        year=year,
        year_rejected=rejected,
    )


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    """Everything one extract turns into, before any of it is written.

    Assembled whole rather than table by table because none of these rows mean
    anything without the user they hang off — the same reason ``IdentityPlan``
    is shaped this way.
    """

    education: tuple[EducationRow, ...]
    goals: tuple[MenteeGoalRow, ...]
    mentors: tuple[MentorProfileRow, ...]
    awards: tuple[AwardRow, ...]

    errors: tuple[str, ...]
    #: Thing name to the bubble ids of rows no user claims. Never silently dropped.
    unattached: dict[str, tuple[str, ...]]
    #: Rows where ``Creator`` and the user-side link name different users. The
    #: link wins; this exists so a disagreement is visible rather than absorbed.
    creator_mismatches: tuple[str, ...]
    #: ``"<bubble id>: <value>"`` for award years outside the column's CHECK.
    #: The award still loads with a null year.
    rejected_award_years: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def country_names(self) -> set[str]:
        names = {name for goal in self.goals for name in goal.country_names}
        return names | {
            m.primary_study_country_name for m in self.mentors if m.primary_study_country_name
        }

    def service_slugs(self) -> set[str]:
        return {slug for goal in self.goals for slug in goal.service_slugs} | {
            slug for mentor in self.mentors for slug in mentor.service_slugs
        }

    def degree_slugs(self) -> set[str]:
        slugs = {row.degree_level_slug for row in self.education if row.degree_level_slug}
        return slugs | {goal.degree_goal_slug for goal in self.goals if goal.degree_goal_slug}


def _links(user_records: list[dict[str, Any]], field: str) -> dict[str, str]:
    """Thing bubble id to the bubble id of the user pointing at it.

    The join direction that matters. ``normalise_list`` because the export joins
    a list with commas while the API returns an array, and neither adapter's
    shape should reach a mapping decision.
    """
    owner: dict[str, str] = {}
    for record in user_records:
        user_id = legacy_anchor(record)
        for thing_id in normalise_list(record.get(field)):
            owner[thing_id] = user_id
    return owner


def plan_profiles(
    user_records: list[dict[str, Any]],
    *,
    education_records: list[dict[str, Any]],
    goal_records: list[dict[str, Any]],
    service_records: list[dict[str, Any]],
    mentor_records: list[dict[str, Any]],
    award_records: list[dict[str, Any]],
    export_timezone: tzinfo,
    this_year: int,
) -> ProfilePlan:
    """Attribute every profile row to a user, and report what cannot be.

    **Four of the five Things are joined from the user side**, following
    ``plan_identity``: a row nobody points at is reported, never attributed by
    inference. ``Creator`` now carries a Bubble user id rather than an email, so
    it serves as an exact cross-check — where it disagrees with the link, the
    link wins and the disagreement is reported.

    **Awards are the exception, and the only one.** ``Scholarship-Awards`` has no
    user-side link in either direction, so ``Creator`` is the sole path. That is
    attribution by *who made the row*, which is not the same claim as *whose row
    it is* — an admin creating an award on someone's behalf would be attributed
    to the admin. An id makes the join exact; it does not make the assumption
    true, and no amount of care here can.
    """
    known = {str(r.get("bubble_id") or r.get("unique id") or "") for r in user_records}

    errors: list[str] = []
    unattached: dict[str, tuple[str, ...]] = {}
    mismatches: list[str] = []

    def owner_of(record: dict[str, Any], linked: dict[str, str]) -> str | None:
        """The user-side link, with ``Creator`` checked against it."""
        thing_id = legacy_anchor(record)
        creator = str(record.get(CREATOR_FIELD) or "").strip()
        user_id = linked.get(thing_id)
        if user_id is None:
            return None
        if creator and creator != user_id:
            mismatches.append(f"{thing_id}: linked to {user_id}, created by {creator}")
        return user_id

    def collect[R](
        thing: str,
        records: list[dict[str, Any]],
        linked: dict[str, str],
        build: Callable[[dict[str, Any], str], R],
    ) -> list[R]:
        rows: list[R] = []
        orphans: list[str] = []
        for record in records:
            user_id = owner_of(record, linked)
            if user_id is None:
                orphans.append(legacy_anchor(record))
                continue
            try:
                rows.append(build(record, user_id))
            except TransformError as exc:
                errors.append(str(exc))
        if orphans:
            unattached[thing] = tuple(sorted(orphans))
        return rows

    education = collect(
        "education",
        education_records,
        _links(user_records, EDUCATION_LINK_FIELD),
        lambda r, u: to_education(r, u, export_timezone=export_timezone),
    )
    goals = collect(
        "mentee_goals",
        goal_records,
        _links(user_records, GOAL_LINK_FIELD),
        lambda r, u: to_mentee_goal(r, u, export_timezone=export_timezone),
    )

    # A mentor's offerings live on two Things that hold the same list — the
    # `Mentor Services` row and the front-search row's own copy. They agree
    # wherever both exist, and the front-search copy is populated on rows the
    # linked Thing is empty for, so the union is strictly more complete than
    # either. `to_mentor_profile` deduplicates.
    service_owner = _links(user_records, SERVICE_LINK_FIELD)
    offered: dict[str, tuple[str, ...]] = {}
    service_orphans: list[str] = []
    for record in service_records:
        user_id = owner_of(record, service_owner)
        if user_id is None:
            service_orphans.append(legacy_anchor(record))
            continue
        try:
            offered[user_id] = service_slugs(
                record.get(MENTOR_SERVICES_FIELD),
                field=MENTOR_SERVICES_FIELD,
                bubble_id=legacy_anchor(record),
            )
        except TransformError as exc:
            errors.append(str(exc))
    if service_orphans:
        unattached["mentor_services"] = tuple(sorted(service_orphans))

    mentor_owner = _links(user_records, MENTOR_LINK_FIELD)
    mentors = collect(
        "mentor_profiles",
        mentor_records,
        mentor_owner,
        lambda r, u: to_mentor_profile(
            r, u, offered=offered.get(u, ()), export_timezone=export_timezone
        ),
    )

    # Awards: `Creator` is the only path. Validated against the known user set
    # rather than trusted, so an award created by somebody outside this extract
    # is reported instead of raising a foreign-key error deep in the loader.
    awards: list[AwardRow] = []
    award_orphans: list[str] = []
    for record in award_records:
        creator = str(record.get(CREATOR_FIELD) or "").strip()
        if creator not in known:
            award_orphans.append(legacy_anchor(record))
            continue
        try:
            awards.append(
                to_award(record, creator, this_year=this_year, export_timezone=export_timezone)
            )
        except TransformError as exc:
            errors.append(str(exc))
    if award_orphans:
        unattached["user_awards"] = tuple(sorted(award_orphans))

    return ProfilePlan(
        education=tuple(education),
        goals=tuple(goals),
        mentors=tuple(mentors),
        awards=tuple(awards),
        errors=tuple(errors),
        unattached=unattached,
        creator_mismatches=tuple(sorted(mismatches)),
        rejected_award_years=tuple(
            sorted(f"{a.legacy_bubble_id}: {a.year_rejected}" for a in awards if a.year_rejected)
        ),
    )
