"""Writing sessions: booking, transitions, attendance, meetings, reminders and
suggested times.

One module per area, split from a single file that neared the 900-code-line
limit. Every name a caller used is re-exported here, so imports did not change.
"""

from __future__ import annotations

from app.infra.db.session_writer.attendance import (
    record_arrival,
    settle_attendance,
)
from app.infra.db.session_writer.booking import (
    DOUBLE_BOOKED,
    book_session,
)
from app.infra.db.session_writer.meetings import (
    provision_meeting,
    release_meeting,
)
from app.infra.db.session_writer.reminders import (
    remind_before_session,
    remind_if_still_waiting,
    remind_unreviewed,
    schedule_session_reminders,
)
from app.infra.db.session_writer.suggestions import (
    remind_suggestion,
    suggest_time,
)
from app.infra.db.session_writer.transitions import (
    expire_requests,
    transition,
)

__all__ = [
    "DOUBLE_BOOKED",
    "book_session",
    "expire_requests",
    "provision_meeting",
    "record_arrival",
    "release_meeting",
    "remind_before_session",
    "remind_if_still_waiting",
    "remind_suggestion",
    "remind_unreviewed",
    "schedule_session_reminders",
    "settle_attendance",
    "suggest_time",
    "transition",
]
