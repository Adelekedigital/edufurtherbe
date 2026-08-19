"""Telling somebody something, over whichever channel reaches them.

**Two adapters and a null one**, following ``hipolabs`` — a concrete class per
source, structurally interchangeable, with no ``Protocol`` declared because the
call site names the type it wants and duck typing does the rest.

**Neither real adapter is wired, and both say so loudly.** Emailit and Zernio
are their own phase; what ships here is the shape, so the producer that sends
reminders can be written against something that exists. A stub that quietly
returned success would be worse than none: the first real send would be the
first time anybody discovered the shape was wrong.

**The null adapter is the default and it is not a fake.** It records the intent
and delivers nothing, which is exactly the current state of the world — there is
no email provider configured and WhatsApp can reach nobody, because the three
``phone_*`` columns are deferred and the runbook lists phone collection as
post-cutover work. `api_storage` in the test suite takes the same position for
the same reason: a default that works is a default that hides a missing wire.

**The template id is resolved here, not in settings.** ``core`` may import
nothing, so it cannot key its maps on `Notification` — and resolving at the
point of use is what ``ConfigurationError`` prescribes anyway: *"raised where
the setting is consumed rather than at startup, because settings load before a
database is necessarily reachable"*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError, ConfigurationError
from app.domain.notifications import Channel, Notification

__all__ = [
    "DeliveryError",
    "EmailitNotifier",
    "Message",
    "NullNotifier",
    "ZernioNotifier",
    "template_for",
]

logger = logging.getLogger(__name__)


class DeliveryError(AppError):
    """The message could not be handed to the provider.

    An ``AppError`` so the API layer can map it, though nothing raises it into a
    request yet — every send today comes from a scheduled producer, where the
    right response is a failed run rather than a status code.
    """


@dataclass(frozen=True, slots=True)
class Message:
    """One thing to say to one person.

    **A template and its variables, never a body.** Both providers compose from
    an approved template — Meta requires it for any business-initiated WhatsApp
    message, which is every message this platform sends — so a body assembled
    here would be a body neither provider would use.

    ``to`` is the address for the channel: an email address, or an E.164 phone
    number. Held as a string rather than a user id because the adapter has no
    database and should not grow one; resolving a person to an address is the
    producer's job.
    """

    notification: Notification
    channel: Channel
    to: str
    variables: dict[str, str]


def template_for(settings: Settings, notification: Notification, channel: Channel) -> str:
    """The provider's id for this message, or an operator-facing failure.

    **Raises rather than falling back.** A missing template has no sensible
    default: sending the wrong message is worse than sending none, and silently
    sending nothing would make a channel look configured when it is not.
    """
    templates = (
        settings.email_templates if channel is Channel.EMAIL else settings.whatsapp_templates
    )
    template = templates.get(str(notification))
    if not template:
        raise ConfigurationError(f"no {channel} template configured for {notification}")
    return template


class NullNotifier:
    """Records the intent and delivers nothing. The default.

    **Not a fake and not a no-op.** It logs what would have been sent, so a
    producer can be developed and run end to end against a real database with
    the sends visible in the log — and so the absence of a provider is a line
    somebody can read rather than silence.

    It does **not** resolve a template, deliberately. Requiring one here would
    make local development need a configured provider to exercise a code path
    that sends nothing.
    """

    def send(self, message: Message) -> None:
        logger.info(
            "notification not sent (no provider configured): %s to %s over %s",
            message.notification,
            message.to,
            message.channel,
        )


class EmailitNotifier:
    """Emailit, and it is not built.

    Raises rather than returning, because a stub that reported success would
    make the first real send the first time anybody discovered the shape was
    wrong — and the producer above it would look correct while delivering
    nothing.
    """

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self._settings = settings
        self._client = client

    def send(self, message: Message) -> None:
        template_for(self._settings, message.notification, Channel.EMAIL)
        raise NotImplementedError(
            "the Emailit adapter is not built — see the integration phase in "
            "docs/handoff-session-build.md"
        )


class ZernioNotifier:
    """WhatsApp through Zernio, and it is not built either.

    **Conversation-centric, not recipient-centric**, which is the one thing
    about this provider that will shape the adapter rather than sit inside it. A
    business-initiated send creates or reuses a conversation
    (``POST /v1/inbox/conversations`` with the recipient's number); a free-form
    reply goes into an existing one. The ``conversationId`` is stable per
    participant and the provider's own documentation says to store it alongside
    contact records — so wiring this needs a column, not just a client.

    **Every message this platform sends is business-initiated**, and therefore
    outside the 24-hour customer-service window, so every one of them must be a
    template Meta has approved. That approval is a lead time nobody here
    controls.

    It can reach nobody today regardless: the three ``phone_*`` columns are
    deferred, so no user has a number.
    """

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self._settings = settings
        self._client = client

    def send(self, message: Message) -> None:
        template_for(self._settings, message.notification, Channel.WHATSAPP)
        raise NotImplementedError(
            "the Zernio adapter is not built — it needs the phone_* columns, an "
            "approved template per message, and the Facebook Business account resolved"
        )
