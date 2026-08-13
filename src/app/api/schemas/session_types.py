"""What a mentor offers, as a stranger sees it.

**A session type is the bookable thing** — this mentor's own product, with a
duration and a venue. It is *not* a `service_offering`, which is the closed
six-row taxonomy at `/api/v1/catalog/service-offerings` describing what **kind**
of help exists and is what matching joins on. Two different concepts, and the
word "service" belongs to the other one.

The fields here are an allowlist rather than a projection of the table.
`created_by` is internal attribution and null on every migrated row;
`category` and `application_stage` are free text with no constraint, no
vocabulary and no value in any row today, so publishing them would commit this
contract to a shape nobody has designed. Adding a field later is additive;
removing one is breaking.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import MeetingProvider


class SessionTypeRead(BaseModel):
    """One offering, and everything needed to ask for its slots."""

    id: str = Field(
        description=(
            "Pass as `session_type_id` to "
            "`/api/v1/users/{user_id}/availability/slots`. A slot's length and "
            "notice come from this offering, so slots cannot be asked for "
            "without naming one."
        )
    )
    name: str
    description: str | None = None
    duration_minutes: int = Field(
        description="How long a session of this type runs, and the step between slots."
    )
    min_notice_minutes: int = Field(
        description=(
            "How far ahead a booking must be made. Slots starting sooner than "
            "this are not offered, which is why the next few hours can look "
            "empty on a mentor who is free."
        )
    )
    meeting_venue: MeetingProvider = Field(
        description=(
            "Where the session happens, already resolved — a session type "
            "without its own venue inherits the mentor's, which is itself "
            "never unset. The meeting **link** is generated per session and "
            "never appears here: a static room means back-to-back sessions "
            "share it and an early joiner walks into the previous one."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> SessionTypeRead:
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            description=str(row["description"]) if row["description"] else None,
            duration_minutes=int(str(row["duration_minutes"])),
            min_notice_minutes=int(str(row["min_notice_minutes"])),
            meeting_venue=MeetingProvider(str(row["meeting_venue"])),
        )
