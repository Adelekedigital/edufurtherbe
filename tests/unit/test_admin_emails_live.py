"""Which emails the admin Emails pages call Live (decision 321).

An email is Live when its mapped Loops template is the shared one: it shows one
of the lines (`marks_shared`). Anything else, and anything unknown, reads only.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.api.deps import admin_emails
from app.core.config import Settings
from app.core.errors import UpstreamError
from app.domain.notifications import Notification

GRANTED = Notification.CREDITS_GRANTED
RENEWED = Notification.CREDITS_RENEWED


class Declared:
    def __init__(self, by_template: dict[str, set[str]], fail: bool = False) -> None:
        self.by_template = by_template
        self.fail = fail

    def declared(self, template: str) -> frozenset[str]:
        if self.fail:
            raise UpstreamError("Loops is down")
        return frozenset(self.by_template.get(template, set()))


def settings(**templates: str) -> Settings:
    return Settings(loops_api_key=SecretStr("k"), email_templates=templates)


def test_only_an_email_on_the_shared_template_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    found = Declared({"shared": {"emailSubject", "creditCount"}, "old": {"creditCount"}})
    monkeypatch.setattr(admin_emails, "template_variables", lambda _s: found)
    live = admin_emails._live(settings(credits_granted="shared", credits_renewed="old"))
    assert live == frozenset({GRANTED})


def test_an_email_with_no_template_is_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_emails, "template_variables", lambda _s: Declared({"shared": {"emailHeading"}})
    )
    assert admin_emails._live(settings(credits_renewed="shared")) == frozenset({RENEWED})


def test_no_provider_means_nothing_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_emails, "template_variables", lambda _s: None)
    assert admin_emails._live(settings(credits_granted="shared")) == frozenset()


def test_loops_down_is_an_upstream_error_not_an_email_not_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_emails, "template_variables", lambda _s: Declared({}, fail=True))
    with pytest.raises(UpstreamError):
        admin_emails._live(settings(credits_granted="shared"))
