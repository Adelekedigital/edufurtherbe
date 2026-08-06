"""Reading the legacy snapshot, from either source, with credentials dropped.

The tests that matter most are the redaction ones. Everything else here would
fail loudly if it broke; a credential surviving into a snapshot file would not
fail at all.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.domain.bubble import (
    REDACTED_FIELDS,
    BubbleTimestampError,
    blank_to_none,
    normalise_list,
    parse_timestamp,
    redact,
)
from app.infra.clients.bubble import BubbleApiSource, BubbleSourceError, JsonExportSource

LAGOS = ZoneInfo("Africa/Lagos")
NEW_YORK = ZoneInfo("America/New_York")

# Every credential field, with values shaped like the real ones.
CREDENTIALS: dict[str, Any] = {
    "calAccessToken": "aaa.bbb.ccc",
    "calAccessTokenExpiresAt": 1748707065792,
    "calRefreshToken": "ddd.eee.fff",
    "calRefreshTokenExpiresAt": 1780243065792,
}


# --------------------------------------------------------------------------
# timestamps — one function, both sources
# --------------------------------------------------------------------------


def test_the_api_format_needs_no_timezone() -> None:
    assert parse_timestamp("2024-07-10T18:15:25.543Z") == datetime(
        2024, 7, 10, 18, 15, 25, 543000, tzinfo=UTC
    )


def test_the_export_format_matches_the_api_for_the_same_record() -> None:
    """Not a synthetic case — this pair is the same Bubble user read both ways.

    User ``1701974206179x877854702892984200`` reads ``Dec 7, 2023 1:36 pm`` in the
    Data-tab export and ``2023-12-07T18:36:46.179Z`` from the Data API. 13:36 EST
    is 18:36 UTC, which is what establishes that the export renders in
    ``America/New_York`` rather than UTC — measured, not assumed.
    """
    from_export = parse_timestamp("Dec 7, 2023 1:36 pm", assume=NEW_YORK)
    from_api = parse_timestamp("2023-12-07T18:36:46.179Z")

    assert from_export.replace(second=0) == from_api.replace(second=0, microsecond=0)


def test_an_offsetless_timestamp_refuses_rather_than_assuming_utc() -> None:
    """The single most dangerous default available here.

    The export carries no offset. Assuming UTC would shift every migrated
    ``created_at`` by four or five hours — plausible-looking, wrong, and
    invisible to every row-count and null-rate check the runbook specifies.
    """
    with pytest.raises(BubbleTimestampError, match="no UTC offset"):
        parse_timestamp("Sep 3, 2025 5:31 am")


def test_the_supplied_timezone_actually_changes_the_result() -> None:
    """Guards against ``assume`` being accepted and ignored, which would pass
    the test above while still producing UTC."""
    assert parse_timestamp("Sep 3, 2025 5:31 am", assume=NEW_YORK) != parse_timestamp(
        "Sep 3, 2025 5:31 am", assume=LAGOS
    )


@pytest.mark.parametrize("value", ["", "   ", "not a date", "2024-13-45"])
def test_an_unreadable_timestamp_raises(value: str) -> None:
    with pytest.raises(BubbleTimestampError):
        parse_timestamp(value, assume=UTC)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_every_credential_field_is_dropped() -> None:
    cleaned = redact({**CREDENTIALS, "First Name": "Sakiratu"})

    assert set(cleaned) == {"First Name"}


def test_composio_auth_id_is_kept() -> None:
    """It is a reference to a credential Composio holds, not a credential, and
    the field mapping migrates it. Redacting it would lose real data."""
    assert redact({"composioAuthId": "ca_XDktsiPz_RuN"}) == {"composioAuthId": "ca_XDktsiPz_RuN"}


def test_redaction_does_not_mutate_the_original() -> None:
    """The mistake this makes impossible is "redacted, then wrote the wrong
    variable" — which produces a clean-looking call site and a leaking file."""
    original = dict(CREDENTIALS)

    redact(original)

    assert original == CREDENTIALS


# --------------------------------------------------------------------------
# shape normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["a", "b"], ["a", "b"]),
        ("a , b", ["a", "b"]),
        ("a,b", ["a", "b"]),
        ("", []),
        (None, []),
    ],
)
def test_lists_normalise_from_either_source(value: Any, expected: list[str]) -> None:
    assert normalise_list(value) == expected


@pytest.mark.parametrize(("value", "expected"), [("", None), ("  ", None), ("x", "x"), (0, 0)])
def test_blank_strings_become_none(value: Any, expected: Any) -> None:
    """``0`` and ``False`` must survive — a credit balance of zero is a fact."""
    assert blank_to_none(value) == expected


# --------------------------------------------------------------------------
# JsonExportSource
# --------------------------------------------------------------------------


