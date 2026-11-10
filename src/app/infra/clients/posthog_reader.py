"""PostHog, behind `AnalyticsReader` (PRD 31 PR 5, decision 331): plain `httpx`.

**The Query API, not Endpoints.** ``POST {api_host}/api/projects/{project_id}/query/``
with ``{"query": {...}}`` and ``Authorization: Bearer <personal API key>``; the
key needs only the **Query Read** scope (https://posthog.com/docs/api/queries).
PostHog's Endpoints product (https://posthog.com/docs/endpoints) turns a query
saved in the web app into a named route (``/api/environments/{id}/endpoints/{name}/run``,
``endpoint:read``), so the definition would live in PostHog where no review sees
it; here every query is built in this file from fixed text.

**The private host**, `ANALYTICS_API_HOST` (``https://us.posthog.com`` or
``https://eu.posthog.com``), never the ingestion host (``us.i.posthog.com``)
the sink sends to.

**Nothing a request sends is written into query text.** A funnel is a
`FunnelsQuery` (structured JSON: events, property filters, a date range); every
other read is a `HogQLQuery` whose variable parts are ``{placeholders}`` filled
from its ``values`` ("Constant values that can be referenced with the
{placeholder} syntax in the query", PostHog's query schema). The text itself is
constants of this module and of `domain.analytics_behaviour`.

**Limits** (https://posthog.com/docs/api/queries, "Limits"): 240 queries a
minute, 2,400 an hour, 3 at once, 10 seconds of execution each. The page reads
at most three queries per tab and caches results for 10 minutes, so it stays far
under them. Short timeouts: a slow answer is an unavailable source, never a
held request.

Synchronous like every adapter; the API layer calls it through `asyncio.to_thread`.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings
from app.core.errors import AnalyticsUpstreamError
from app.domain.analytics_behaviour import (
    ACCEPTED,
    APP_INSTALLED,
    BOOKED_WITHIN,
    BOOKING_REQUESTED,
    BOOKING_STARTED,
    DISCARDED,
    FROM_SEARCH,
    FROM_SERVER,
    JOIN_CLICKED,
    JOIN_FAILED,
    LOGIN_FAILED,
    LOGIN_VERIFIED,
    PROFILE_VIEWED,
    PUSH_ALLOWED,
    REQUEST_FAILED,
    SEARCH,
    WRITING_ASSIST,
    BehaviourFigures,
    Counted,
    ExploreFigures,
    Funnel,
    LoginCodes,
    Match,
    Searchers,
)
from app.domain.mentor_stats import Window
from app.domain.ports import AnalyticsReader
from app.infra.http.client import shared
from app.infra.http.upstream import trim_origin

__all__ = [
    "NOT_INTERNAL",
    "NullAnalyticsReader",
    "PostHogReader",
    "analytics_reader_for",
    "replay_url_for",
]

logger = logging.getLogger(__name__)

#: The two Behaviour routes wait out PostHog's own 10-second query budget.
TAB_TIMEOUT = httpx.Timeout(12.0, connect=2.0)
#: What a database tab adds for PostHog (the log-in alert, the Explore card):
#: about two seconds, then the tab answers without it.
OPTIONAL_TIMEOUT = httpx.Timeout(2.0, connect=1.0)

#: The one exclusion every read applies: the internal accounts' `users.id`s,
#: read from the database (`analytics_scope.internal_ids`), which are the
#: distinct ids the app identifies people by (ADR 0014). Server events are
#: never sent for them at all.
NOT_INTERNAL = "NOT has({internal}, distinct_id)"

#: A name this module writes into query text: an event or property name.
_NAME = re.compile(r"[a-z_$][a-z0-9_$]*")
#: A breakdown value a browser sent (a source, a route) shown back as a row:
#: anything else is not a value the app sends, so it is not shown.
_SHOWN = re.compile(r"[A-Za-z0-9_./\[\]:-]{1,120}")

UNAVAILABLE = "product analytics are not answering right now"


def _name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"not a plain name: {value!r}")
    return value


def _events(names: Sequence[str]) -> str:
    """A list of event names inside a funnel step's HogQL filter: each one a
    constant of `domain.analytics_behaviour`, checked to be a plain name."""
    return ", ".join(f"'{_name(n)}'" for n in names)


def _since_until(window: Window) -> dict[str, int]:
    return {"since": int(window.since.timestamp()), "until": int(window.until.timestamp())}


def _ids(internal: Sequence[UUID]) -> list[str]:
    """The internal ids as text, each one read back through `UUID`."""
    return [str(UUID(str(user))) for user in internal]


#: Fragments written into several queries, each said once. Every other
#: ``{placeholder}`` is a value (`hogql`).
_LOGIN_COUNTS = (
    "countIf(has({logins}, event)) AS attempts,\n"
    "    countIf(event = {failed}) AS failed,\n"
    "    countIf(event = {failed} AND toString(properties.is_migrated) = 'true') AS failed_migrated"
)
_FRAGMENTS = {
    "{not_internal}": NOT_INTERNAL,
    "{login_counts}": _LOGIN_COUNTS,
    "{from_server}": f"isNotNull(properties.{_name(FROM_SERVER.key)})",
}


def _written(text: str) -> str:
    """``text`` with this module's fixed fragments put in before it is sent."""
    for token, fragment in _FRAGMENTS.items():
        text = text.replace(token, fragment)
    return text


