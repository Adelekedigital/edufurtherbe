"""A mentor the caller has met, as `/me/mentors` lists them (decision 248).

**The id, the last session and the state are required; everything else is
optional**, the split decision 246's models make: what identifies, orders and
acts on a card has no honest default, and every other field may be added to
later, so it carries one and is registered in
`tests/unit/test_added_fields_stay_optional.py`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.common import AvatarFocusRead
from app.domain.met_mentors import Bookability, bookability


class MetMentorRead(BaseModel):
    """One mentor you have had at least one completed session with."""

    id: str = Field(description="The mentor's user id. `/mentors/{id}` accepts it.")
    slug: str | None = Field(
        default=None,
        description=(
            "The mentor's public handle, **only while their profile is public** "
            "(approved and listed), the one time `/mentors/{slug}` resolves. "
            "Null otherwise, or when they have none: link by `id` then."
        ),
    )
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None
    #: Where to centre `avatar_url`; `null` means the client's default crop.
    avatar_focus: AvatarFocusRead | None = None
    degree: str | None = Field(
        default=None,
        description=(
            "The degree that best describes the mentor, as on a session's party "
            "card: their highest, then most recent. `null` with no education entry."
        ),
    )
    institution: str | None = Field(
        default=None,
        description="Where that degree is from. `null` as for `degree`.",
    )
    session_count: int = Field(
        default=0,
        description=(
            "Completed sessions you have had with this mentor, by the rule "
            "`/me`'s `mentee_completed_sessions` counts."
        ),
    )
    last_session_at: dt.datetime = Field(
        description="When the most recent of those sessions started. The list's order."
    )
    bookability: Bookability = Field(
        description=(
            "What you can do next with this mentor, one state by precedence: "
            "`has_upcoming` (a confirmed session with them has not started), "
            "then `pending` (a request to them awaits their answer), then "
            "`unavailable` (they are not taking bookings, as on their profile), "
            "else `open`. A client should treat a value it does not know as "
            "`unavailable`: more may be added."
        )
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MetMentorRead:
        return cls(
            id=str(row["id"]),
            slug=row["slug"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            avatar_url=row["avatar_url"],
            avatar_focus=AvatarFocusRead.of(row["avatar_focus_x"], row["avatar_focus_y"]),
            degree=row["degree"],
            institution=row["institution"],
            session_count=int(row["session_count"]),
            last_session_at=row["last_session_at"],
            bookability=bookability(
                has_upcoming=bool(row["has_upcoming"]),
                pending=bool(row["pending"]),
                taking_bookings=bool(row["taking_bookings"]),
            ),
        )
