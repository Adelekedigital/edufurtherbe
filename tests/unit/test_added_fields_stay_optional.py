"""Fields added to a response after the frontend shipped stay out of `required`.

The frontend's CI pulls the published spec on every run, and its fixtures build
these objects by hand. A field added as required turns its `main` red the moment
the spec publishes, before it can add the field: TypeScript rejects a property
the spec does not yet know. Optional, and always sent (FE, 2026-10-09).
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.main import create_app

#: Each schema, and the fields added to it after the frontend first consumed it.
ADDED = {
    "PartyRead": ("in_room_at", "degree", "institution"),
    "SessionRead": ("mentee_attendance_sessions", "refund_until"),
    "UserRead": ("mentee_cancel_refund_hours",),
    "SessionTypeRead": ("requires_booking_confirmation",),
    "SessionAnswerRead": ("answered", "required"),
}


@pytest.mark.parametrize(("schema", "fields"), ADDED.items())
def test_an_added_field_is_not_required(schema: str, fields: tuple[str, ...]) -> None:
    spec = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"][schema]  # type: ignore[call-arg]

    assert set(fields) <= set(spec["properties"])
    assert not set(fields) & set(spec.get("required", []))