#: What each name placeholder in the queries below stands for: PRD 30's event
#: and property values, from `domain.analytics_behaviour`. A query is sent
#: with the ones it names (`hogql`), so its text is a literal of this module.
NAMES: dict[str, str | list[str]] = {
    "logins": [LOGIN_VERIFIED, LOGIN_FAILED],
    "failed": LOGIN_FAILED,
    "search": SEARCH,
    "viewed": PROFILE_VIEWED,
    "from_search": FROM_SEARCH,
    "join_clicked": JOIN_CLICKED,
    "join_failed": JOIN_FAILED,
    "assist": WRITING_ASSIST,
    "accepted": ACCEPTED,
    "discarded": DISCARDED,
    "installed": APP_INSTALLED,
    "push": PUSH_ALLOWED,
    "request_failed": REQUEST_FAILED,
    "requested": BOOKING_REQUESTED,
    "explore_events": [SEARCH, PROFILE_VIEWED, BOOKING_STARTED],
    "booked_events": [SEARCH, BOOKING_REQUESTED],
}

LOGIN_QUERY = _written("""
SELECT
    {login_counts}
FROM events
WHERE has({logins}, event)
    AND timestamp >= fromUnixTimestamp({since}) AND timestamp < fromUnixTimestamp({until})
    AND {not_internal}
""")

CARDS_QUERY = _written("""
SELECT
    {login_counts},
    countIf(event = {search}) AS searches,
    countIf(event = {search} AND toInt(properties.result_count) = 0) AS searches_no_results,
    countIf(event = {viewed} AND toString(properties.source) = {from_search})
        AS profile_views_from_search,
    countIf(event = {viewed}) AS profile_views,
    countIf(event = {join_clicked}) AS join_clicked,
    countIf(event = {join_failed}) AS join_failed,
    quantileIf(0.5)(toFloat(properties.minutes_from_start), event = {join_clicked})
        AS median_minutes_from_start,
    countIf(event = {assist}) AS assist_used,
    countIf(event = {assist} AND toString(properties.outcome) = {accepted}) AS assist_accepted,
    countIf(event = {assist} AND toString(properties.outcome) = {discarded}) AS assist_discarded,
    countIf(event = {installed}) AS app_installed,
    uniqIf(person_id, event = {push}) AS push_allowed_people,
    uniq(person_id) AS active_people
FROM events
WHERE timestamp >= fromUnixTimestamp({since}) AND timestamp < fromUnixTimestamp({until})
    AND {not_internal}
""")

#: Profile views by their `source`, most first.
SOURCES_QUERY = _written("""
SELECT toString(properties.source) AS key, count() AS count
FROM events
WHERE event = {viewed}
    AND timestamp >= fromUnixTimestamp({since}) AND timestamp < fromUnixTimestamp({until})
    AND {not_internal}
GROUP BY key
ORDER BY count DESC, key
LIMIT 20
""")

#: Failed requests by their `route`, most first.
ROUTES_QUERY = _written("""
SELECT toString(properties.route) AS key, count() AS count
FROM events
WHERE event = {request_failed}
    AND timestamp >= fromUnixTimestamp({since}) AND timestamp < fromUnixTimestamp({until})
    AND {not_internal}
GROUP BY key
ORDER BY count DESC, key
LIMIT 20
""")

