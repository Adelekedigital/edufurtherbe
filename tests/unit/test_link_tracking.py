"""The pure rules behind profile link tracking (decisions 346, 347)."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api.deps.link_tracking import client_address
from app.core.config import Settings
from app.domain.enums import LinkVia
from app.domain.link_tracking import (
    MAX_REFERRER_HOST,
    VISITOR_KEY_BYTES,
    guest_identity,
    member_identity,
    normalise_referrer_host,
    normalise_via,
    visitor_key,
)
from app.main import create_app

SECRET = b"s" * 32
DAY = dt.date(2026, 11, 23)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("copy", LinkVia.COPY),
        (" Share ", LinkVia.SHARE),
        ("INVITE", LinkVia.INVITE),
        ("newsletter", None),
        ("", None),
        (None, None),
    ],
)
def test_only_our_own_channels_survive(raw: str | None, expected: LinkVia | None) -> None:
    assert normalise_via(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("l.instagram.com", "l.instagram.com"),
        ("wa.me", "wa.me"),
        ("WWW.LinkedIn.com", "linkedin.com"),
        ("linkedin.com:443", "linkedin.com"),
        ("https://www.google.com/search?q=x", "google.com"),
        ("user:pw@t.co", "t.co"),
        ("example.com.", "example.com"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        (" news.ycombinator.com ", "news.ycombinator.com"),
        ("localhost", None),
        ("203.0.113.7", None),
        ("[2001:db8::1]", None),
        ("bücher.example", None),
        ("bad host.com", None),
        ("-bad.com", None),
        ("<script>.com", None),
        ("", None),
        (None, None),
        ("a" * 64 + ".com", None),
        (".".join(["a" * 63] * 4), None),
    ],
)
def test_a_referrer_is_a_bare_host_or_nothing(raw: str | None, expected: str | None) -> None:
    assert normalise_referrer_host(raw) == expected


def test_the_longest_host_dns_allows_is_kept() -> None:
    host = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    assert len(host) == MAX_REFERRER_HOST
    assert normalise_referrer_host(host) == host
    assert normalise_referrer_host("e" + host) is None


def test_a_visitor_key_is_stable_for_one_visitor_mentor_and_day() -> None:
    mentor = uuid4()
    who = guest_identity("203.0.113.7", "Chrome")
    key = visitor_key(SECRET, DAY, mentor, who)

    assert len(key) == VISITOR_KEY_BYTES
    assert visitor_key(SECRET, DAY, mentor, who) == key
    assert visitor_key(SECRET, DAY + dt.timedelta(days=1), mentor, who) != key
    assert visitor_key(SECRET, DAY, uuid4(), who) != key
    assert visitor_key(SECRET, DAY, mentor, guest_identity("203.0.113.7", "Safari")) != key
    assert visitor_key(b"t" * 32, DAY, mentor, who) != key


def test_a_member_and_a_guest_never_share_a_key() -> None:
    member = uuid4()
    assert member_identity(member) != guest_identity(str(member), "")
    assert member_identity(member) == member_identity(member)


def _request(headers: dict[str, str], client: tuple[str, int] | None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": client})


@pytest.mark.parametrize(
    ("headers", "client", "expected"),
    [
        ({"X-Forwarded-For": "203.0.113.7, 76.76.21.1"}, ("10.0.0.1", 1), "203.0.113.7"),
        ({"X-Forwarded-For": " "}, ("10.0.0.1", 1), "10.0.0.1"),
        ({}, ("10.0.0.1", 1), "10.0.0.1"),
        ({}, None, ""),
    ],
)
def test_a_guest_is_the_first_forwarded_hop_else_the_peer(
    headers: dict[str, str], client: tuple[str, int] | None, expected: str
) -> None:
    assert client_address(_request(headers, client)) == expected


def test_the_visitor_secret_is_the_setting_else_shared_by_every_process() -> None:
    """Unset, every process of a deployment derives the same key from the
    database URL, never the URL itself; with no database, a random one."""
    dsn = "postgresql://u:pw@db.example:5432/app"
    set_one = Settings(_env_file=None, visitor_key_secret="v" * 32, database_url=dsn)
    derived = Settings(_env_file=None, database_url=dsn).visitor_secret()

    assert set_one.visitor_secret() == b"v" * 32
    assert derived == Settings(_env_file=None, database_url=dsn).visitor_secret()
    assert len(derived) == 32
    assert b"pw" not in derived
    assert derived != Settings(_env_file=None, database_url=dsn + "2").visitor_secret()
    alone = Settings(_env_file=None)
    assert alone.visitor_secret() != alone.visitor_secret()


def test_the_app_keys_visitors_with_the_configured_secret() -> None:
    app = create_app(Settings(_env_file=None, visitor_key_secret="w" * 32))

    assert app.state.visitor_key_secret == b"w" * 32
