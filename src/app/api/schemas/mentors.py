"""A mentor as a stranger sees them.

**An allowlist, not the owner's shape minus a few fields.** `MentorProfileRead`
exists for the mentor themselves and answers *why am I not showing up* —
`approval_status`, `listing_status`, and the booking settings it reads off their
primary offering. None of that is a stranger's business, and building this by
subtraction is how one of them survives a refactor.

`custom_meeting_url` used to be named here as the sharpest exclusion — a static
room link is a bearer credential anyone holding it can walk into. D88's contract
step deleted that column rather than moving it, so the exclusion is now
structural. If booking gives `MeetingProvider.CUSTOM` a URL somewhere, it belongs
back on this list.

Never here, from `users`: `email`, `email_verified_at`, `auth_id`,
`last_active_at`, `legacy_bubble_id`. From `user_profiles`:
`gender`, which is sensitive and has no stated product need; adding it later is
additive, removing it would not be.

**Countries are names, not foreign keys.** Returning a `countries.id` would
reproduce the gap the party identity change closed one pull request earlier: a
response that is correct and unusable without a second call this API does not
offer.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.common import AvatarFocusRead, LinkedInRead, Page, XRead, YouTubeRead
from app.api.schemas.reviews import ReviewSummaryRead
from app.api.schemas.session_types import SessionTypeRead
from app.domain.enums import ApprovalStatus, AwardFunding, CoverArt, CoverColor, ListingStatus


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
    a card for — and because under the `offering` filter, a row that cannot say
    *why* it matched is a bad card. It is at most six short rows.

    Names are nullable for the same reason they are everywhere else: the columns
    are, and the M2 transform maps them from optional Bubble fields.
    """

    id: str
    slug: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    avatar_url: str | None = None
    #: Where a card should centre `avatar_url`; `null` means use the default.
    avatar_focus: AvatarFocusRead | None = None
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

    #: How many published reviews this mentor has. **Never null**, for the
    #: same reason as the count above.
    review_count: int = 0
    #: The session value, `1..5`, rendered `X/5` — the rule is on
    #: `ReviewSummaryRead.session_value`. **Null** when nobody
    #: has reviewed them — a ratio over no rows is unknown, where zero would
    #: read as *rated badly*.
    session_value: float | None = None

    offerings: list[ServiceOfferingRead] = Field(default_factory=list)

    #: The first instant this mentor could be booked, within the booking horizon.
    #: **Non-null only when `next_available_state` is `open`.** Stored and
    #: refreshed by a job (ADR 0029); booking always reads live slots.
    next_available_at: datetime | None = None
    #: What a null `next_available_at` means. `none`: nothing free in the
    #: booking horizon. `refreshing`: not recomputed since a booking, an hours
    #: change or the mentor becoming bookable — unknown, not empty.
    next_available_state: Literal["open", "none", "refreshing"] = "refreshing"
    #: The offering `next_available_at` belongs to — open booking on it. Null
    #: whenever the time is (settled decision #189).
    next_available_session_type_id: UUID | None = None

    #: When this person became a mentor — `mentor_profiles.created_at`, which
    #: for a migrated mentor is their creation date on the legacy platform. Not
    #: the approval date: migrated mentors carry no approval event to read.
    joined_at: datetime
    #: The title of their most recent scholarship or award — exactly the one the
    #: profile's `scholarships` list leads with. `null` when they list none.
    top_award: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MentorSummaryRead:
        return cls(
            id=str(row["user_id"]),
            slug=_text(row["slug"]),
            first_name=_text(row["first_name"]),
            last_name=_text(row["last_name"]),
            headline=_text(row["headline"]),
            avatar_url=_text(row["avatar_url"]),
            avatar_focus=AvatarFocusRead.of(row["avatar_focus_x"], row["avatar_focus_y"]),
            primary_study_country=_text(row["primary_study_country"]),
            origin_country=_text(row["origin_country"]),
            degree=_text(row["degree"]),
            study_course=_text(row["study_course"]),
            institution=_text(row["institution"]),
            completed_sessions=int(str(row["completed_sessions"] or 0)),
            review_count=int(str(row["review_count"] or 0)),
            session_value=(
                None if row["session_value"] is None else float(str(row["session_value"]))
            ),
            offerings=[ServiceOfferingRead(**o) for o in row["offerings"]],
            **_next_available(row),
            **_joined_and_award(row),
        )


