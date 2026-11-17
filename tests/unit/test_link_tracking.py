"""The pure rules behind profile link tracking (decisions 346, 347, 348, 353)."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api.deps.link_tracking import client_address
from app.domain.enums import LinkVia
from app.domain.link_tracking import (
    MAX_REFERRER_HOST,
    VISITOR_KEY_BYTES,
    DaySpan,
    day_span,
    guest_identity,
    member_identity,
    normalise_referrer_host,
    normalise_via,
    visitor_key,
)
from app.domain.mentor_stats import Window, trend_windows

#: One day's random key (`visitor_day_keys`).
DAY_KEY = b"s" * 32


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


def test_a_visitor_key_is_stable_for_one_visitor_mentor_and_day_key() -> None:
    mentor = uuid4()
    who = guest_identity("203.0.113.7", "Chrome")
    key = visitor_key(DAY_KEY, mentor, who)

    assert len(key) == VISITOR_KEY_BYTES
    assert visitor_key(DAY_KEY, mentor, who) == key
    # Another day has another random key, so the same visitor cannot be matched.
    assert visitor_key(b"t" * 32, mentor, who) != key
    assert visitor_key(DAY_KEY, uuid4(), who) != key
    assert visitor_key(DAY_KEY, mentor, guest_identity("203.0.113.7", "Safari")) != key


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


def _at(day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("since", "until", "expected"),
    [
        # Mid-day edges: a day counts in the window its midnight falls in, so
        # today (begun before `until`) is in and the first, part day is not.
        (_at(10, 9), _at(17, 9), DaySpan(dt.date(2026, 11, 11), dt.date(2026, 11, 18))),
        # Midnight edges: `since`'s day is in, `until`'s is not.
        (_at(10), _at(17), DaySpan(dt.date(2026, 11, 10), dt.date(2026, 11, 17))),
        # One microsecond past midnight is the next day's window.
        (
            _at(10) + dt.timedelta(microseconds=1),
            _at(17) + dt.timedelta(microseconds=1),
            DaySpan(dt.date(2026, 11, 11), dt.date(2026, 11, 18)),
        ),
        # Another zone is read in UTC: 23:30 on the 9th at -02:00 is 01:30 on
        # the 10th, so the 10th is a part day and the 11th the first.
        (
            dt.datetime(2026, 11, 9, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-2))),
            _at(17),
            DaySpan(dt.date(2026, 11, 11), dt.date(2026, 11, 17)),
        ),
    ],
)
def test_a_window_covers_the_utc_days_whose_midnight_is_in_it(
    since: dt.datetime, until: dt.datetime, expected: DaySpan
) -> None:
    span = day_span(Window(since, until))

    assert span == expected
    assert (span.until - span.since).days == (until - since).days


def test_adjacent_windows_cover_adjacent_days() -> None:
    current, previous = trend_windows(_at(17, 9), 7)

    assert day_span(previous).until == day_span(current).since


def test_a_naive_window_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        day_span(Window(dt.datetime(2026, 11, 10), dt.datetime(2026, 11, 17)))