#: Searchers per asked window (``g`` is its 1-based place in ``{starts}`` and
#: ``{ends}``, Unix seconds): a person who searched, and whether they viewed a
#: profile or started a booking after their first search in that window.
EXPLORE_QUERY = _written("""
SELECT g, countIf(searched) AS searched, countIf(searched AND led) AS led
FROM (
    SELECT
        person_id,
        g,
        countIf(event = {search}) > 0 AS searched,
        maxIf(timestamp, event != {search}) > minIf(timestamp, event = {search}) AS led
    FROM (
        SELECT
            person_id,
            timestamp,
            event,
            arrayJoin(arrayFilter(
                i -> toUnixTimestamp(timestamp) >= {starts}[i]
                    AND toUnixTimestamp(timestamp) < {ends}[i],
                range(1, length({starts}) + 1)
            )) AS g
        FROM events
        WHERE has({explore_events}, event)
            AND timestamp >= fromUnixTimestamp({since}) AND timestamp < fromUnixTimestamp({until})
            AND {not_internal}
    )
    GROUP BY person_id, g
)
GROUP BY g
ORDER BY g
""")

#: Searchers of ``[since, until)`` who requested a booking (the server's
#: event, which carries the session) within ``{within}`` seconds of their
#: first search in it; events read up to ``{booked_until}``.
BOOKED_QUERY = _written("""
SELECT countIf(searched AND arrayExists(
    b -> b > first_search AND b <= first_search + {within}, booked_at
)) AS booked
FROM (
    SELECT
        person_id,
        countIf(event = {search} AND timestamp < fromUnixTimestamp({until})) > 0 AS searched,
        toUnixTimestamp(minIf(
            timestamp, event = {search} AND timestamp < fromUnixTimestamp({until})
        )) AS first_search,
        groupArrayIf(
            toUnixTimestamp(timestamp),
            event = {requested} AND {from_server}
        ) AS booked_at
    FROM events
    WHERE has({booked_events}, event)
        AND timestamp >= fromUnixTimestamp({since})
        AND timestamp < fromUnixTimestamp({booked_until})
        AND {not_internal}
    GROUP BY person_id
)
""")

#: A ``{placeholder}`` in a query's text.
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _iso(at: dt.datetime) -> str:
    return at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _filter(match: Match) -> dict[str, Any]:
    """A PostHog property filter: ``exact`` (``is_not`` negated, which also
    matches a person without it) or ``is_set``."""
    if match.value is None:
        return {"type": match.on, "key": _name(match.key), "operator": "is_set", "value": "is_set"}
    operator = "is_not" if match.negate else "exact"
    return {"type": match.on, "key": _name(match.key), "operator": operator, "value": [match.value]}


def funnel_query(
    funnel: Funnel, by: str, window: Window, internal: Sequence[UUID]
) -> dict[str, Any]:
    """The `FunnelsQuery` for ``funnel`` over people entering in ``window``,
    narrowed by the ``by`` option (already one the funnel offers).
    ``filterTestAccounts`` stays off: the exclusion is `NOT_INTERNAL` over
    ``internal``, here, not a project setting nobody reviews."""
    breakdown = funnel.by[by]
    series = []
    for i, step in enumerate(funnel.tracked):
        where = [_filter(m) for m in step.where]
        if breakdown.step == i:
            where += [_filter(m) for m in breakdown.matches]
        if len(step.events) == 1:
            node: dict[str, Any] = {"kind": "EventsNode", "event": _name(step.events[0])}
        else:
            node = {"kind": "EventsNode", "event": None}
            where.append({"type": "hogql", "key": f"event IN ({_events(step.events)})"})
        node["properties"] = where
        series.append(node)
    listed = "[" + ", ".join(f"'{user}'" for user in _ids(internal)) + "]"
    people = [*funnel.people, *(breakdown.matches if breakdown.step is None else ())]
    return {
        "kind": "FunnelsQuery",
        "series": series,
        "dateRange": {
            "date_from": _iso(window.since),
            "date_to": _iso(window.until),
            "explicitDate": True,
        },
        "funnelsFilter": {
            "funnelWindowInterval": funnel.window_days,
            "funnelWindowIntervalUnit": "day",
            "funnelOrderType": "ordered",
            "funnelVizType": "steps",
        },
        "filterTestAccounts": False,
        "properties": [
            {"type": "hogql", "key": NOT_INTERNAL.replace("{internal}", listed)},
            *(_filter(m) for m in people),
        ],
    }


def hogql(name: str, query: str, values: dict[str, Any]) -> dict[str, Any]:
    """A `HogQLQuery`: ``query`` as written, every ``{placeholder}`` in its
    ``values`` (the window's ``values``, else `NAMES`). ``name`` labels it in
    PostHog's query log."""
    wanted = set(_PLACEHOLDER.findall(query))
    filled = {key: values[key] if key in values else NAMES[key] for key in sorted(wanted)}
    return {"kind": "HogQLQuery", "query": query, "values": filled, "name": name}


