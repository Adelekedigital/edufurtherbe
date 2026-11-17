"""`GET /me/mentor-stats/profile-link`: a mentor's own link figures (decision 353).

Counts only, by UTC day, over decision 246's windows. The figures are tested at
the store with fixed days, the contract through the API. **Two mentors wherever
a scope is under test**, for the reason `test_api_me_mentor_stats` gives.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import make_public_mentor, token_for

from app.api.deps import PRIVATE_NO_STORE
from app.domain.enums import LinkVia, ShareMethod
from app.domain.link_tracking import TOP_REFERRERS, DaySpan, day_span
from app.domain.mentor_stats import trend_windows
from app.infra.db.link_stats import link_stats
from app.infra.db.link_tracking import record_share, record_visit
from conftest import add_user, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/mentor-stats/profile-link"

CURRENT = DaySpan(dt.date(2026, 11, 10), dt.date(2026, 11, 17))
PREVIOUS = DaySpan(dt.date(2026, 11, 3), dt.date(2026, 11, 10))

#: Every channel, in the order the response lists them: ours, then none.
NO_VISITS = [{"key": via, "count": 0} for via in [*LinkVia, None]]
NO_SHARES = [{"key": method, "count": 0} for method in ShareMethod]


def by_via(**counts: int) -> list[dict[str, Any]]:
    """The channel list with ``counts`` (`none` for no channel), the rest zero."""
    return [
        {"key": via, "count": counts.get(via.value if via else "none", 0)}
        for via in [*LinkVia, None]
    ]


def by_method(**counts: int) -> list[dict[str, Any]]:
    return [{"key": method, "count": counts.get(method.value, 0)} for method in ShareMethod]


async def visit(
    engine: AsyncEngine,
    mentor: UUID,
    day: dt.date,
    *,
    via: LinkVia | None = None,
    referrer: str | None = None,
    times: int = 1,
) -> None:
    """``times`` distinct visitors to ``mentor`` on ``day``, through the writer."""
    async with async_sessionmaker(engine)() as session:
        for _ in range(times):
            await record_visit(
                session,
                mentor_id=mentor,
                day=day,
                visitor_key=secrets.token_bytes(32),
                signed_in=False,
                via=via,
                referrer_host=referrer,
            )
        await session.commit()


async def share(
    engine: AsyncEngine, mentor: UUID, day: dt.date, method: ShareMethod, *, own: bool = False
) -> None:
    async with async_sessionmaker(engine)() as session:
        await record_share(
            session,
            mentor_id=mentor,
            day=day,
            visitor_key=secrets.token_bytes(32),
            signed_in=True,
            method=method,
            own=own,
        )
        await session.commit()


async def figures(engine: AsyncEngine, mentor: UUID) -> list[dict[str, Any]]:
    async with async_sessionmaker(engine)() as session:
        return await link_stats(session, mentor, [CURRENT, PREVIOUS])


# --------------------------------------------------------------------------
# The figures, at the store, where the days are ours
# --------------------------------------------------------------------------


async def test_visits_count_by_channel_and_shares_by_method(db_engine: AsyncEngine) -> None:
    """Every channel and method is listed, zeros included, and the totals are
    their sums; each window counts its own days."""
    mentor = await make_public_mentor(db_engine, "links-figures")
    day = CURRENT.since + dt.timedelta(days=2)
    await visit(db_engine, mentor, day, via=LinkVia.SHARE, times=3)
    await visit(db_engine, mentor, day, via=LinkVia.COPY)
    await visit(db_engine, mentor, day, times=2)
    await visit(db_engine, mentor, PREVIOUS.since, via=LinkVia.INVITE)
    await share(db_engine, mentor, day, ShareMethod.NATIVE)
    await share(db_engine, mentor, day, ShareMethod.NATIVE)
    await share(db_engine, mentor, day, ShareMethod.COPY)

    current, previous = await figures(db_engine, mentor)

    assert (current["since"], current["until"]) == CURRENT
    assert (previous["since"], previous["until"]) == PREVIOUS
    assert current["visits"] == 6
    assert current["visits_by_via"] == by_via(share=3, copy=1, none=2)
    assert current["shares"] == 3
    assert current["shares_by_method"] == by_method(native=2, copy=1)
    assert previous["visits"] == 1
    assert previous["visits_by_via"] == by_via(invite=1)
    assert previous["shares"] == 0
    assert previous["shares_by_method"] == NO_SHARES


async def test_the_mentors_own_shares_are_not_counted(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "links-own")
    await share(db_engine, mentor, CURRENT.since, ShareMethod.COPY, own=True)
    await share(db_engine, mentor, CURRENT.since, ShareMethod.NATIVE, own=True)
    await share(db_engine, mentor, CURRENT.since, ShareMethod.NATIVE)

    current, _ = await figures(db_engine, mentor)

    assert current["shares"] == 1
    assert current["shares_by_method"] == by_method(native=1)


async def test_a_window_includes_its_first_day_and_not_its_until_day(
    db_engine: AsyncEngine,
) -> None:
    """`CURRENT.until` is outside both; `PREVIOUS.until` is `CURRENT.since`, so
    a visit on it is the current window's only."""
    mentor = await make_public_mentor(db_engine, "links-edges")
    await visit(db_engine, mentor, CURRENT.since, via=LinkVia.COPY, referrer="wa.me")
    await visit(db_engine, mentor, CURRENT.until, via=LinkVia.SHARE, referrer="x.com")
    await visit(db_engine, mentor, PREVIOUS.since - dt.timedelta(days=1), referrer="x.com")
    await share(db_engine, mentor, CURRENT.since, ShareMethod.COPY)
    await share(db_engine, mentor, CURRENT.until, ShareMethod.NATIVE)
    await share(db_engine, mentor, PREVIOUS.since - dt.timedelta(days=1), ShareMethod.NATIVE)

    current, previous = await figures(db_engine, mentor)

    assert current["visits_by_via"] == by_via(copy=1)
    assert current["top_referrers"] == [{"host": "wa.me", "count": 1}]
    assert current["shares_by_method"] == by_method(copy=1)
    assert previous["visits_by_via"] == NO_VISITS
    assert previous["top_referrers"] == []
    assert previous["shares_by_method"] == NO_SHARES


