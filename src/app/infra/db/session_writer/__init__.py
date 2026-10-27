"""Writing sessions: booking, transitions, attendance, meetings, reminders and
suggested times.

One module per area, split from a single file that neared the 900-code-line
limit. Every name a caller used is re-exported here, so imports did not change.
"""

from __future__ import annotations

from app.infra.db.session_writer.attendance import (
    confirm_presence,
    door_row,
    observe_departure,
    observe_presence,
    presence_to_confirm,
    record_arrival,
    settle_attendance,
)
from app.infra.db.session_writer.booking import (
    DOUBLE_BOOKED,
    book_session,
)
from app.infra.db.session_writer.follow_ups import (
    FollowUps,
    on_confirmed,
    on_ended,
    retry_releases,
)
from app.infra.db.session_writer.meetings import add_meet_link
from app.infra.db.session_writer.minutes import measure_minutes
from app.infra.db.session_writer.reminders import (
    remind_before_session,
    remind_if_still_waiting,
    remind_unreviewed,
)
from app.infra.db.session_writer.suggestions import (
    remind_suggestion,
    suggest_time,
)
from app.infra.db.session_writer.transitions import (
    end_for_move,
    expire_requests,
    transition,
)

__all__ = [
    "DOUBLE_BOOKED",
    "FollowUps",
    "add_meet_link",
    "book_session",
    "confirm_presence",
    "door_row",
    "end_for_move",
    "expire_requests",
    "measure_minutes",
    "observe_departure",
    "observe_presence",
    "on_confirmed",
    "on_ended",
    "presence_to_confirm",
    "record_arrival",
    "remind_before_session",
    "remind_if_still_waiting",
    "remind_suggestion",
    "remind_unreviewed",
    "retry_releases",
    "settle_attendance",
    "suggest_time",
    "transition",
]
