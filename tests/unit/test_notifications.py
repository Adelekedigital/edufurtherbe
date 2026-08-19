"""The notification shape, without a provider or a database.

Nothing here sends anything — no provider is wired. What is tested is the
**contract the producer will be written against**: how a message is described,
how a template is resolved, and that an unbuilt adapter refuses loudly rather
than reporting success.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.domain.notifications import Channel, Notification
from app.infra.clients.notifications import (
    EmailitNotifier,
    Message,
    NullNotifier,
    ZernioNotifier,
    template_for,
)

REMINDER = Notification.MENTOR_RESPONSE_REMINDER


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def a_message(channel: Channel = Channel.EMAIL) -> Message:
    return Message(notification=REMINDER, channel=channel, to="mentor@example.test", variables={})


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_a_template_map_accepts_json() -> None:
    """What `.env.example` documents and what an existing configuration uses."""
    configured = settings(email_templates=f'{{"{REMINDER}": "tmpl_1"}}')

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_1"


def test_a_template_map_accepts_key_value_pairs() -> None:
    """**What a person types into a cloud console**, which gives no hint a map
    is wanted.

    `cors_origins` supports both spellings for this reason, and the comment
    beside it records the cost of not doing so: a `SettingsError` naming neither
    the value nor the expected shape, which took a deploy to diagnose.
    """
    configured = settings(email_templates=f"{REMINDER}=tmpl_2")

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_2"


def test_a_real_mapping_from_code_is_left_alone() -> None:
    """Disabling the decoder must not also disable the type check — the tests
    and `create_app` pass real mappings, not strings."""
    configured = settings(email_templates={str(REMINDER): "tmpl_3"})

    assert template_for(configured, REMINDER, Channel.EMAIL) == "tmpl_3"


def test_the_two_channels_are_separate_maps() -> None:
    """**Not one map with two columns.** A message exists on one channel long
    before the other — email reaches everybody today and WhatsApp reaches
    nobody — and a single map would make the absent half look like a mistake
    rather than a phase."""
    configured = settings(email_templates={str(REMINDER): "email_tmpl"})

    assert template_for(configured, REMINDER, Channel.EMAIL) == "email_tmpl"
    with pytest.raises(ConfigurationError):
        template_for(configured, REMINDER, Channel.WHATSAPP)


def test_a_missing_template_is_an_operator_fault_not_a_fallback() -> None:
    """**Raises rather than falling back**, because a missing template has no
    sensible default: sending the wrong message is worse than sending none, and
    silently sending nothing would make a channel look configured when it is
    not.

    `ConfigurationError` specifically — it maps to a 500 with its detail
    withheld, because it names settings and a caller learning which one is
    missing learns what this service is wired to.
    """
    with pytest.raises(ConfigurationError) as raised:
        template_for(settings(), REMINDER, Channel.EMAIL)

    assert str(REMINDER) in str(raised.value)


# --------------------------------------------------------------------------
# The adapters
# --------------------------------------------------------------------------


def test_the_default_notifier_delivers_nothing_and_says_so() -> None:
    """**Not a fake.** It records the intent so a producer can be run end to end
    with the sends visible in the log, and so the absence of a provider is a
    line somebody can read rather than silence.

    It resolves no template, deliberately: requiring one would make local
    development need a configured provider to exercise a path that sends
    nothing.

    **Captured off the emitting logger rather than through `caplog`.** `caplog`
    reads records from the handler it installs on the *root* logger, and by the
    time the whole suite has run there are two `LogCaptureHandler`s on root with
    the level left at `WARNING` — so an `INFO` record reaches a handler that this
    fixture is not the reader of, and `caplog.text` is empty. It passed in
    isolation and failed in the suite, which is the signature of shared state
    rather than of the code under test.

    Attaching to `app.infra.clients.notifications` depends on neither the root
    level nor which handlers are on it. This is the suite's only `caplog` user,
    so nothing else was relying on the behaviour that broke.
    """
    emitter = logging.getLogger("app.infra.clients.notifications")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect(level=logging.INFO)
    restore = emitter.level
    emitter.addHandler(handler)
    emitter.setLevel(logging.INFO)
    try:
        NullNotifier().send(a_message())
    finally:
        emitter.removeHandler(handler)
        emitter.setLevel(restore)

    logged = "\n".join(record.getMessage() for record in records)
    assert "not sent" in logged
    assert "mentor@example.test" in logged


@pytest.mark.parametrize("adapter", [EmailitNotifier, ZernioNotifier])
def test_an_unbuilt_adapter_refuses_rather_than_reporting_success(adapter: type) -> None:
    """**A stub that returned success would make the first real send the first
    time anybody discovered the shape was wrong** — and the producer above it
    would look correct while delivering nothing.

    Both are configured here, so what is asserted is the refusal rather than a
    missing template incidentally raising first.
    """
    configured = settings(
        email_templates={str(REMINDER): "e"}, whatsapp_templates={str(REMINDER): "w"}
    )
    channel = Channel.EMAIL if adapter is EmailitNotifier else Channel.WHATSAPP

    with pytest.raises(NotImplementedError):
        adapter(configured).send(a_message(channel))


@pytest.mark.parametrize("adapter", [EmailitNotifier, ZernioNotifier])
def test_an_unbuilt_adapter_still_fails_on_a_missing_template_first(adapter: type) -> None:
    """The configuration error comes before the not-built one, so an operator
    setting a provider up learns what is missing rather than being told the
    adapter does not exist — which they would then have no reason to doubt."""
    channel = Channel.EMAIL if adapter is EmailitNotifier else Channel.WHATSAPP

    with pytest.raises(ConfigurationError):
        adapter(settings()).send(a_message(channel))
