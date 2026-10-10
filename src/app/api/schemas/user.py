"""What a user looks like over the wire."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.schemas.common import AvatarFocusRead, LinkedInRead, StoredEmail, XRead, YouTubeRead
from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.domain.emails import normalise_email
from app.domain.enums import CoverArt, CoverColor, CreditState, PrimaryRole


class NormalisedEmail(BaseModel):
    """Mixin for any model carrying an email.

    **Normalisation is declarative, not per-handler.** Lowercasing in each route
    is the version that works until somebody adds a route and forgets, and the
    failure is a second account nobody can find. Here it happens before the
    handler is entered, on every model that inherits it.

    **Its one user is a response** (`UserRead`), so the field is
    :data:`StoredEmail`, not `EmailStr`: a stored address is reported as stored,
    never re-validated into a 500 (#321).
    """

    email: StoredEmail

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


class MonthlyCreditsRead(BaseModel):
    """The monthly grant's part of the balance. The card's bar draws this."""

    model_config = ConfigDict(from_attributes=True)

    #: Monthly credits held now: the 1st-of-month grant, a migrated opening
    #: balance, and refunds of either.
    balance: int
    #: The monthly grant. Draw the bar as `balance` of `ceiling`, clamped: a late
    #: refund can briefly put `balance` above it.
    ceiling: int
    #: The soonest instant a held monthly credit stops being spendable (the 1st
    #: of next month, midnight UTC, exclusive). Null when `balance` is 0, or
    #: when none of the held monthly credits expire (a migrated opening balance).
    expires_at: datetime | None
    #: Whether this account receives the monthly grant on the 1st: a mentee goal
    #: and an unlock from a qualifying invite. `ceiling` is the grant either way,
    #: so `false` tells "not unlocked yet" apart from "spent".
    unlocked: bool


class BonusCreditGroupRead(BaseModel):
    """Bonus credits that stop being spendable at the same instant."""

    model_config = ConfigDict(from_attributes=True)

    count: int
    #: Exclusive, like `next_reset_at`. Null means the credits never expire.
    expires_at: datetime | None


class BonusCreditsRead(BaseModel):
    """Every credit that is not the monthly grant: the starter, the invite
    bonus, support grants, and refunds of any of them."""

    model_config = ConfigDict(from_attributes=True)

    balance: int
    #: Soonest expiry first; never-expiring last.
    groups: list[BonusCreditGroupRead]


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
    #: `balance` split in two (decision 232): `monthly.balance + bonus.balance`
    #: always equals `balance`.
    monthly: MonthlyCreditsRead
    bonus: BonusCreditsRead


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

    #: **Present for every caller on `/me`** (decision 228): anyone signed in may
    #: book, a mentor included, and every booking spends a credit. Nullable only
    #: in the schema; the monthly grant, not this card, requires a goal.
    credits: CreditsRead | None = None

    #: Sessions the caller has **received** as a mentee with status `completed`.
    #: **Never null** — zero is a real answer, as on the discovery card. Not the
    #: card's `completed_sessions`, which counts sessions a mentor *gave*; a
    #: dual-role user has both and they are different numbers.
    mentee_completed_sessions: int = 0

    #: The caller's booking counts, per role — the sidebar's Bookings badge and
    #: dashboard headings. `as_mentee` is always present; only `as_mentor` needs
    #: a mentor profile (decision 228).
    booking_counts: BookingCountsRead = Field(default_factory=lambda: BookingCountsRead())
    mentee_cancel_refund_hours: int = Field(
        default=12,
        description=(
            "How many hours before the start a mentee may cancel and get the credit "
            "back: the deployment's `MENTEE_CANCEL_REFUND_HOURS`. For copy that "
            "explains the rule without a session to hand; a session's own deadline "
            "is its `refund_until`."
        ),
    )