class _MalformedError(ValueError):
    """The answer was not the shape the query asks for."""


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _MalformedError(f"not a count: {value!r}")
    if not math.isfinite(value) or value < 0 or value != int(value):
        raise _MalformedError(f"not a count: {value!r}")
    return int(value)


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _MalformedError(f"not a number: {value!r}")
    if not math.isfinite(value):
        raise _MalformedError(f"not a number: {value!r}")
    return float(value)


def _rows(body: Any, columns: Sequence[str]) -> list[dict[str, Any]]:
    """A HogQL answer's rows, by column name; `_MalformedError` unless every
    asked column is there."""
    if not isinstance(body, dict):
        raise _MalformedError("not an object")
    names, results = body.get("columns"), body.get("results")
    if not isinstance(names, list) or not isinstance(results, list):
        raise _MalformedError("no columns or results")
    missing = set(columns) - set(names)
    if missing:
        raise _MalformedError(f"missing columns {sorted(missing)}")
    rows = []
    for row in results:
        if not isinstance(row, list) or len(row) != len(names):
            raise _MalformedError("a row is not as wide as its columns")
        rows.append(dict(zip(names, row, strict=True)))
    return rows


def _one(body: Any, columns: Sequence[str]) -> dict[str, Any]:
    rows = _rows(body, columns)
    if len(rows) != 1:
        raise _MalformedError("an aggregate answered other than one row")
    return rows[0]


def _counted(body: Any) -> list[Counted]:
    return [
        Counted(key=row["key"], count=_int(row["count"]))
        for row in _rows(body, ("key", "count"))
        if isinstance(row["key"], str) and _SHOWN.fullmatch(row["key"])
    ]


def _login(row: dict[str, Any]) -> LoginCodes:
    return LoginCodes(
        attempts=_int(row["attempts"]),
        failed=_int(row["failed"]),
        failed_migrated=_int(row["failed_migrated"]),
    )


_LOGIN_COLUMNS = ("attempts", "failed", "failed_migrated")
_COUNT_COLUMNS = (
    "searches",
    "searches_no_results",
    "profile_views_from_search",
    "profile_views",
    "join_clicked",
    "join_failed",
    "assist_used",
    "assist_accepted",
    "assist_discarded",
    "app_installed",
    "push_allowed_people",
    "active_people",
)


class NullAnalyticsReader:
    """Product analytics are not configured here: every read is unavailable."""

    def _refuse(self) -> AnalyticsUpstreamError:
        return AnalyticsUpstreamError(UNAVAILABLE)

    def funnel(
        self, funnel: Funnel, by: str, window: Window, internal: Sequence[UUID]
    ) -> list[int]:
        del funnel, by, window, internal
        raise self._refuse()

    def behaviour(self, window: Window, internal: Sequence[UUID]) -> BehaviourFigures:
        del window, internal
        raise self._refuse()

    def explore(self, windows: Sequence[Window], internal: Sequence[UUID]) -> ExploreFigures:
        del windows, internal
        raise self._refuse()

    def login_codes(self, window: Window, internal: Sequence[UUID]) -> LoginCodes:
        del window, internal
        raise self._refuse()


