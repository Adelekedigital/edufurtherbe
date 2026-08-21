"""Asking Loops what each template declares.

**Two traps, both invisible in a passing test on nine templates.** The list is
cursor-paginated with `perPage` capped at 50, so reading page one and stopping
works perfectly today and silently drops templates once an account passes fifty.
And `dataVariables` is empty for an *unpublished* template, so treating empty as
"needs nothing" sends a blank email — which is the failure the whole design
exists to prevent, wearing a convincing disguise.

Both are asserted here rather than left for the day they bite.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.errors import UpstreamError
from app.infra.clients.templates import LoopsTemplates, StaticTemplates

FAKE_KEY = "not-a-real-loops-key"


def page(templates: list[dict[str, Any]], *, cursor: str | None = None) -> dict[str, Any]:
    return {"data": templates, "pagination": {"nextCursor": cursor}}


def template(template_id: str, *variables: str) -> dict[str, Any]:
    return {"id": template_id, "name": template_id, "dataVariables": list(variables)}


def loops(handler: Any) -> tuple[LoopsTemplates, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return LoopsTemplates(FAKE_KEY, client=client), seen


# --------------------------------------------------------------------------
# Reading the list
# --------------------------------------------------------------------------


def test_one_call_answers_for_every_template() -> None:
    """The whole point of priming: nine templates, one request."""
    api, seen = loops(
        lambda _: httpx.Response(
            200, json=page([template("tmpl_a", "name"), template("tmpl_b", "mentorName")])
        )
    )

    assert api.declared("tmpl_a") == frozenset({"name"})
    assert api.declared("tmpl_b") == frozenset({"mentorName"})
    assert len(seen) == 1


def test_the_cursor_is_followed_to_the_end() -> None:
    """**The bug that ships green.** `perPage` caps at 50, so an account with
    sixty templates returns two pages — and an implementation that reads the
    first and stops loses the rest while every test on nine passes."""
    pages = [
        page([template("tmpl_first", "name")], cursor="page-2"),
        page([template("tmpl_last", "mentorName")]),
    ]
    api, seen = loops(lambda _: httpx.Response(200, json=pages.pop(0)))

    assert api.declared("tmpl_last") == frozenset({"mentorName"})
    assert len(seen) == 2, "the second page was never asked for"
    assert seen[1].url.params.get("cursor") == "page-2"


def test_a_short_page_does_not_end_the_walk() -> None:
    """**Only a null cursor means the end.** A page shorter than `perPage` is
    not a signal — believing it is would stop one page early whenever the
    provider chose to return fewer."""
    pages = [
        page([template("tmpl_one", "name")], cursor="more"),
        page([template("tmpl_two", "name")]),
    ]
    api, seen = loops(lambda _: httpx.Response(200, json=pages.pop(0)))
    api.prime()

    assert len(seen) == 2


def test_it_asks_for_the_largest_page_the_provider_allows() -> None:
    """Fewer round trips to learn the same thing."""
    api, seen = loops(lambda _: httpx.Response(200, json=page([])))
    api.prime()

    assert seen[0].url.params["perPage"] == "50"


# --------------------------------------------------------------------------
# The miss
# --------------------------------------------------------------------------


def test_an_unknown_template_is_fetched_on_its_own() -> None:
    """**The single endpoint earns its place here.** A name the cache has never
    seen is most likely a template published seconds ago, and fetching that one
    is the precise answer where refetching all of them is the crude one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/")[-1] == "tmpl_new":
            return httpx.Response(200, json=template("tmpl_new", "recipientName"))
        return httpx.Response(200, json=page([template("tmpl_old", "name")]))

    api, seen = loops(handler)

    assert api.declared("tmpl_new") == frozenset({"recipientName"})
    assert seen[-1].url.path.split("/")[-1] == "tmpl_new"


def test_a_second_lookup_is_answered_from_cache() -> None:
    """Templates change rarely and messages send often; a fetch per send is a
    round trip for no new information."""
    api, seen = loops(lambda _: httpx.Response(200, json=page([template("tmpl_a", "name")])))

    api.declared("tmpl_a")
    api.declared("tmpl_a")

    assert len(seen) == 1


# --------------------------------------------------------------------------
# When it goes wrong
# --------------------------------------------------------------------------


def test_an_unpublished_template_reports_nothing_and_says_so() -> None:
    """**The disguise.** Loops returns `dataVariables: []` for a draft. This
    reports it faithfully; `build_variables` is what refuses to send on it —
    the two together are what stop a blank email, and neither alone would."""
    api, _ = loops(lambda _: httpx.Response(200, json=page([template("tmpl_draft")])))

    assert api.declared("tmpl_draft") == frozenset()


def test_a_failed_prime_falls_through_to_single_fetches() -> None:
    """A cold cache is slower and still correct. Raising would stop a sweep
    that has other messages to send."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/")[-1] == "tmpl_a":
            return httpx.Response(200, json=template("tmpl_a", "name"))
        return httpx.Response(500, json={"error": "down"})

    api, _ = loops(handler)

    assert api.declared("tmpl_a") == frozenset({"name"})


def test_a_failed_single_fetch_is_raised_rather_than_guessed() -> None:
    """Returning an empty set here would be indistinguishable from an
    unpublished template, and would send blank."""
    api, _ = loops(lambda _: httpx.Response(503))

    with pytest.raises(UpstreamError):
        api.declared("tmpl_missing")


def test_a_response_that_is_not_an_object_is_refused() -> None:
    api, _ = loops(lambda _: httpx.Response(200, json=["nope"]))

    with pytest.raises(UpstreamError):
        api.declared("tmpl_a")


# --------------------------------------------------------------------------
# The static adapter
# --------------------------------------------------------------------------


def test_the_static_adapter_answers_from_its_map() -> None:
    api = StaticTemplates({"tmpl_a": frozenset({"name"})})

    assert api.declared("tmpl_a") == frozenset({"name"})


def test_an_unwired_static_adapter_declares_nothing() -> None:
    """**Which means it refuses rather than sends blank.** An empty declaration
    is treated as unpublished downstream, so a deployment that forgot to wire
    discovery gets failed rows naming the problem, never empty emails."""
    assert StaticTemplates().declared("tmpl_a") == frozenset()
