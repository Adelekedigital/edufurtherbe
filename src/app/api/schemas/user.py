"""What a user looks like over the wire."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.api.schemas.common import AvatarFocusRead, LinkedInRead, XRead, YouTubeRead
from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.domain.emails import normalise_email
from app.domain.enums import CoverArt, CoverColor, CreditState, PrimaryRole


class NormalisedEmail(BaseModel):
    """Mixin for any model carrying an email.

    **Normalisation is declarative, not per-handler.** Lowercasing in each route
    is the version that works until somebody adds a route and forgets, and the
    failure is a second account nobody can find. Here it happens before the
    handler is entered, on every model that inherits it.
    """

    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalise(cls, value: str) -> str:
        return normalise_email(value)


class UserProfileRead(BaseModel):
    """The profile fields a user may see about themselves."""

    model_config = ConfigDict(from_attributes=True)

    about_me: str | None = None
    gender: str | None = None
    avatar_url: str | None = None
    #: Where to centre `avatar_url`; `null` means the client's default crop.
    avatar_focus: AvatarFocusRead | None = None
    banner_url: str | None = None
    #: The cover when there is no banner (#193). `null` colour means automatic.
    cover_color: CoverColor | None = None
    cover_art: CoverArt = CoverArt.NONE
    #: Canonical `https://` links or `null` — render them, never parse them (#182).
    social_linkedin: LinkedInRead = None
    social_twitter: XRead = None
    social_youtube: YouTubeRead = None


class CreditsRead(BaseModel):
    """The dashboard card's credit block.

    **Four fields, and no percentage.** The card draws a bar whose filled
    position is the balance; the server publishes the two numbers and the band,
    and the client draws. A percentage computed here would be a third
    representation of the same fact.

    ``allowance`` is ``max(ladder.steady_state, balance)`` rather than the
    allowance alone — a refund landing after the monthly grant leaves a balance
    above it, and the card would otherwise read "4 credits left" beside a bar
    with three positions.

    ``state`` is a name, never a colour or a sentence: the copy and the palette
    belong to the front end, which knows the viewer's language.
    """

    model_config = ConfigDict(from_attributes=True)

    balance: int
    allowance: int
    state: CreditState
    #: The 1st of the next month, midnight UTC — **exclusive**. A lot granted in
    #: August survives all of 31 August and dies as September opens, which is
    #: what makes the card's "Next reset date" literally true.
    next_reset_at: datetime


class MentorBookingCounts(BaseModel):
    """The caller's bookings as a mentor."""

    #: Requests waiting on their accept or decline, before the deadline.
    awaiting_your_response: int
    #: Confirmed sessions that have not started yet.
    upcoming: int


class MenteeBookingCounts(BaseModel):
    """The caller's bookings as a mentee."""

    #: Their own requests still waiting on the mentor's answer.
    awaiting_mentor: int
    #: Confirmed sessions that have not started yet.
    upcoming: int


class BookingCountsRead(BaseModel):
    """Both roles' counts. `as_mentor` is null without a mentor profile;
    `as_mentee` is always filled on `/me`, since anyone signed in may book."""

    as_mentor: MentorBookingCounts | None = None
    as_mentee: MenteeBookingCounts | None = None

    @classmethod
    def of(cls, counts: dict[str, int], *, mentor: bool) -> BookingCountsRead:
        return cls(
            as_mentor=(
                MentorBookingCounts(
                    awaiting_your_response=counts["mentor_awaiting"],
                    upcoming=counts["mentor_upcoming"],
                )
                if mentor
                else None
            ),
            as_mentee=MenteeBookingCounts(
                awaiting_mentor=counts["mentee_awaiting"], upcoming=counts["mentee_upcoming"]
            ),
        )


class UserRead(NormalisedEmail):
    """A user as returned to themselves.

    ``primary_role`` is included because the client needs it to pick a
    dashboard — which is the *only* thing it is for. It is not an authorization
    claim, and a client treating it as one would be wrong in the same way a
    server would be.

    ``auth_id`` and ``legacy_bubble_id`` are deliberately absent. One is a vendor
    identifier and the other a migration anchor; neither is anybody's business
    outside this service.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    first_name: str | None = None
    last_name: str | None = None
    slug: str | None = None
    primary_role: PrimaryRole
    timezone: str
    email_verified_at: datetime | None = None
    created_at: datetime
    profile: UserProfileRead | None = None
    is_admin: bool = False

    # The attributes a profile page renders, embedded so one call is enough.
    #
    # **Additive only.** Every field above keeps its name and meaning; a client
    # built against the previous shape is unaffected. These are the *same*
    # models the `/users/{id}/…` routes return, built by the same store
    # functions — `test_me_and_the_sub_resource_agree` fails if the two ever
    # diverge, which is the whole reason both exist.
    education: list[EducationRead] = []
    #: At most one — `mentee_goals` is 1:1 with the user.
    goal: GoalRead | None = None
    awards: list[AwardRead] = []
    #: Null for the great majority of users, who are not mentors.
    mentor_profile: MentorProfileRead | None = None

    #: **Null unless the caller has a mentee goal**, which is the same predicate
    #: the monthly grant uses. Deliberately not "is not a mentor": authorization
    #: here is profile existence, so a dual-role user is both a mentor and a
    #: mentee, and a negative predicate would hide the card from somebody who
    #: can book.
    credits: CreditsRead | None = None

    #: Sessions the caller has **received** as a mentee with status `completed`.
    #: **Never null** — zero is a real answer, as on the discovery card. Not the
    #: card's `completed_sessions`, which counts sessions a mentor *gave*; a
    #: dual-role user has both and they are different numbers.
    mentee_completed_sessions: int = 0

    #: The caller's booking counts, per role — the sidebar's Bookings badge and
    #: dashboard headings. Each half is null when the caller lacks that role.
    booking_counts: BookingCountsRead = Field(default_factory=lambda: BookingCountsRead())
