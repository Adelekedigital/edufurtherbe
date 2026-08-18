"""What the platform tells people, as a closed set.

**A vocabulary rather than free strings**, because it is what the template map
in ``core/config.py`` is keyed on: an unknown key there is a message that would
be composed at send time and fail in front of a user, where a closed set fails
at startup with the name of the thing that is missing.

**Deliberately almost empty.** Only messages a settled decision already names
belong here — the rest arrive with the notification sequence, which is being
designed. Inventing the set now would mean guessing at product and then holding
template ids for messages nobody decided to send, which is the shape that gets a
vocabulary shipped and then quietly ignored.

Adding a member is one line here plus one entry in the deployed map. That is the
whole point of settling the shape before the content.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Channel", "Notification"]


class Notification(StrEnum):
    """One thing the platform says. The key of the template map."""

    #: The mentor has an unanswered request and the deadline is approaching.
    #: Sent three times — on booking, 24 hours before ``respond_by`` where the
    #: lead allows it, and 12 hours before — because the mentor's decision is
    #: what frees or fills the slot, and an unanswered request costs them the
    #: hour whether or not they meant to keep it.
    #:
    #: **One member for three sends, not three.** The message is the same
    #: sentence with a different urgency; the *schedule* belongs to the producer
    #: and splitting it into three would put a timing decision in a vocabulary.
    MENTOR_RESPONSE_REMINDER = "mentor_response_reminder"


class Channel(StrEnum):
    """How it is delivered.

    **Both, not one.** Email reaches everybody today; WhatsApp reaches nobody,
    because the three ``phone_*`` columns are deferred and the migration runbook
    lists phone collection as post-cutover work. So a message declared for both
    channels is delivered on one for now, and that is a fact about the data
    rather than about the code.

    ADR 0006 bounds the WhatsApp side: platform-to-user transactional only.
    Mentor-to-mentee conversation stays in-platform, because handing both sides
    of a paid booking a direct off-platform channel is disintermediation by
    default rather than by decision.
    """

    EMAIL = "email"
    WHATSAPP = "whatsapp"
