"""The reason codes each side may give are published, and match what is enforced.

The frontend filters its reason picker by side from this list, so a list that
drifted from `domain/sessions.py` would offer a code the server refuses, or hide
one it accepts.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.routes.sessions import reason_codes_doc
from app.core.config import Settings
from app.domain.enums import SessionReasonCode
from app.domain.sessions import TRANSITIONS, reason_required
from app.main import create_app

#: Each ending, and whether its body may offer another time instead.
ENDINGS = {"decline": True, "withdraw": False, "cancel": True}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app(Settings(_env_file=None)).openapi()


def _ending(spec: dict[str, Any], action: str) -> dict[str, Any]:
    return spec["paths"][f"/api/v1/sessions/{{session_id}}/{action}"]["post"]


@pytest.mark.parametrize("action", ENDINGS)
def test_each_ending_publishes_its_codes(spec: dict[str, Any], action: str) -> None:
    assert reason_codes_doc(action, offers=ENDINGS[action]) in _ending(spec, action)["description"]


@pytest.mark.parametrize("action", ENDINGS)
def test_the_published_list_is_the_enforced_one(action: str) -> None:
    """Every permitted code is named on its side's line, and no other code is."""
    lines = reason_codes_doc(action, offers=ENDINGS[action]).splitlines()
    for role, codes in TRANSITIONS[action].reasons.items():
        (line,) = (line for line in lines if line.startswith(f"- {role.value}: "))
        named = {code for code in SessionReasonCode if f"`{code.value}`" in line}
        assert named == codes


@pytest.mark.parametrize("action", ENDINGS)
def test_only_an_offering_ending_mentions_the_exemption(action: str) -> None:
    assert ("suggested_starts_at" in reason_codes_doc(action, offers=ENDINGS[action])) is ENDINGS[
        action
    ]


@pytest.mark.parametrize("action", ENDINGS)
def test_each_ending_requires_a_body_and_accepting_takes_none(
    spec: dict[str, Any], action: str
) -> None:
    """A required reason means a required body; `accept` explains itself."""
    assert _ending(spec, action)["requestBody"]["required"] is True
    assert "requestBody" not in _ending(spec, "accept")


def test_other_is_open_to_both_sides_on_every_ending() -> None:
    """**`other` is the "none of these" answer**, so a side without it would be
    forced to pick a reason that is not true."""
    for action in ENDINGS:
        for codes in TRANSITIONS[action].reasons.values():
            assert SessionReasonCode.OTHER in codes


@pytest.mark.parametrize("action", ENDINGS)
def test_ending_a_session_requires_a_reason_unless_offering_a_time(action: str) -> None:
    assert reason_required(action, offers_another_time=False)
    assert not reason_required(action, offers_another_time=True)


def test_accepting_never_requires_a_reason() -> None:
    assert not reason_required("accept", offers_another_time=False)
