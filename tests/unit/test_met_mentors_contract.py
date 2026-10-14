"""`MetMentorRead`'s required fields (decision 248).

Everything else is optional (`test_added_fields_stay_optional`); these three are
not, because a card without them cannot be identified, ordered or acted on, and
no default is true.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.main import create_app

REQUIRED = {"id", "last_session_at", "bookability"}


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, object]]:
    return create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]  # type: ignore[call-arg, no-any-return]


def test_exactly_the_identity_the_order_and_the_state_are_required(
    schemas: dict[str, dict[str, object]],
) -> None:
    assert set(schemas["MetMentorRead"].get("required", [])) == REQUIRED  # type: ignore[call-overload]


def test_the_list_is_a_typed_page_on_me_mentors(schemas: dict[str, dict[str, object]]) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()  # type: ignore[call-arg]
    ok = spec["paths"]["/api/v1/me/mentors"]["get"]["responses"]["200"]
    ref = ok["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    data = schemas[ref]["properties"]["data"]  # type: ignore[index]
    assert data["items"]["$ref"].endswith("/MetMentorRead")  # type: ignore[index]
