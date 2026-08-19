"""The audience rule, the template map, and what Loops is actually asked.

**The audience rule is the part worth testing hardest**, because getting it
wrong is invisible: telling both parties everything looks like generosity and
reads as noise, and telling the wrong one looks like nothing at all. So every
case asserts **who was not told** as well as who was.

The adapter is driven against `httpx.MockTransport`, so these assert the
*request* rather than the return value — the half that has gone wrong twice in
this codebase's integrations, silently both times.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.domain.notifications import AUDIENCE, Channel, Notification, recipients
from app.infra.clients.notifications import (
    DeliveryError,
    LoopsNotifier,
    NullNotifier,
    ZernioNotifier,
    template_for,
)

MENTOR = UUID("00000000-0000-4000-8000-00000000000a")
MENTEE = UUID("00000000-0000-4000-8000-00000000000b")
REMINDER = Notification.MENTOR_RESPONSE_REMINDER


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def told(notification: Notification, actor: UUID | None = None) -> tuple[UUID, ...]:
    return recipients(notification, mentor_id=MENTOR, mentee_id=MENTEE, actor_id=actor)


# --------------------------------------------------------------------------
# Who hears it
# --------------------------------------------------------------------------


def test_an_auto_confirming_booking_tells_only_the_mentor() -> None:
    """**The mentee is looking at a confirmation screen.** Telling them too is
    the legacy behaviour, and it tells somebody something they already know."""
    assert told(Notification.SESSION_BOOKED) == (MENTOR,)


def test_an_acceptance_tells_only_the_mentee() -> None:
    """**The same rule, applied the other way** — the mentor has just clicked
    the button, and the mentee has been waiting on exactly this answer. These
    two cases are why the rule is stated as *who did not act* rather than as a
    list of who gets what."""
    assert told(Notification.REQUEST_ACCEPTED) == (MENTEE,)


@pytest.mark.parametrize(
    ("notification", "expected"),
    [
        (Notification.SESSION_REQUESTED, (MENTOR,)),
        (Notification.REQUEST_DECLINED, (MENTEE,)),
        (Notification.REQUEST_WITHDRAWN, (MENTOR,)),
    ],
)
def test_the_rest_follow_from_who_acted(
    notification: Notification, expected: tuple[UUID, ...]
) -> None:
    assert told(notification) == expected


def test_a_cancellation_tells_whichever_party_did_not_do_it() -> None:
    """**The only action either party may take**, and therefore the only member
    whose audience is not knowable from the member alone. That is the whole
    reason `recipients` takes an actor."""
    assert told(Notification.SESSION_CANCELLED, actor=MENTOR) == (MENTEE,)
    assert told(Notification.SESSION_CANCELLED, actor=MENTEE) == (MENTOR,)


def test_an_expiry_tells_both_because_nobody_acted() -> None:
    """**The rule's one exception, and it is principled rather than forgotten.**
    The mentor let it lapse and the mentee has been waiting on an answer that is
    no longer coming — neither of them knows."""
    assert set(told(Notification.REQUEST_EXPIRED)) == {MENTOR, MENTEE}


def test_a_cancellation_with_no_actor_tells_both() -> None:
    """Unreachable through the API, where cancelling requires a caller. It is
    the right answer if anything reaches it: telling both is a message somebody
    did not need, where telling neither is a session called off in silence."""
    assert set(told(Notification.SESSION_CANCELLED)) == {MENTOR, MENTEE}


def test_a_message_that_is_not_about_a_session_refuses_to_guess() -> None:
    """`MENTOR_APPROVED` has no mentor and mentee to choose between. Raising
    beats returning nothing, which would let a caller send it to an empty
    audience and never notice."""
    with pytest.raises(KeyError):
        told(Notification.MENTOR_APPROVED)


def test_every_session_message_has_an_audience() -> None:
    """The guard against adding a member and forgetting the rule. A message with
    no entry raises at send time, which is the worst moment to discover it."""
    unmapped = {
        member
        for member in Notification
        if member not in AUDIENCE
        and member not in {Notification.MENTOR_APPROVED, Notification.MENTOR_DECLINED}
    }

    assert unmapped == set()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_a_template_map_accepts_json() -> None:
    """What `.env.example` documents and what an existing configuration uses."""
    configured = settings(email_templates=f'{{"{REMINDER}": "tmpl_1"}}')

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_1"


def test_a_template_map_accepts_key_value_pairs() -> None:
    """**What a person types into a cloud console**, which gives no hint a map
    is wanted. `cors_origins` supports both spellings for this reason, and its
    comment records what not doing so cost: a `SettingsError` naming neither the
    value nor the shape, diagnosed over a deploy."""
    configured = settings(email_templates=f"{REMINDER}=tmpl_2")

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_2"


def test_the_provider_prefix_is_stripped() -> None:
    """**ADR 0025's incremental path out**, and it has to be stripped exactly
    here: an adapter receiving `loops:tmpl_abc` would send a template id no
    provider knows, and the failure would look like a bad template rather than
    a bad prefix."""
    configured = settings(email_templates={str(REMINDER): "loops:tmpl_abc"})

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_abc"


def test_a_value_with_no_prefix_is_taken_as_it_is() -> None:
    """A single-provider deployment should not have to carry a prefix it has no
    use for."""
    configured = settings(email_templates={str(REMINDER): "tmpl_plain"})

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_plain"


def test_the_two_channels_are_separate_maps() -> None:
    """**Not one map with two columns.** A message exists on email long before
    WhatsApp — nobody has a phone number — and a single map would make the
    absent half look like a mistake rather than a phase."""
    configured = settings(email_templates={str(REMINDER): "email_tmpl"})

    assert template_for(configured, REMINDER, Channel.EMAIL) == "email_tmpl"
    with pytest.raises(ConfigurationError):
        template_for(configured, REMINDER, Channel.WHATSAPP)


def test_a_missing_template_is_an_operator_fault_not_a_fallback() -> None:
    """Sending the wrong message is worse than sending none, and a silent no-op
    would make a channel look configured when it is not."""
    with pytest.raises(ConfigurationError) as raised:
        template_for(settings(), REMINDER, Channel.EMAIL)

    assert str(REMINDER) in str(raised.value)


# --------------------------------------------------------------------------
# The adapters
# --------------------------------------------------------------------------


def loops(handler: Any) -> tuple[LoopsNotifier, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    configured = settings(email_templates={str(REMINDER): "loops:tmpl_abc"})
    return LoopsNotifier("key", client=client).with_settings(configured), seen


def send(notifier: LoopsNotifier, key: str = "outbox-row-id") -> None:
    notifier.send(
        notification=REMINDER,
        channel=Channel.EMAIL,
        to="mentor@example.test",
        variables={"first_name": "Ada"},
        idempotency_key=key,
    )


def test_a_send_carries_the_template_the_recipient_and_the_variables() -> None:
    notifier, seen = loops(lambda _: httpx.Response(200, json={"success": True}))

    send(notifier)

    (request,) = seen
    body = json.loads(request.content)
    assert body["transactionalId"] == "tmpl_abc"
    assert body["email"] == "mentor@example.test"
    assert body["dataVariables"] == {"first_name": "Ada"}


def test_the_outbox_row_id_is_the_idempotency_key() -> None:
    """**What makes a retry after a timeout safe.** The row exists exactly once
    per message per recipient, so replaying it replays the provider's answer
    rather than sending a second copy — the failure an outbox would otherwise be
    blamed for."""
    notifier, seen = loops(lambda _: httpx.Response(200, json={"success": True}))
    # Generated rather than written out: a UUID literal here reads as a
    # credential to the secret scanner, correctly enough that arguing with it
    # costs more than not having one.
    row_id = str(uuid4())

    send(notifier, key=row_id)

    assert seen[0].headers["Idempotency-Key"] == row_id


def test_a_recipient_never_becomes_a_contact() -> None:
    """**One flag, and it decides the bill.** `addToAudience: true` would turn
    every transactional recipient into a Loops contact, and the free tier caps
    contacts at a thousand against roughly twelve hundred migrated users — so it
    would move the account onto a paid plan and quietly change what
    "transactional-only" means for unsubscribe handling."""
    notifier, seen = loops(lambda _: httpx.Response(200, json={"success": True}))

    send(notifier)

    assert json.loads(seen[0].content)["addToAudience"] is False


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, json={"error": "boom"}),
        lambda _: httpx.Response(401, json={"error": "bad key"}),
    ],
    ids=["server-error", "bad-key"],
)
def test_a_refusal_is_raised_so_the_drain_can_retry(handler: Any) -> None:
    """Swallowing it would leave the row marked sent with nobody told, which is
    the one outcome the outbox exists to make impossible."""
    notifier, _ = loops(handler)

    with pytest.raises(DeliveryError):
        send(notifier)


def test_a_network_failure_is_the_same_answer() -> None:
    """No response at all, which `raise_for_status` never sees."""

    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    notifier, _ = loops(refuse)

    with pytest.raises(DeliveryError):
        send(notifier)


def test_loops_refuses_a_channel_it_does_not_carry() -> None:
    """It is the email sender. Sending a WhatsApp message through it would
    deliver an email to a phone number."""
    notifier, _ = loops(lambda _: httpx.Response(200, json={"success": True}))

    with pytest.raises(DeliveryError):
        notifier.send(
            notification=REMINDER,
            channel=Channel.WHATSAPP,
            to="+2348000000000",
            variables={},
            idempotency_key=str(uuid4()),
        )


def test_the_default_notifier_delivers_nothing_and_raises_nothing() -> None:
    """The state of a system with no provider configured, and every path above
    it has to keep working. It resolves no template either — requiring one would
    make local development need a configured provider to exercise a code path
    that sends nothing."""
    assert (
        NullNotifier().send(
            notification=REMINDER,
            channel=Channel.EMAIL,
            to="somebody@example.test",
            variables={},
            idempotency_key="k",
        )
        is None
    )


def test_the_whatsapp_adapter_refuses_rather_than_reporting_success() -> None:
    """A stub that returned success would make the first real send the first
    time anybody discovered the shape was wrong — and the drain would mark the
    row sent with nobody told."""
    configured = settings(whatsapp_templates={str(REMINDER): "wa_tmpl"})

    with pytest.raises(NotImplementedError):
        ZernioNotifier(configured).send(
            notification=REMINDER,
            channel=Channel.WHATSAPP,
            to="+2348000000000",
            variables={},
            idempotency_key="k",
        )
