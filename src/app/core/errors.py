"""The base error taxonomy.

Transport-agnostic on purpose: no HTTP status codes here. The API layer maps
these to responses, which is what keeps ``domain/`` free of framework concepts.
"""


class AppError(Exception):
    """Base class for every error this application raises deliberately."""


class NotFoundError(AppError):
    """The resource does not exist, or is not visible to this caller.

    Deliberately conflates the two. Distinguishing them leaks the existence of
    other tenants' rows to anyone who can enumerate ids.
    """


class ConflictError(AppError):
    """The request cannot be applied to the current state of the resource."""


class InsufficientCreditError(ConflictError):
    """The caller has no spendable credit left.

    A conflict rather than a validation failure: the request is well formed and
    the offering is real — what is wrong is the caller's balance, which is state.

    Carries a problem type, because the client's response is unlike every other
    refusal on the booking path: not "pick another time", but "you are out of
    credits", which is a different screen. Settled decision #110's bar for
    earning one.
    """


class OnboardingIncompleteError(ConflictError):
    """The profile does not yet meet the bar for finishing onboarding.

    A conflict rather than a validation failure: nothing about the *request* is
    wrong, and the caller sent no body at all. What is wrong is the state of the
    resource, which is exactly what 409 says.

    Carries a problem type, because the client's response differs from every
    other refusal on this route — send the user back to the profile form, never
    retry. That is the bar settled decision #110 set for earning one.
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


class ValidationError(AppError):
    """Input violated a domain rule, as opposed to failing a schema check."""


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