class FeaturedMentorRead(MentorSummaryRead):
    """The week's featured mentor: their discovery card, and their bio.

    **The card plus one field, not a new shape.** The frontend renders it with
    the same component as the list, so it must carry the same fields from the
    same query (`mentor_card`). `about_me` is the full text; the design clamps
    it to two lines, which is a layout decision the client owns.
    """

    about_me: str | None = None

    @classmethod
    def from_featured(cls, row: dict[str, Any]) -> FeaturedMentorRead:
        return cls(**MentorSummaryRead.from_row(row).model_dump(), about_me=_text(row["about_me"]))


class SimilarMentorRead(MentorSummaryRead):
    """A discovery card, and the offering that makes this mentor similar.

    **The card plus one field**, like `FeaturedMentorRead`: the frontend renders
    it with the list's component, so it carries the list's fields from the
    list's query.
    """

    shared_offering: ServiceOfferingRead = Field(
        description=(
            "The kind of help this mentor shares with the profile being viewed — "
            "the first shared one in the platform's order. Always one of this "
            "card's `offerings`."
        )
    )

    @classmethod
    def from_similar(cls, row: dict[str, Any]) -> SimilarMentorRead:
        return cls(
            **MentorSummaryRead.from_row(row).model_dump(),
            shared_offering=ServiceOfferingRead(**row["shared_offering"]),
        )


