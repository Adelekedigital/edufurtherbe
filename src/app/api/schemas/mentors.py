"""A mentor as a stranger sees them.

**An allowlist, not the owner's shape minus a few fields.** `MentorProfileRead`
exists for the mentor themselves and answers *why am I not showing up* —
`approval_status`, `listing_status`, `requires_booking_confirmation`. None of
that is a stranger's business, and building this by subtraction is how one of
them survives a refactor.

Never here, from `users`: `email`, `email_verified_at`, `auth_id`,
`last_active_at`, `legacy_bubble_id`. From `mentor_profiles`:
`custom_meeting_url`, which is a **static room link** — a bearer credential
anyone holding it can walk into, not a description. From `user_profiles`:
`gender`, which is sensitive and has no stated product need; adding it later is
additive, removing it would not be.

**Countries are names, not foreign keys.** Returning a `countries.id` would
reproduce the gap the party identity change closed one pull request earlier: a
response that is correct and unusable without a second call this API does not
offer.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.session_types import SessionTypeRead


class ServiceOfferingRead(BaseModel):
    """One kind of help this mentor gives.

    The closed six-row taxonomy from settled decision #53 — **not** a session
    type. This is *what kind of help*; a session type is the bookable product
    with a duration and a notice window. Both words are in the domain vocabulary
    because they are close enough to be swapped by accident.
    """

    slug: str
    display_name: str


class MentorSummaryRead(BaseModel):
    """One mentor as a search result — a card, not a profile.

    **Deliberately smaller than `MentorPublicRead`.** Twenty of these render a
    results page; twenty full profiles with their inlined session types would be
    payload for cards nobody has clicked. What is missing here is on
    `/mentors/{handle}`, one click away.

    `offerings` stays because it is the matching axis — the thing a mentee scans
    a card for — and because when the service filter arrives, a row that cannot
    say *why* it matched is a bad card. It is at most six short rows.

    Names are nullable for the same reason they are everywhere else: the columns
    are, and the M2 transform maps them from optional Bubble fields.
    """

    id: str
    slug: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    avatar_url: str | None = None
    primary_study_country: str | None = None
    #: Where the mentor is *from*. The search document has always indexed it,
    #: so a mentee could find a mentor by a fact the card could not show them.
    origin_country: str | None = None

    #: The academic line: "Ph.D, Mathematics, Washington University". Three
    #: nullable fields rather than one rendered string, because a card lays them
    #: out and a server-side join would fix the punctuation and the order for
    #: every client forever.
    #:
    #: `degree` is the user's own abbreviation where they have one and the
    #: level's generic name where they do not — never a guessed specific form,
    #: which would render "B.Sc" for a law graduate.
    degree: str | None = None
    study_course: str | None = None
    institution: str | None = None

    #: Sessions delivered. **Never null** — zero is a real answer, and a nullable
    #: count makes every client write the same coalesce while leaving "no data"
    #: and "none yet" indistinguishable on the card.
    completed_sessions: int = 0

    offerings: list[ServiceOfferingRead] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MentorSummaryRead:
        return cls(
            id=str(row["user_id"]),
            slug=_text(row["slug"]),
            first_name=_text(row["first_name"]),
            last_name=_text(row["last_name"]),
            headline=_text(row["headline"]),
            avatar_url=_text(row["avatar_url"]),
            primary_study_country=_text(row["primary_study_country"]),
            origin_country=_text(row["origin_country"]),
            degree=_text(row["degree"]),
            study_course=_text(row["study_course"]),
            institution=_text(row["institution"]),
            completed_sessions=int(str(row["completed_sessions"] or 0)),
            offerings=[ServiceOfferingRead(**o) for o in row["offerings"]],
        )


class EducationRead(BaseModel):
    """One degree, as a public profile shows it.

    **An allowlist, and four of the columns the owner-facing query returns are
    deliberately absent.** `is_most_recent` is blank on every migrated row and
    means nothing (D98); `degree_category` is the raw legacy value the field
    mapping marks *migrate, then deprecate*; `study_program` holds degree names
    rather than subjects and is 8-of-21 populated; `degree_level_slug` is a code
    for filtering, not for rendering.

    `degree` resolves the same way the discovery card resolves it — the user's own
    abbreviation, else the level's generic name — because a profile page showing
    "Doctorate (PhD)" beside a card showing "Ph.D" is one product with two
    spellings of one fact.
    """

    id: str
    degree: str | None = None
    study_course: str | None = None
    institution: str | None = None
    #: Both nullable, and both rendered as a range. An entry with neither is
    #: still an entry — the design omits the dates rather than the row.
    date_start: date | None = None
    date_end: date | None = None

    @classmethod
    def from_row(cls, row: dict[str, object]) -> EducationRead:
        return cls(
            id=str(row["id"]),
            degree=_text(row["degree_abbreviation"]) or _text(row["degree_level_short_name"]),
            study_course=_text(row["study_course"]),
            institution=_text(row["institution_name"]) or _text(row["school_name_raw"]),
            date_start=row["date_start"],  # type: ignore[arg-type]
            date_end=row["date_end"],  # type: ignore[arg-type]
        )


class AwardRead(BaseModel):
    """One scholarship or award.

    **`evidence_url` and `verification_status` are excluded, and the first is the
    one that matters.** `evidence_url` is a link to the holder's proof document —
    a private artefact on an endpoint that takes no token. The owner-facing list
    returns both and must keep doing so; this is a narrower view of the same
    query, not a second query.

    `verification_status` is `unverified` on every row because nothing verifies
    an award yet. Publishing it would put "unverified" against every credential
    on the platform, which says something untrue about the holder rather than
    something true about the system.
    """

    id: str
    title: str
    institution: str
    year: int | None = None

    @classmethod
    def from_row(cls, row: dict[str, object]) -> AwardRead:
        return cls(
            id=str(row["id"]),
            # The catalogue name when the award is linked to one, else what the
            # holder typed. `scholarship_program_id` is absent from the export, so
            # `programme_name` is null on all ten migrated rows today and the
            # fallback is the whole behaviour — the same raw-plus-resolved pair as
            # `institution_name`/`school_name_raw` above.
            title=str(_text(row.get("programme_name")) or row["title"]),
            institution=str(row["institution"]),
            year=int(str(row["year"])) if row["year"] is not None else None,
        )


class LanguageRead(BaseModel):
    """One language a user speaks.

    No `proficiency`: the column is `NOT NULL` with a `'fluent'` default that the
    ETL never overrides, so every migrated row claims a fluency nobody asked
    about. It becomes returnable when a write path collects it.
    """

    id: str
    display_name: str
    code: str

    @classmethod
    def from_row(cls, row: dict[str, object]) -> LanguageRead:
        return cls(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            # `char(3)`, so it arrives space-padded from PostgreSQL.
            code=str(row["code"]).strip(),
        )


class MentorPublicRead(BaseModel):
    """Everything the public may read about one mentor."""

    id: str
    slug: str | None = Field(
        default=None,
        description=(
            "The legacy public profile handle. Nullable — 4 of 43 migrated users "
            "have none — and either this or the id addresses this endpoint."
        ),
    )
    first_name: str | None = None
    last_name: str | None = None
    timezone: str = Field(
        description=(
            "The mentor's IANA zone. Returned so a client can show *their* local "
            "time beside a slot, which is the one thing a UTC instant cannot say."
        )
    )
    headline: str | None = None
    about_me: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    primary_study_program: str | None = None
    primary_study_country: str | None = Field(
        default=None, description="Where they studied, resolved to a name."
    )
    origin_country: str | None = None
    social_linkedin: str | None = None
    social_twitter: str | None = None
    social_youtube: str | None = None
    offerings: list[ServiceOfferingRead] = Field(
        default_factory=list, description="What kind of help they give."
    )
    session_types: list[SessionTypeRead] = Field(
        default_factory=list,
        description=(
            "What can actually be booked, and the duration and notice governing "
            "each. Inlined because a profile page needs both and a second round "
            "trip for a handful of rows is waste — but read by the **same** "
            "function that serves `/users/{id}/session-types`, so the two "
            "endpoints cannot disagree."
        ),
    )
    education: list[EducationRead] = Field(
        default_factory=list, description="Degrees, most recent first."
    )
    scholarships: list[AwardRead] = Field(
        default_factory=list, description="Scholarships and awards, newest first."
    )
    #: What this mentor has actually done. Derived every request (D56) — no
    #: stored totals, no counters. `completed_sessions` is the **same number** the
    #: discovery card shows, from the same predicate, because two definitions of
    #: "delivered" is the defect #8 describes.
    completed_sessions: int = 0
    #: Scheduled duration summed over completed sessions, not measured time:
    #: Daily could supply real minutes and Google Meet cannot, and one number
    #: with two definitions by venue is worse than one honest definition.
    mentoring_minutes: int = 0
    mentees_mentored: int = 0
    #: Whole-number percentage, or **null when nothing is known** — zero would
    #: say "never shows up" where null says "no data yet".
    attendance_rate: int | None = None

    languages: list[LanguageRead] = Field(
        default_factory=list,
        description=(
            "Languages spoken, alphabetically. Empty for most migrated users — "
            "the legacy export carries one for 3 profiles in 19."
        ),
    )

    @classmethod
    def from_row(
        cls,
        row: dict[str, object],
        offerings: list[dict[str, object]],
        session_types: list[dict[str, object]],
        education: list[dict[str, object]],
        scholarships: list[dict[str, object]],
        languages: list[dict[str, object]],
        stats: dict[str, object],
    ) -> MentorPublicRead:
        return cls(
            id=str(row["user_id"]),
            slug=_text(row["slug"]),
            first_name=_text(row["first_name"]),
            last_name=_text(row["last_name"]),
            timezone=str(row["timezone"]),
            headline=_text(row["headline"]),
            about_me=_text(row["about_me"]),
            avatar_url=_text(row["avatar_url"]),
            banner_url=_text(row["banner_url"]),
            primary_study_program=_text(row["primary_study_program"]),
            primary_study_country=_text(row["primary_study_country"]),
            origin_country=_text(row["origin_country"]),
            social_linkedin=_text(row["social_linkedin"]),
            social_twitter=_text(row["social_twitter"]),
            social_youtube=_text(row["social_youtube"]),
            offerings=[ServiceOfferingRead(**o) for o in offerings],  # type: ignore[arg-type]
            session_types=[SessionTypeRead.from_row(s) for s in session_types],
            education=[EducationRead.from_row(e) for e in education],
            scholarships=[AwardRead.from_row(a) for a in scholarships],
            languages=[LanguageRead.from_row(x) for x in languages],
            completed_sessions=int(str(stats["completed_sessions"])),
            mentoring_minutes=int(str(stats["mentoring_minutes"])),
            mentees_mentored=int(str(stats["mentees_mentored"])),
            attendance_rate=(
                int(str(stats["attendance_rate"])) if stats["attendance_rate"] is not None else None
            ),
        )


def _text(value: object) -> str | None:
    return str(value) if value is not None else None