class PostHogReader:
    """Reads one PostHog project through its Query API."""

    def __init__(
        self,
        *,
        api_host: str,
        project_id: str,
        api_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = f"{trim_origin(api_host)}/api/projects/{project_id}/query/"
        self._key = api_key
        self._client = client or shared(TAB_TIMEOUT)

    def _ask(self, query: dict[str, Any], timeout: httpx.Timeout) -> Any:
        """The answer's body, or the one unavailable error for any failure:
        a transport error, a timeout, a non-2xx, or a body that is not JSON.
        Only the status and the kind of failure are logged: an error body can
        echo the request, key included."""
        response: httpx.Response | None = None
        try:
            response = self._client.post(
                self._url,
                json={"query": query, "refresh": "blocking"},
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "analytics read failed",
                extra={
                    "query_kind": query.get("kind"),
                    "failure": type(exc).__name__,
                    "status": None if response is None else response.status_code,
                },
            )
            raise AnalyticsUpstreamError(UNAVAILABLE) from exc

    def _read[T](
        self,
        query: dict[str, Any],
        parse: Callable[[Any], T],
        timeout: httpx.Timeout = TAB_TIMEOUT,
    ) -> T:
        body = self._ask(query, timeout)
        try:
            return parse(body)
        except (_MalformedError, AttributeError, KeyError, TypeError) as exc:
            logger.warning(
                "analytics answer unreadable",
                extra={"query_kind": query.get("kind"), "why": str(exc)},
            )
            raise AnalyticsUpstreamError(UNAVAILABLE) from exc

    def funnel(
        self, funnel: Funnel, by: str, window: Window, internal: Sequence[UUID]
    ) -> list[int]:
        steps = len(funnel.tracked)

        def parse(body: Any) -> list[int]:
            results = body["results"]
            if not isinstance(results, list) or len(results) != steps:
                raise _MalformedError("not one result per step")
            ordered = sorted(results, key=lambda r: r.get("order", 0))
            return [_int(r["count"]) for r in ordered]

        return self._read(funnel_query(funnel, by, window, internal), parse)

    def login_codes(self, window: Window, internal: Sequence[UUID]) -> LoginCodes:
        values = {**_since_until(window), "internal": _ids(internal)}
        query = hogql("admin_analytics_login_codes", LOGIN_QUERY, values)
        return self._read(query, lambda body: _login(_one(body, _LOGIN_COLUMNS)), OPTIONAL_TIMEOUT)

    def behaviour(self, window: Window, internal: Sequence[UUID]) -> BehaviourFigures:
        values = {**_since_until(window), "internal": _ids(internal)}
        cards = self._read(
            hogql("admin_analytics_cards", CARDS_QUERY, values),
            lambda body: _one(body, (*_LOGIN_COLUMNS, *_COUNT_COLUMNS)),
        )
        sources = self._read(
            hogql("admin_analytics_profile_sources", SOURCES_QUERY, values), _counted
        )
        routes = self._read(hogql("admin_analytics_failed_routes", ROUTES_QUERY, values), _counted)
        try:
            counts = {name: _int(cards[name]) for name in _COUNT_COLUMNS}
            counts["profile_views_total"] = counts.pop("profile_views")
            median = _number(cards.get("median_minutes_from_start"))
            login = _login(cards)
        except _MalformedError as exc:
            logger.warning("analytics answer unreadable", extra={"why": str(exc)})
            raise AnalyticsUpstreamError(UNAVAILABLE) from exc
        return BehaviourFigures(
            login=login,
            profile_views=sources,
            errors=routes,
            median_minutes_from_start=median,
            **counts,
        )

    def explore(self, windows: Sequence[Window], internal: Sequence[UUID]) -> ExploreFigures:
        since = min(w.since for w in windows)
        until = max(w.until for w in windows)
        values = {
            **_since_until(Window(since=since, until=until)),
            "starts": [int(w.since.timestamp()) for w in windows],
            "ends": [int(w.until.timestamp()) for w in windows],
            "internal": _ids(internal),
        }

        def per_window(body: Any) -> list[Searchers]:
            found = {_int(r["g"]): r for r in _rows(body, ("g", "searched", "led"))}
            return [
                Searchers(searched=_int(found[i]["searched"]), led=_int(found[i]["led"]))
                if i in found
                else Searchers(searched=0, led=0)
                for i in range(1, len(windows) + 1)
            ]

        searchers = self._read(
            hogql("admin_analytics_explore", EXPLORE_QUERY, values), per_window, OPTIONAL_TIMEOUT
        )
        first = windows[0]
        booked = self._read(
            hogql(
                "admin_analytics_explore_booked",
                BOOKED_QUERY,
                {
                    **_since_until(first),
                    "booked_until": int((first.until + BOOKED_WITHIN).timestamp()),
                    "within": int(BOOKED_WITHIN.total_seconds()),
                    "internal": values["internal"],
                },
            ),
            lambda body: _int(_one(body, ("booked",))["booked"]),
            OPTIONAL_TIMEOUT,
        )
        return ExploreFigures(per_window=searchers, booked=booked)


def _api(settings: Settings) -> tuple[str, str, str] | None:
    """The private host, project id and key, when all three are set and the
    id is a number (it is written into the path)."""
    host, project = settings.analytics_api_host, settings.analytics_project_id
    key = settings.analytics_api_key
    if not host or not project or key is None or not project.isdigit():
        return None
    secret = key.get_secret_value()
    return (host, project, secret) if secret.strip() else None


def analytics_reader_for(settings: Settings) -> AnalyticsReader:
    """PostHog when its private API is configured, else the null reader.
    Reading needs no sending, so `ANALYTICS_ENABLED` does not decide."""
    api = _api(settings)
    if api is None:
        return NullAnalyticsReader()
    host, project, key = api
    return PostHogReader(api_host=host, project_id=project, api_key=key)


def replay_url_for(settings: Settings) -> str | None:
    """The project's session replay list in PostHog's app, or ``None`` when
    the private API is not configured."""
    api = _api(settings)
    if api is None:
        return None
    host, project, _ = api
    return f"{trim_origin(host)}/project/{project}/replay/home"
