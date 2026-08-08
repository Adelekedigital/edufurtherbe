"""Fetching the catalogue, from either source.

The test that matters most is the one asserting **no plain-HTTP request is ever
made**. Everything else here would fail loudly if it broke; a request quietly
falling back to `http://universities.hipolabs.com` would not fail at all — it
would work, and ship our users' traffic unencrypted. Same shape as the redaction
tests in `test_bubble_source.py`, and for the same reason.

`httpx.MockTransport` throughout: the real catalogue is 2.25 MB behind a third
party, so a test that fetched it would be slow and would go red on somebody
else's outage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.infra.clients.hipolabs import (
    CATALOGUE_URL,
    CatalogueUnavailableError,
    FileCatalogue,
    HipolabsCatalogue,
)

RECORDS: list[dict[str, Any]] = [
    {
        "name": "University of Lagos",
        "domains": ["unilag.edu.ng"],
        "web_pages": ["https://unilag.edu.ng/"],
        "alpha_two_code": "NG",
    }
]
COMMIT = [{"sha": "abcdef0123456789abcdef"}]


def client_for(
    catalogue: httpx.Response, commits: httpx.Response | None = None
) -> tuple[httpx.Client, list[str]]:
    """A client that answers the two URLs, recording every one it was asked for."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "api.github.com" in str(request.url):
            return commits or httpx.Response(200, json=COMMIT)
        return catalogue

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def ok(records: Any = None) -> httpx.Response:
    return httpx.Response(200, json=RECORDS if records is None else records)


# --------------------------------------------------------------------------
# The URLs — the reason this module exists
# --------------------------------------------------------------------------


def test_every_request_goes_over_https_and_none_touches_the_api() -> None:
    """ADR 0020's whole decision, asserted mechanically.

    `universities.hipolabs.com` has no TLS. A browser is blocked from calling it
    by mixed-content policy; nothing blocks a server, which is exactly why this
    needs a test rather than a comment.
    """
    client, seen = client_for(ok())

    HipolabsCatalogue(client).fetch()

    assert seen, "no request was made at all"
    assert all(url.startswith("https://") for url in seen), seen
    assert not any("universities.hipolabs.com" in url for url in seen), seen


def test_a_failed_fetch_does_not_fall_back_to_the_plain_http_api() -> None:
    """The failure path, which the test above does not reach.

    Written because a mutation proved it: reintroducing an `http://` fallback in
    the `except` branch left every other test in this file green. Failure is the
    only moment a fallback is tempting, and it is the moment nobody is watching —
    so the assertion has to be on the failure, not on the success.
    """
    client, seen = client_for(httpx.Response(503))

    with pytest.raises(CatalogueUnavailableError):
        HipolabsCatalogue(client).fetch()

    assert seen, "no request was made at all"
    assert all(url.startswith("https://") for url in seen), seen
    assert not any("universities.hipolabs.com" in url for url in seen), seen


def test_the_catalogue_url_is_the_raw_file() -> None:
    client, seen = client_for(ok())

    HipolabsCatalogue(client).fetch()

    assert CATALOGUE_URL in seen


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def test_a_fetch_returns_the_records_and_the_commit() -> None:
    client, _ = client_for(ok())

    catalogue = HipolabsCatalogue(client).fetch()

    assert catalogue.records == RECORDS
    # Truncated: enough to identify a revision, short enough to read in a log.
    assert catalogue.source_commit == "abcdef012345"


def test_an_unreachable_commits_api_still_yields_a_usable_catalogue() -> None:
    """Best-effort, and deliberately so. The commit is provenance, not
    correctness — failing the sync because an unrelated API was slow would turn a
    nice-to-have into a dependency."""
    client, _ = client_for(ok(), commits=httpx.Response(500))

    catalogue = HipolabsCatalogue(client).fetch()

    assert catalogue.records == RECORDS
    assert catalogue.source_commit is None


@pytest.mark.parametrize(
    "commits",
    [
        pytest.param([], id="no commits"),
        pytest.param([{"message": "no sha here"}], id="a commit with no sha"),
        pytest.param({"message": "Not Found"}, id="an error object, not a list"),
    ],
)
def test_a_commits_response_without_a_sha_is_not_fatal(commits: Any) -> None:
    client, _ = client_for(ok(), commits=httpx.Response(200, json=commits))

    assert HipolabsCatalogue(client).fetch().source_commit is None


def test_an_http_error_on_the_catalogue_is_fatal() -> None:
    """The opposite of the commit. Without records there is nothing to mirror,
    and a sync that quietly wrote nothing would look like a sync that ran."""
    client, _ = client_for(httpx.Response(503))

    with pytest.raises(CatalogueUnavailableError, match="could not fetch"):
        HipolabsCatalogue(client).fetch()


def test_a_body_that_is_not_json_is_fatal() -> None:
    """What a captive portal or an error page actually returns — HTTP 200 with
    HTML in it."""
    client, _ = client_for(httpx.Response(200, text="<html>upstream is down</html>"))

    with pytest.raises(CatalogueUnavailableError, match="could not fetch"):
        HipolabsCatalogue(client).fetch()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="empty list"),
        pytest.param({"detail": "not found"}, id="an object, not a list"),
        pytest.param("a string", id="a bare string"),
    ],
)
def test_a_catalogue_that_is_not_a_non_empty_list_is_refused(payload: Any) -> None:
    """An empty list would mirror nothing and report success, leaving the table
    at whatever it held while `last_synced_at` claimed it was fresh."""
    client, _ = client_for(ok(payload))

    with pytest.raises(CatalogueUnavailableError, match="non-empty list"):
        HipolabsCatalogue(client).fetch()


# --------------------------------------------------------------------------
# The file source
# --------------------------------------------------------------------------


def test_a_file_catalogue_reads_the_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(RECORDS), encoding="utf-8")

    catalogue = FileCatalogue(path).fetch()

    assert catalogue.records == RECORDS
    # A file carries no provenance — the commit is what the fetch adds.
    assert catalogue.source_commit is None


def test_a_missing_file_names_the_path(tmp_path: Path) -> None:
    path = tmp_path / "absent.json"

    with pytest.raises(CatalogueUnavailableError, match=r"absent\.json"):
        FileCatalogue(path).fetch()


def test_a_malformed_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CatalogueUnavailableError, match="could not read"):
        FileCatalogue(path).fetch()


def test_an_empty_file_catalogue_is_refused(tmp_path: Path) -> None:
    """The same rule as the fetch. Both sources produce one canonical record, so
    both must refuse the same things — or `--from-file` becomes a way past a
    check the network path enforces.
    """
    path = tmp_path / "catalogue.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(CatalogueUnavailableError, match="non-empty list"):
        FileCatalogue(path).fetch()