class MentorPage(Page[MentorSummaryRead]):
    """A discovery page, and on the first page, how many mentors there are.

    **A subclass rather than a field on `Page`.** Every other list pages without
    a count, and a nullable `total` on the shared envelope would promise one to
    clients of endpoints that will never send it.
    """

    total: int | None = Field(
        default=None,
        description=(
            "How many mentors this request lists across every page — the same "
            "`q` and `offering`. Sent on the **first page only** (no `cursor`); "
            "`null` on later pages, so carry the first page's value forward. "
            "`0` is a real answer."
        ),
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
    funding: AwardFunding | None = Field(
        default=None,
        description=(
            "`full` or `partial`, as the holder says. **Null when the mentor hasn't said**, "
            'which is most awards: show "fully funded" only when this is `full`.'
        ),
    )

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
            funding=AwardFunding(str(row["funding"])) if row.get("funding") else None,
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
    #: Where a card should centre `avatar_url`; `null` means use the default.
    avatar_focus: AvatarFocusRead | None = None
    banner_url: str | None = None
    cover_color: CoverColor | None = Field(
        default=None,
        description=(
            "The cover colour when there is no `banner_url` (which wins when set). "
            "`null` means automatic: hash the mentor's id over `CoverColor`'s "
            "values, in their published order."
        ),
    )
    cover_art: CoverArt = Field(
        default=CoverArt.NONE, description="What is drawn over the cover colour."
    )
    primary_study_program: str | None = None
    primary_study_country: str | None = Field(
        default=None, description="Where they studied, resolved to a name."
    )
    origin_country: str | None = None
    #: Canonical `https://` links or `null` — render them, never parse them (#182).
    social_linkedin: LinkedInRead = None
    social_twitter: XRead = None
    social_youtube: YouTubeRead = None
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

    #: What this mentor's reviews add up to. Derived every request, like
    #: the figures above — D56 bans a stored average as firmly as a stored
    #: count, and for a sharper reason: an average is a property of the
    #: *set*, so any sibling added or withdrawn invalidates a cached one.
    reviews: ReviewSummaryRead = Field(default_factory=ReviewSummaryRead)

    #: When this person became a mentor — `mentor_profiles.created_at`, which
    #: for a migrated mentor is their creation date on the legacy platform. Not
    #: the approval date: migrated mentors carry no approval event to read.
    joined_at: datetime
    #: The title of their most recent scholarship or award — exactly the one the
    #: profile's `scholarships` list leads with. `null` when they list none.
    top_award: str | None = None

    languages: list[LanguageRead] = Field(
        default_factory=list,
        description=(
            "Languages spoken, alphabetically. Empty for most migrated users — "
            "the legacy export carries one for 3 profiles in 19."
        ),
    )

    #: The same pair, with the same meaning, as the discovery card's — read from
    #: the same stored table (ADR 0029) and gated by the same helper. A profile
    #: the public cannot see has nothing bookable, so its owner reads `none`.
    next_available_at: datetime | None = None
    next_available_state: Literal["open", "none", "refreshing"] = "refreshing"
    #: The offering `next_available_at` belongs to — open booking on it. Null
    #: whenever the time is (settled decision #189).
    next_available_session_type_id: UUID | None = None

    # `exclude_if`, not a model serializer: a wrap serializer typed `dict`
    # replaces this model's whole serialization schema, and the published
    # OpenAPI loses every property of the profile.
    approval_status: ApprovalStatus | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "**Only in the mentor's own view of their profile, and absent for "
            "everyone else** — its presence means the caller is this mentor. "
            "Sent with a bearer token, the owner reads their profile in any "
            "state, including before approval."
        ),
    )
    listing_status: ListingStatus | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Owner only, like `approval_status`. `unlisted` means hidden from search.",
    )
    setup_needed: list[Literal["session_type", "weekly_hours"]] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "**Owner only**, like `approval_status`. What stops this profile being "
            "live: `session_type` (no active offering) and/or `weekly_hours` (no "
            "weekly availability). Empty when nothing is missing. While it is not "
            "empty, strangers get a `404` for this profile and it is not on "
            "Explore; it goes live by itself as soon as both exist (#192)."
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
        reviews: dict[str, Any],
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
            avatar_focus=AvatarFocusRead.of(row["avatar_focus_x"], row["avatar_focus_y"]),
            banner_url=_text(row["banner_url"]),
            cover_color=(
                CoverColor(str(row["cover_color"])) if row["cover_color"] is not None else None
            ),
            cover_art=CoverArt(str(row["cover_art"])),
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
            reviews=ReviewSummaryRead.from_row(reviews),
            **_joined_and_award(row),
            **_next_available(row),
            **_owner_fields(row),
        )


def _next_available(row: dict[str, Any]) -> dict[str, Any]:
    """`next_available_at` and `next_available_state`, for a card or a profile.

    **The stored time is only a claim while the state says `open`.** The state
    is computed once in SQL; this is the one place it gates the time — shared by
    the discovery card and the profile, so the two cannot disagree about when a
    mentor is free.
    """
    state = row["next_available_state"]
    vouched = state == "open"
    session_type = row.get("next_available_session_type_id")
    return {
        "next_available_at": row["next_available_at"] if vouched else None,
        # Gated with the time, never apart from it: a change to any offering
        # logs a change and the state leaves `open`, so an id sent here is one
        # of the offerings the mentor still has live.
        "next_available_session_type_id": session_type if vouched and session_type else None,
        "next_available_state": state,
    }


#: Published to the mentor reading their own profile, and absent — not null —
#: for everybody else, so their presence alone means "this is you".
OWNER_ONLY = ("approval_status", "listing_status")


def _owner_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The owner-only fields, when the row says the caller is the owner."""
    if not row.get("is_owner"):
        return {}
    missing = [
        need
        for need, present in (
            ("session_type", row["has_offering"]),
            ("weekly_hours", row["has_hours"]),
        )
        if not present
    ]
    return {**{key: row[key] for key in OWNER_ONLY}, "setup_needed": missing}


def _joined_and_award(row: dict[str, Any]) -> dict[str, Any]:
    """`joined_at` and `top_award`, mapped once for the card and the profile."""
    return {"joined_at": row["joined_at"], "top_award": _text(row["top_award"])}


def _text(value: object) -> str | None:
    return str(value) if value is not None else None
