"""What a user looks like over the wire."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from app.api.schemas.profile import AwardRead, EducationRead, GoalRead, MentorProfileRead
from app.domain.enums import CreditState, PrimaryRole


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
        return value.strip().lower()


class UserProfileRead(BaseModel):
    """The profile fields a user may see about themselves."""

    model_config = ConfigDict(from_attributes=True)

    about_me: str | None = None
    gender: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    social_linkedin: str | None = None
    social_twitter: str | None = None
    social_youtube: str | None = None


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
