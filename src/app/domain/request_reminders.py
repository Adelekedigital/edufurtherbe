"""A mentee reminding the mentor about a pending request (decision 294).

Once per session, while the request still awaits an answer. The message is the
mentee's own words, carried to the mentor in the email.
"""

from __future__ import annotations

__all__ = ["REMINDER_MESSAGE_MAX_LENGTH"]

#: The longest reminder a mentee may write, after trimming (Mentee Bookings PRD).
REMINDER_MESSAGE_MAX_LENGTH = 500
