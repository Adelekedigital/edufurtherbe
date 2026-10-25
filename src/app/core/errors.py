"""The base error taxonomy.

Transport-agnostic on purpose: no HTTP status codes here. The API layer maps
these to responses, which is what keeps ``domain/`` free of framework concepts.
"""


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    def problem_members(self) -> dict[str, object]:
        """Extension members for the problem document — RFC 9457 lets a problem
        type carry its own. None by default; a refusal that has facts a client
        acts on overrides this (settled decision #197)."""
        return {}

    def problem_headers(self) -> dict[str, str]:
        """Response headers the refusal carries. None by default; a rate limit
        sets `Retry-After`."""
        return {}


class NotFoundError(AppError):
    """The resource does not exist, or is not visible to this caller.

    Deliberately conflates the two. Distinguishing them leaks the existence of
    other tenants' rows to anyone who can enumerate ids.
    """


class ConflictError(AppError):
    """The request cannot be applied to the current state of the resource."""


class LinkPlatformTakenError(ConflictError):
    """The caller already has a link on this platform; one each, except
    websites (decision 275). Edit or delete the one they have."""


class WebsiteLimitReachedError(ConflictError):
    """The caller already lists the most websites allowed (decision 275).

    `limit` says how many, so the client need not repeat the number.
    """

    def __init__(self, message: str = "", *, limit: int) -> None:
        super().__init__(message)
        self.limit = limit

    def problem_members(self) -> dict[str, object]:
        return {"limit": self.limit}


class SessionTypeLimitReachedError(ConflictError):
    """The mentor already holds as many session types as they may (Session
    Types PRD, decision 286). `limit` says how many, so the client need not
    know the default or a support override."""

    def __init__(self, message: str = "", *, limit: int) -> None:
        super().__init__(message)
        self.limit = limit

    def problem_members(self) -> dict[str, object]:
        return {"limit": self.limit}


class AccountExistsError(ConflictError):
    """A first sign-in's email already belongs to an account it is not linked to.

    Refused rather than linked, and refused rather than duplicated. Linking by
    email is how an account is taken over; a second account for one address is
    how a person ends up with two histories. The only safe outcome is a person —
    support — deciding (settled decision #178).

    Carries a problem type, because the client's response is unlike any other
    refusal: not "sign in again", but "contact support".
    """


class InviteForNewAccountsError(ConflictError):
    """The caller's account is too old to claim an invite (decision 264).

    Carries a problem type: the client says "invites are for new accounts"
    rather than a generic conflict, and nothing the caller does with the code
    changes the answer.
    """


class InsufficientCreditError(ConflictError):
    """The caller has no spendable credit left.

    A conflict rather than a validation failure: the request is well formed and
    the offering is real — what is wrong is the caller's balance, which is state.

    Carries a problem type, because the client's response is unlike every other
    refusal on the booking path: not "pick another time", but "you are out of
    credits", which is a different screen. Settled decision #110's bar for
    earning one.
    """


