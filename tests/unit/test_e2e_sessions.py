"""E2E sessions: made only for seeded `.test` accounts, only on staging.

`scripts/e2e_session.py` hands out real Supabase sessions, so each lock is
tested refusing on its own, and the positive case is tested too: a lock that
refuses everything would pass every negative test.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.infra.auth.e2e_sessions import (
    GENERATE_LINK,
    VERIFY,
    E2ESessionClient,
    NotAnE2EAccountError,
    refuse_e2e_session,
)
from app.infra.db.demo_seed import DEMO_DOMAIN, E2E_ACCOUNTS, E2ERunError, e2e_email, e2e_ref

#: A made-up project ref, shaped like Supabase's (16+ lowercase alphanumerics).
REF = "abcdefghijklmnopqrst"
OTHER = "zyxwvutsrqponmlkjihg"
#: Built at runtime so no key-shaped literal lands in the repository.
KEY = "-".join(["not", "a", "real", "key"])


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "staging",
        "e2e_allow": True,
        "e2e_supabase_project": REF,
        "supabase_url": f"https://{REF}.supabase.co",
        "supabase_service_role_key": KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_staging_with_every_lock_open_may_make_a_session() -> None:
    assert refuse_e2e_session(settings(), "staging") is None


@pytest.mark.parametrize(
    ("overrides", "declared"),
    [
        pytest.param({"e2e_allow": False}, "staging", id="E2E_ALLOW off"),
        pytest.param({}, "production", id="declared production"),
        pytest.param({"environment": "production"}, "staging", id="settings say production"),
        pytest.param({"environment": "local"}, "staging", id="settings say local"),
        pytest.param({"e2e_supabase_project": None}, "staging", id="no project set"),
        pytest.param({"e2e_supabase_project": "  "}, "staging", id="blank project"),
        pytest.param({"e2e_supabase_project": OTHER}, "staging", id="another project"),
        pytest.param({"supabase_url": None}, "staging", id="no Supabase"),
    ],
)
def test_each_lock_refuses_on_its_own(overrides: dict[str, Any], declared: str) -> None:
    assert refuse_e2e_session(settings(**overrides), declared) is not None


def test_an_environment_nobody_set_is_refused() -> None:
    """The default `local` must not pass as a chosen environment."""
    values = {"e2e_allow": True, "e2e_supabase_project": REF}
    unset = Settings(  # type: ignore[call-arg]
        _env_file=None,
        supabase_url=f"https://{REF}.supabase.co",
        supabase_service_role_key=KEY,
        **values,
    )
    assert "environment" not in unset.model_fields_set
    assert refuse_e2e_session(unset, "staging") is not None


def test_a_refusal_never_names_the_project() -> None:
    why = refuse_e2e_session(settings(e2e_supabase_project=OTHER), "staging") or ""
    assert REF not in why
    assert OTHER not in why


def test_the_allowlist_is_every_account_of_any_run_on_the_demo_domain() -> None:
    for ref in E2E_ACCOUNTS:
        assert e2e_ref(e2e_email(ref, "r1")) == ref
    assert e2e_email("mentee_new", "r1") == f"e2e-r1-mentee-new@{DEMO_DOMAIN}"
    assert e2e_ref(f"E2E-R1-MENTEE-NEW@{DEMO_DOMAIN.upper()}") == "mentee_new"


@pytest.mark.parametrize("run", ["", "R1", "a-b", "x" * 17, "r1@evil.test"])
def test_a_malformed_run_tag_is_refused(run: str) -> None:
    with pytest.raises(E2ERunError):
        e2e_email("mentee_new", run)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.path, body))
        if request.url.path == GENERATE_LINK:
            return httpx.Response(200, json={"action_link": "L", "hashed_token": "H"})
        if request.url.path == VERIFY:
            return httpx.Response(200, json={"access_token": "T"})
        return httpx.Response(200, json={"id": "019ff5c8-bb86-7f52-b01f-026ad188b66e", **body})


def client(recorder: Recorder) -> E2ESessionClient:
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return E2ESessionClient(
        base_url=f"https://{REF}.supabase.co", service_role_key=KEY, client=http
    )


def test_an_allowlisted_account_gets_a_link_and_a_token() -> None:
    recorder = Recorder()
    sessions = client(recorder)
    link = sessions.magic_link(
        e2e_email("mentee_with_credits", "r1"), redirect_to="https://x.test/cb"
    )
    assert (link.action_link, link.hashed_token) == ("L", "H")
    assert sessions.access_token(link) == "T"
    assert recorder.calls == [
        (
            GENERATE_LINK,
            {
                "type": "magiclink",
                "email": e2e_email("mentee_with_credits", "r1"),
                "redirect_to": "https://x.test/cb",
            },
        ),
        (VERIFY, {"type": "magiclink", "token_hash": "H"}),
    ]


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("someone@example.com", id="outside the domain"),
        pytest.param(f"demo-00-amara@{DEMO_DOMAIN}", id="a demo mentor, not an E2E account"),
        pytest.param(f"e2e-r1-unknown@{DEMO_DOMAIN}", id="an E2E-shaped address for no account"),
        pytest.param(f"e2e-r1-mentee-new@{DEMO_DOMAIN}.evil.test", id="the domain as a prefix"),
        pytest.param(f"x.e2e-r1-mentee-new@{DEMO_DOMAIN}", id="the pattern inside an address"),
    ],
)
def test_anything_else_is_refused_before_a_request(address: str) -> None:
    recorder = Recorder()
    sessions = client(recorder)
    with pytest.raises(NotAnE2EAccountError):
        sessions.magic_link(address)
    with pytest.raises(NotAnE2EAccountError):
        sessions.ensure_account(address)
    assert recorder.calls == []