async def test_top_referrers_are_the_most_visited_hosts_first(db_engine: AsyncEngine) -> None:
    """Most visits first, a tie by host name; at most `TOP_REFERRERS`; a visit
    with no referrer is in the totals and in no host's count."""
    mentor = await make_public_mentor(db_engine, "links-referrers")
    day = CURRENT.since
    await visit(db_engine, mentor, day, referrer="wa.me", times=3)
    await visit(db_engine, mentor, day, referrer="l.instagram.com", times=2)
    await visit(db_engine, mentor, day, referrer="linkedin.com", times=2)
    for n in range(TOP_REFERRERS):
        await visit(db_engine, mentor, day, referrer=f"site-{n:02}.example")
    await visit(db_engine, mentor, day, times=4)

    current, _ = await figures(db_engine, mentor)

    assert current["visits"] == 3 + 2 + 2 + TOP_REFERRERS + 4
    assert current["top_referrers"] == [
        {"host": "wa.me", "count": 3},
        {"host": "l.instagram.com", "count": 2},
        {"host": "linkedin.com", "count": 2},
        *({"host": f"site-{n:02}.example", "count": 1} for n in range(TOP_REFERRERS - 3)),
    ]


async def test_a_tie_at_the_cut_is_decided_by_byte_order_not_collation(
    db_engine: AsyncEngine,
) -> None:
    """`a-z.io` sorts before `ab.io` byte by byte (`-` < `b`), though a
    language collation that skips punctuation puts `ab.io` first; the cut and
    the listed order follow the byte order either way."""
    mentor = await make_public_mentor(db_engine, "links-collation")
    day = CURRENT.since
    for n in range(TOP_REFERRERS - 1):
        await visit(db_engine, mentor, day, referrer=f"busy-{n}.example", times=2)
    await visit(db_engine, mentor, day, referrer="ab.io")
    await visit(db_engine, mentor, day, referrer="a-z.io")

    current, _ = await figures(db_engine, mentor)

    assert [row["host"] for row in current["top_referrers"]] == [
        *(f"busy-{n}.example" for n in range(TOP_REFERRERS - 1)),
        "a-z.io",
    ]


async def test_only_the_mentors_own_rows_count(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "links-mine")
    other = await make_public_mentor(db_engine, "links-theirs")
    await visit(db_engine, mentor, CURRENT.since, via=LinkVia.COPY, referrer="wa.me")
    await visit(db_engine, other, CURRENT.since, via=LinkVia.COPY, referrer="wa.me", times=5)
    await visit(db_engine, other, CURRENT.since, referrer="x.com")
    await share(db_engine, other, CURRENT.since, ShareMethod.NATIVE)

    current, previous = await figures(db_engine, mentor)

    assert current["visits_by_via"] == by_via(copy=1)
    assert current["top_referrers"] == [{"host": "wa.me", "count": 1}]
    assert current["shares_by_method"] == NO_SHARES
    assert previous["visits"] == 0


