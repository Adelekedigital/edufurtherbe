"""The error taxonomy.

Guards the property the API layer relies on: one base class it can map to a
response, so a new error type cannot escape as an unhandled 500.
"""

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError


def test_every_error_derives_from_app_error() -> None:
    for error_type in (NotFoundError, ConflictError, ValidationError):
        assert issubclass(error_type, AppError)


def test_app_error_is_an_exception() -> None:
    assert issubclass(AppError, Exception)


def test_errors_carry_their_message() -> None:
    error = NotFoundError("mentor not found")

    assert str(error) == "mentor not found"
