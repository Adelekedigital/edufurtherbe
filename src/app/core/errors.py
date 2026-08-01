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


class ValidationError(AppError):
    """Input violated a domain rule, as opposed to failing a schema check."""
