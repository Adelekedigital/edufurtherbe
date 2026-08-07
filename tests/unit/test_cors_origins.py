"""How ``cors_origins`` is spelled, and what happens when it is not spelled.

The field broke a deploy. `pydantic-settings` JSON-decodes complex types **inside
the settings source**, before any validator runs, so an operator who typed a bare
origin into a cloud console got ``SettingsError: error parsing value for field
"cors_origins"`` and an application that would not boot. The setting was not even
wired to anything at the time.

Both spellings are accepted deliberately. JSON is what `.env.example` documented
and what any existing configuration uses; comma-separated is what a person types
into a form field. Dropping either one breaks somebody.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

ORIGIN = "https://app.edufurther.org"
SECOND = "https://staging.edufurther.org"


def origins(raw: str | None, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Build Settings with only this variable set, from the environment."""
    if raw is None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("CORS_ORIGINS", raw)
    return Settings(_env_file=None).cors_origins


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(f"{ORIGIN}", [ORIGIN], id="bare-origin"),
        pytest.param(f"{ORIGIN},{SECOND}", [ORIGIN, SECOND], id="comma"),
        pytest.param(f" {ORIGIN} , {SECOND} ", [ORIGIN, SECOND], id="comma-with-spaces"),
        pytest.param(f'["{ORIGIN}"]', [ORIGIN], id="json-single"),
        pytest.param(f'["{ORIGIN}", "{SECOND}"]', [ORIGIN, SECOND], id="json-multiple"),
        pytest.param("", [], id="empty"),
        pytest.param("   ", [], id="whitespace-only"),
        pytest.param(f"{ORIGIN},,", [ORIGIN], id="trailing-separator"),
    ],
)
def test_the_forms_an_operator_might_type(
    raw: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert origins(raw, monkeypatch) == expected


def test_unset_is_empty_rather_than_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default has to survive the field being absent entirely.

    This is the state that unblocked the broken deploy — unsetting the variable —
    so it is the one that must keep working.
    """
    assert origins(None, monkeypatch) == []


def test_malformed_json_fails_as_a_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not ``SettingsError``.

    A ``ValidationError`` names the field and the reason; the ``SettingsError``
    this replaces said only "error parsing value for field", which is what made
    the original failure take a deploy to diagnose. Asserting the *type* is the
    point — a test that merely asserted "raises" would pass against the old
    behaviour it exists to rule out.
    """
    with pytest.raises(ValidationError):
        origins('["unclosed', monkeypatch)


def test_a_json_array_of_non_strings_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator hands parsed JSON on for normal field validation.

    Bypassing the decoder must not also bypass the type check.
    """
    with pytest.raises(ValidationError):
        origins("[1, 2]", monkeypatch)
