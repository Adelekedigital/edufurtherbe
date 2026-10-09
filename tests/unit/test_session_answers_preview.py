"""The session read's field set, and how one answer reads as a line of text."""

from __future__ import annotations

from typing import Any

import pytest

from app.api.schemas.sessions import PartyRead, SessionRead
from app.infra.db.session_answer_rows import preview_text


def test_the_session_read_has_exactly_these_fields() -> None:
    """A field added or dropped here is a contract change the frontend must hear of."""
    assert set(SessionRead.model_fields) == {
        "id",
        "mentor_id",
        "mentee_id",
        "mentor",
        "mentee",
        "session_type_id",
        "session_type",
        "status",
        "starts_at",
        "duration_minutes",
        "topic",
        "booking_message",
        "meeting_provider",
        "meeting_url",
        "respond_by",
        "join_opens_at",
        "join_closes_at",
        # #379: until when `/door` hands back a way in. Optional in the spec, so
        # adding it cannot fail a client that validates against the old one.
        "door_closes_at",
        "created_at",
        "suggestion",
        "answers_preview",
        "mentee_attendance_rate",
    }


def _answer(**overrides: Any) -> dict[str, Any]:
    return {"text": None, "options": [], "file": None} | overrides


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (_answer(text="MSc Public Policy"), "MSc Public Policy"),
        (_answer(options=[{"text": "IELTS"}, {"text": "TOEFL"}]), "IELTS, TOEFL"),
        (_answer(file={"filename": "My CV.pdf"}), "My CV.pdf"),
        (_answer(), ""),
    ],
)
def test_an_answer_reads_as_one_line(answer: dict[str, Any], expected: str) -> None:
    assert preview_text(answer) == expected


def test_each_party_has_exactly_these_fields() -> None:
    """A party is what each side of a session sees of the other, so a field added
    here is a contract change the frontend must hear of first (and an email or
    slug here would be a leak: see the schema's own docstring)."""
    assert set(PartyRead.model_fields) == {
        "id",
        "deleted",
        "first_name",
        "last_name",
        "avatar_url",
        "avatar_focus",
        "timezone",
        "joined_at",
        # #382: when Daily first saw this party in the room. Optional in the
        # spec; `joined_at` stays the Join press.
        "in_room_at",
        "attendance_status",
    }