async def test_every_window_is_read_in_one_statement(db_engine: AsyncEngine) -> None:
    mentor = await make_public_mentor(db_engine, "links-snapshot")
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        rows = await figures(db_engine, mentor)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)

    assert len(statements) == 1, statements
    assert len(rows) == 2


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


async def test_a_mentor_reads_their_counts_and_nothing_per_visitor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A visit recorded through the public write is read back here: 90 days by
    default, today included, counts only."""
    mentor = await make_public_mentor(db_engine, "links-api")
    other = await make_public_mentor(db_engine, "links-api-other")
    started = dt.datetime.now(dt.UTC)
    for handle in (mentor, other):
        recorded = await api_client.post(
            f"/api/v1/mentors/{handle}/visits",
            json={"via": "share", "referrer_host": "https://www.WA.me/x"},
            headers={"User-Agent": "test", "X-Forwarded-For": "203.0.113.9"},
        )
        assert recorded.status_code == 204, recorded.text
    await share(db_engine, mentor, started.date(), ShareMethod.COPY)

    response = await api_client.get(URL, headers=bearer(await token_for(db_engine, mentor)))

    if dt.datetime.now(dt.UTC).date() != started.date():
        pytest.skip("the run crossed UTC midnight, so the window moved under it")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == PRIVATE_NO_STORE
    body = response.json()
    current, previous = day_span_pair(started)
    assert body["window_days"] == 90
    assert (body["current"]["since"], body["current"]["until"]) == current
    assert (body["previous"]["since"], body["previous"]["until"]) == previous
    assert body["current"] == {
        "since": current[0],
        "until": current[1],
        "visits": 1,
        "visits_by_via": json_via(share=1),
        "top_referrers": [{"host": "wa.me", "count": 1}],
        "shares": 1,
        "shares_by_method": json_method(copy=1),
    }
    assert body["previous"]["visits"] == 0


def day_span_pair(now: dt.datetime, days: int = 90) -> tuple[tuple[str, str], tuple[str, str]]:
    current, previous = (day_span(window) for window in trend_windows(now, days))
    return (
        (current.since.isoformat(), current.until.isoformat()),
        (previous.since.isoformat(), previous.until.isoformat()),
    )


def json_via(**counts: int) -> list[dict[str, Any]]:
    return [
        {"key": row["key"] and row["key"].value, "count": row["count"]} for row in by_via(**counts)
    ]


def json_method(**counts: int) -> list[dict[str, Any]]:
    return [{"key": row["key"].value, "count": row["count"]} for row in by_method(**counts)]


async def test_a_year_is_its_own_days(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "links-year")
    year = dt.datetime.now(dt.UTC).year - 1

    response = await api_client.get(
        f"{URL}?year={year}", headers=bearer(await token_for(db_engine, mentor))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["current"]["since"], body["current"]["until"]) == (
        f"{year}-01-01",
        f"{year + 1}-01-01",
    )
    assert (body["previous"]["since"], body["previous"]["until"]) == (
        f"{year - 1}-01-01",
        f"{year}-01-01",
    )
    assert body["window_days"] == (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days


async def test_a_non_mentor_reads_zeros_not_an_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        user = await add_user(
            conn, f"plain-links-{uuid4()}@example.test", primary_role="mentee", timezone="UTC"
        )

    response = await api_client.get(
        f"{URL}?window_days=7", headers=bearer(await token_for(db_engine, user))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window_days"] == 7
    for block in (body["current"], body["previous"]):
        assert block["visits"] == block["shares"] == 0
        assert block["visits_by_via"] == json_via()
        assert block["top_referrers"] == []
        assert block["shares_by_method"] == json_method()


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("window_days=6", 422),
        ("window_days=7", 200),
        ("window_days=365", 200),
        ("window_days=366", 422),
        ("window_days=30&year=2026", 422),
        ("year=1999", 422),
    ],
)
async def test_the_window_is_bounded(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, query: str, code: int
) -> None:
    mentor = await make_public_mentor(db_engine, f"links-bound-{uuid4().hex[:8]}")

    response = await api_client.get(
        f"{URL}?{query}", headers=bearer(await token_for(db_engine, mentor))
    )

    assert response.status_code == code, response.text


async def test_without_a_token_it_is_401(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(URL)

    assert response.status_code == 401
