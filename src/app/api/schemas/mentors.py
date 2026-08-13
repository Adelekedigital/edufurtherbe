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
    years_of_experience: int | None = None
    about_me: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    primary_study_program: str | None = None
    primary_study_country: str | None = Field(
        default=None, description="Where they studied, resolved to a name."
    )
    origin_country: str | None = None
    current_country: str | None = None
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

    @classmethod
    def from_row(
        cls,
        row: dict[str, object],
        offerings: list[dict[str, object]],
        session_types: list[dict[str, object]],
    ) -> MentorPublicRead:
        return cls(
            id=str(row["user_id"]),
            slug=_text(row["slug"]),
            first_name=_text(row["first_name"]),
            last_name=_text(row["last_name"]),
            timezone=str(row["timezone"]),
            headline=_text(row["headline"]),
            years_of_experience=(
                int(str(row["years_of_experience"]))
                if row["years_of_experience"] is not None
                else None
            ),
            about_me=_text(row["about_me"]),
            avatar_url=_text(row["avatar_url"]),
            banner_url=_text(row["banner_url"]),
            primary_study_program=_text(row["primary_study_program"]),
            primary_study_country=_text(row["primary_study_country"]),
            origin_country=_text(row["origin_country"]),
            current_country=_text(row["current_country"]),
            social_linkedin=_text(row["social_linkedin"]),
            social_twitter=_text(row["social_twitter"]),
            social_youtube=_text(row["social_youtube"]),
            offerings=[ServiceOfferingRead(**o) for o in offerings],  # type: ignore[arg-type]
            session_types=[SessionTypeRead.from_row(s) for s in session_types],
        )


def _text(value: object) -> str | None:
    return str(value) if value is not None else None