def write_export(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    (tmp_path / "user.json").write_text(json.dumps(records), encoding="utf-8")
    return tmp_path


def test_the_export_reader_emits_canonical_records(tmp_path: Path) -> None:
    directory = write_export(
        tmp_path,
        [{"unique id": "1x2", "email": "a@example.com", "First Name": "Ada", "Slug": ""}],
    )

    (record,) = JsonExportSource(directory, timezone=NEW_YORK).read("user")

    assert record["bubble_id"] == "1x2"
    assert record["email"] == "a@example.com"
    assert record["Slug"] is None
    assert record["provider_identities"] == {}
    assert "unique id" not in record


def test_no_credential_survives_the_export_reader(tmp_path: Path) -> None:
    """The assertion this file exists for.

    Written against the serialised output rather than the dict, because what
    matters is whether a token can reach a file — a check on keys alone would
    miss a value copied under a different name.
    """
    directory = write_export(tmp_path, [{"unique id": "1x2", **CREDENTIALS}])

    (record,) = JsonExportSource(directory, timezone=NEW_YORK).read("user")

    written = json.dumps(record)
    for field, value in CREDENTIALS.items():
        assert field not in written
        assert str(value) not in written


def test_both_sources_agree_on_the_timestamp_key(tmp_path: Path) -> None:
    """The export says ``Creation Date``; the API says ``Created Date``.

    A sixth difference between the two, and the one that was missed: the first
    list had five. It surfaced by dry-running the loader against the real export,
    which refused **all 43 records** for a missing timestamp — loud, but only
    because something ran it. Both keys now become ``created_at``, a name neither
    source uses, so the transform cannot accidentally depend on one of them.
    """
    directory = write_export(
        tmp_path, [{"unique id": "1x2", "Creation Date": "Sep 3, 2025 5:31 am"}]
    )
    (from_export,) = JsonExportSource(directory, timezone=NEW_YORK).read("user")

    source = api_source(
        [{"results": [{"_id": "1x2", "Created Date": "2025-09-03T09:31:00Z"}], "remaining": 0}]
    )
    (from_api,) = source.read("user")

    assert from_export["created_at"] == "Sep 3, 2025 5:31 am"
    assert from_api["created_at"] == "2025-09-03T09:31:00Z"
    for record in (from_export, from_api):
        assert "Creation Date" not in record
        assert "Created Date" not in record


def test_a_record_without_an_id_is_refused(tmp_path: Path) -> None:
    directory = write_export(tmp_path, [{"First Name": "Ada"}])

    with pytest.raises(BubbleSourceError, match="unique id"):
        list(JsonExportSource(directory, timezone=NEW_YORK).read("user"))


def test_a_missing_export_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BubbleSourceError, match="no export"):
        list(JsonExportSource(tmp_path, timezone=NEW_YORK).read("absent"))


# --------------------------------------------------------------------------
# BubbleApiSource
# --------------------------------------------------------------------------


def api_source(pages: list[dict[str, Any]]) -> BubbleApiSource:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"response": pages[len(calls) - 1]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = BubbleApiSource("https://app.example.org/version-test/api/1.1/", "tok", client)
    source.calls = calls  # type: ignore[attr-defined]
    return source


def test_the_api_reader_lifts_authentication_into_email_and_identities() -> None:
    """``authentication`` is the one thing the export does not have, and
    ``Google.id`` is ``auth_identities.provider_user_id`` — a NOT NULL column, so
    without this the table cannot be migrated at all."""
    source = api_source(
        [
            {
                "results": [
                    {
                        "_id": "1x2",
                        "authentication": {
                            "email": {"email": "a@example.com", "email_confirmed": None},
                            "Google": {"id": "104789988730282226729", "email": "a@example.com"},
                        },
                    }
                ],
                "remaining": 0,
            }
        ]
    )

    (record,) = source.read("user")

    assert record["bubble_id"] == "1x2"
    assert record["email"] == "a@example.com"
    assert record["provider_identities"] == {"google": "104789988730282226729"}
    assert "authentication" not in record


def test_an_email_registration_yields_no_provider_identity() -> None:
    """``authentication.email`` is the account, not a linked provider. Treating
    it as one would create an ``auth_identities`` row for every email user and
    fail against the ``auth_provider`` enum."""
    source = api_source(
        [
            {
                "results": [{"_id": "1x2", "authentication": {"email": {"email": "a@x.com"}}}],
                "remaining": 0,
            }
        ]
    )

    (record,) = source.read("user")

    assert record["provider_identities"] == {}


def test_the_reader_follows_the_cursor_to_exhaustion() -> None:
    source = api_source(
        [
            {"results": [{"_id": "a"}, {"_id": "b"}], "remaining": 1},
            {"results": [{"_id": "c"}], "remaining": 0},
        ]
    )

    assert [r["bubble_id"] for r in source.read("user")] == ["a", "b", "c"]
    assert [dict(r.url.params)["cursor"] for r in source.calls] == ["0", "2"]  # type: ignore[attr-defined]


def test_no_credential_survives_the_api_reader() -> None:
    source = api_source([{"results": [{"_id": "1x2", **CREDENTIALS}], "remaining": 0}])

    (record,) = source.read("user")

    written = json.dumps(record)
    for field, value in CREDENTIALS.items():
        assert field not in written
        assert str(value) not in written


def test_an_error_response_does_not_echo_the_token() -> None:
    """A Bubble auth failure can include the bearer token in its body. The
    message carries the status only."""

    def handler(request: httpx.Request) -> httpx.Response:
        # The request is asserted on rather than ignored: a handler that never
        # looks at it would pass even if the reader called the wrong path.
        assert request.url.path.endswith("/obj/user")
        return httpx.Response(401, json={"body": {"token": "super-secret-token"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = BubbleApiSource("https://app.example.org/api/1.1", "super-secret-token", client)

    with pytest.raises(BubbleSourceError) as caught:
        list(source.read("user"))

    assert "super-secret-token" not in str(caught.value)
    assert "401" in str(caught.value)


def test_the_redacted_field_list_is_not_empty() -> None:
    """Every redaction test above would pass vacuously against an empty tuple."""
    assert len(REDACTED_FIELDS) == 4