class MissingItemsError(ConflictError):
    """A refusal that names what the caller still owes, as the `missing`
    extension member: plain strings, because `core` imports nothing."""

    def __init__(self, message: str = "", *, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing

    def problem_members(self) -> dict[str, object]:
        return {"missing": list(self.missing)}


class OnboardingIncompleteError(MissingItemsError):
    """The profile does not yet meet the bar for finishing onboarding.

    A conflict rather than a validation failure: nothing about the *request* is
    wrong, and the caller sent no body at all. What is wrong is the state of the
    resource, which is exactly what 409 says.

    Carries a problem type, because the client's response differs from every
    other refusal on this route — send the user back to the profile form, never
    retry. That is the bar settled decision #110 set for earning one.

    `missing` names the mentee requirements still owed, in step order, sent as
    the `missing` extension member (decision 261). Plain strings, because
    `core` imports nothing: the caller passes the domain's codes.
    """


class MentorApplicationIncompleteError(MissingItemsError):
    """A mentor application cannot be submitted while it is missing something.

    Its own problem type for the same reason as onboarding's: the client sends
    the mentor back to the step that is missing, never retries. `missing`
    carries the domain's `MentorApplicationItem` codes, in step order.
    """


class MentorApplicationInReviewError(ConflictError):
    """A submitted application's reviewed fields hold still until the decision.

    `fields` names what the request would have changed (a `FROZEN_FIELDS` name
    or a `FrozenRecord`, in `domain.mentor_application`), so the client can say
    which part is locked.
    """

    def __init__(self, message: str = "", *, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields

    def problem_members(self) -> dict[str, object]:
        return {"fields": list(self.fields)}


class MentorApplicationDeclinedError(ConflictError):
    """A declined mentor's booking setup is locked (owner, 2026-10-20).

    Session types, hours, the calendar, conferencing and the booking rules can
    be read but not changed; the profile stays editable. `fields` names the
    booking rules a mentor-profile write sent, and is empty elsewhere.
    """

    def __init__(self, message: str = "", *, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields

    def problem_members(self) -> dict[str, object]:
        return {"fields": list(self.fields)}


class MentorApplicationDecidedError(ConflictError):
    """The application was already approved or declined, so there is nothing to
    submit. A type of its own: the client shows the outcome, not the form."""


class BookingNeedsOnboardingError(MissingItemsError):
    """Booking is for someone who has finished onboarding (owner, 2026-10-18).

    Its own problem type: the client shows the finish-setup step, then brings
    the mentee back to confirm. `missing` names the mentee answers still owed,
    as on `/problems/onboarding-incomplete`; it can be empty when every answer
    is given and only the completion call is left.
    """


class AlreadyReviewedError(ConflictError):
    """This session already carries a review by this author.

    Terminal: no amount of waiting makes it succeed. Separate from
    :class:`ReviewIntervalError` because a client's response to the two differs,
    which is precisely the condition settled decision #110 named as the trigger
    for a machine-readable problem type.
    """


class ReviewIntervalError(ConflictError):
    """This author reviewed the same offering inside ``REVIEW_INTERVAL``.

    Retryable, once the window passes — the opposite of the error above, and
    indistinguishable from it without a type.
    """


class BookingOverlapError(ConflictError):
    """The mentee already has a live session at an overlapping time (#342).

    Each of the three booking limits carries its own problem type, because the
    client's copy differs for each and none of them should suggest cancelling.
    """


class BookingWithMentorExistsError(ConflictError):
    """The mentee already has a live session with this mentor (#342)."""


class JoinWindowClosedError(ConflictError):
    """Arrivals have stopped and this party never pressed Join in time, so the
    door is closed to them (owner, 2026-10-08)."""


class BookingLimitReachedError(ConflictError):
    """The mentee already holds the most live sessions allowed at once (#342)."""


class RateLimitedError(AppError):
    """The caller has done this too often recently (#281).

    Carries when to try again, which the API layer sends as `Retry-After`. Not a
    `ConflictError`: nothing about the resource is in the way, only time is.
    """

    def __init__(self, message: str = "", *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

    def problem_headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after_seconds)}


class ValidationError(AppError):
    """Input violated a domain rule, as opposed to failing a schema check.

    `field_errors` names the parts of the request at fault as `(pointer,
    message)` pairs — RFC 6901 pointers into the body, the shape the 422's
    `errors` member publishes (#195). Empty for a refusal about the request as
    a whole, which is most of them.
    """

    def __init__(self, message: str = "", *, field_errors: tuple[tuple[str, str], ...] = ()):
        super().__init__(message)
        self.field_errors = field_errors


class ConfigurationError(AppError):
    """A required setting is absent or unusable, found when something needs it.

    Distinct from the others: this is an operator fault, not a caller fault, and
    it must never be mapped to a 4xx. Raised where the setting is consumed rather
    than at startup, because settings load before a database is necessarily
    reachable and a required field would break ``import app.main``.
    """


class UpstreamError(AppError):
    """A third party we depend on failed, or answered in a way we cannot use.

    **Here rather than beside the adapters that raise it**, for the reason
    `AuthenticationError` gives below: `api` may not import `infra`, so an error
    defined there is one the transport layer cannot map. Left in `infra` it fell
    through to a 500 — an unmapped `AppError`, exactly the gap that table's
    fallback exists to make visible.

    Not the caller's fault, so never a 4xx. The one case where the caller can
    act — a Google grant that came back without a refresh token — is still a
    fault of the exchange rather than of the request that started it.
    """


class AuthenticationError(AppError):
    """The caller could not be authenticated.

    Lives here rather than beside the token verifier that raises it. ``api`` may
    not import ``infra``, so an error defined there is an error the transport
    layer cannot map — it fell through to a 500 exactly once, which is how this
    ended up in the taxonomy where it belonged all along.

    One error for every cause. A caller learning *which* check failed learns
    which half of their guess was right.
    """


class WritingAssistUnavailableError(AppError):
    """The writing assistant has no answer to give right now (decision 267).

    Off, unconfigured, the router unreachable or out of time, or an answer the
    router itself would not stand behind: the client's response is the same
    for every one ("couldn't write that now, try again or write your own"), so
    one class and one problem type. A `503`, not `UpstreamError`'s `502`: the
    feature being switched off is this service's own state (decision 142's
    reopen case), and the client is meant to try again later.
    """
