"""`PartyRead` extends the shared `PersonBaseRead` (Messages, decision 301).

The person fields moved into a base that a chat's `PersonRead` extends too.
The move must not change what a session's party publishes: this pins the
schema to what it was before, compared as data, so key order is free.
"""

import json

from app.main import create_app
from conftest import PROJECT_ROOT

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "party_read_schema.json"


def test_a_session_party_s_published_shape_is_unchanged() -> None:
    published = create_app().openapi()["components"]["schemas"]["PartyRead"]

    assert published == json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_is_the_party_and_not_an_empty_object() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert {"id", "deleted", "degree", "institution", "timezone", "attendance_status"} <= set(
        fixture["properties"]
    )
