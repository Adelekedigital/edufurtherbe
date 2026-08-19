"""Telling somebody something, over whichever channel reaches them.

**Two adapters and a null one**, following ``hipolabs`` — a concrete class per
source, structurally interchangeable, with no ``Protocol`` declared because the
call site names the type it wants and duck typing does the rest.

**Loops is wired; Zernio is not.** ADR 0025 settles transactional email as
Loops, with Emailit carrying the Supabase auth code, marketing and subscription
— so nothing here sends the auth code, and `EmailitNotifier` is gone rather than
left as a stub for a job this port does not have.

Zernio still refuses loudly, because a stub that quietly returned success would
make the first real send the first time anybody discovered the shape was wrong.
It can reach nobody regardless: the three ``phone_*`` columns are deferred.

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
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import AppError, ConfigurationError
from app.domain.notifications import Channel, Notification

__all__ = [
    "DeliveryError",
    "LoopsNotifier",
    "NullNotifier",
    "ZernioNotifier",
    "template_for",
]

LOOPS_API = "https://app.loops.so/api/v1/transactional"

#: Long enough for a slow provider, short enough that one message cannot hold a
#: sweep open. A send that times out is retried; a sweep that hangs is not.
TIMEOUT = httpx.Timeout(15.0)

logger = logging.getLogger(__name__)


class DeliveryError(AppError):
    """The message could not be handed to the provider.

    An ``AppError`` so the API layer can map it, though nothing raises it into a
    request yet — every send today comes from a scheduled producer, where the
    right response is a failed run rather than a status code.
    """


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
    # **The provider prefix is stripped here and nowhere else** (ADR 0025). It
    # exists so a message can move between senders by configuration alone, and
    # an adapter that received it would send a template id no provider knows.
    # A value without one is taken as-is, so a single-provider deployment need
    # not carry a prefix it has no use for.
    return template.split(":", 1)[-1]


class NullNotifier:
    """Records the intent and delivers nothing. The default.

    **Not a fake and not a no-op.** It logs what would have been sent, so the
    outbox can be drained end to end against a real database with the sends
    visible in the log — and so the absence of a provider is a line somebody can
    read rather than silence.

    It resolves no template, deliberately. Requiring one would make local
    development need a configured provider to exercise a path that sends
    nothing.
    """

    def send(
        self,
        *,
        notification: Notification,
        channel: Channel,
        to: str,
        variables: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        del variables, idempotency_key
        logger.info(
            "notification not sent (no provider configured): %s to %s over %s",
            notification,
            to,
            channel,
        )


class LoopsNotifier:
    """Transactional email through Loops (ADR 0025).

    **The outbox row's id is the idempotency key**, and Loops accepts one. That
    is what makes a retry after a timeout safe: the row exists exactly once per
    message per recipient, so replaying it replays the provider's answer rather
    than sending a second copy — which is the failure an outbox would otherwise
    be blamed for.

    **``addToAudience`` stays false, and it is not a default worth trusting to
    silence.** Setting it true would turn every transactional recipient into a
    Loops *contact*, and the free tier's cap is a thousand against roughly
    twelve hundred migrated users — so one flag would move the account onto a
    paid plan and quietly change what "transactional-only" means for
    unsubscribe handling.
    """

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._settings: Settings | None = None
        self._client = client or httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=TIMEOUT
        )

    def with_settings(self, settings: Settings) -> LoopsNotifier:
        """Bind the template map. Separate from construction because the key and
        the map arrive from the same place but are needed at different moments —
        the client once, the map per send."""
        self._settings = settings
        return self

    def send(
        self,
        *,
        notification: Notification,
        channel: Channel,
        to: str,
        variables: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        if channel is not Channel.EMAIL:
            raise DeliveryError(f"Loops carries email, not {channel}")
        if self._settings is None:  # pragma: no cover - wiring error, not a runtime one
            raise ConfigurationError("the Loops notifier has no template map")

        try:
            response = self._client.post(
                LOOPS_API,
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "email": to,
                    "transactionalId": template_for(self._settings, notification, channel),
                    # See the class docstring: true would make every recipient a
                    # contact and move the account onto a paid plan.
                    "addToAudience": False,
                    "dataVariables": variables,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Raised rather than swallowed: the drain records the failure and
            # retries, and a send that silently did nothing would leave the row
            # marked sent with nobody told.
            raise DeliveryError(f"loops refused {notification}: {exc}") from exc


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

    def send(
        self,
        *,
        notification: Notification,
        channel: Channel,
        to: str,
        variables: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        del to, variables, idempotency_key
        template_for(self._settings, notification, channel)
        raise NotImplementedError(
            "the Zernio adapter is not built — it needs the phone_* columns, an "
            "approved template per message, and the Facebook Business account resolved"
        )
