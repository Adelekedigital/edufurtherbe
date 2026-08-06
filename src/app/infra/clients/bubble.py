"""Two ways to read the legacy snapshot, emitting one shape.

``JsonExportSource`` reads the Data-tab export; ``BubbleApiSource`` calls the
Data API. Both satisfy ``domain.bubble.BubbleSource``, and both return canonical
redacted records, so the transform never learns which one it got.

The export is the source that has been used for migrations before and is trusted;
the API is the one that carries `authentication`, and therefore the only place
`auth_identities.provider_user_id` exists at all. Neither is redundant.
"""

import json
from collections.abc import Iterator
from datetime import tzinfo
from pathlib import Path
from typing import Any

import httpx

from app.core.errors import AppError
from app.domain.bubble import blank_to_none, redact

# The export renders timestamps in the Bubble application's timezone with no
# offset. This is not a guess: the same user (1701974206179x877854702892984200)
# reads `Dec 7, 2023 1:36 pm` in the export and `2023-12-07T18:36:46.179Z` from
# the API, and 13:36 EST is 18:36 UTC exactly.
#
# It is still passed explicitly rather than defaulted, because it is a property
# of the Bubble app's settings and can be changed by someone in a dashboard
# without anything here noticing.
EXPORT_ID_FIELD = "unique id"
API_ID_FIELD = "_id"


class BubbleSourceError(AppError):
    """The snapshot could not be read."""


def _identities(record: dict[str, Any]) -> dict[str, str]:
    """Provider subject ids, from the API's ``authentication`` object.

    ``{"Google": {"id": "1047…", "email": "…"}}`` becomes ``{"google": "1047…"}``.
    The ``email`` key inside ``authentication`` is the account itself rather than
    a linked provider, so it is excluded — an email registration produces no
    ``auth_identities`` row, which is what the field mapping specifies.

    The export has none of this, so a record read from it yields ``{}`` and the
    transform creates no identity rows. That is a real difference in what the two
    sources *contain*, not a difference in shape, and it is why the API is
    required for M1 rather than merely preferred.
    """
    auth = record.get("authentication") or {}
    return {
        provider.lower(): str(details["id"])
        for provider, details in auth.items()
        if provider.lower() != "email" and isinstance(details, dict) and details.get("id")
    }


def _canonicalise(record: dict[str, Any], *, id_field: str) -> dict[str, Any]:
    """One record, in the shape everything downstream expects.

    Redaction happens **here**, in the step both adapters share, rather than in
    each of them — so it cannot be implemented once and forgotten in the other.
    """
    fields = {key: blank_to_none(value) for key, value in redact(record).items()}

    identifier = fields.pop(id_field, None)
    if not identifier:
        raise BubbleSourceError(f"record has no {id_field!r}: {sorted(fields)[:6]}")

    auth = fields.pop("authentication", None) or {}
    email = fields.pop("email", None) or (auth.get("email") or {}).get("email")

    # The export says `Creation Date`; the API says `Created Date`. Found by
    # dry-running the loader against the real export, which refused all 43
    # records for a missing timestamp — the sixth difference between the two
    # sources and the one that was not in the original list.
    #
    # Both become `created_at`/`modified_at`, so the transform reads a name
    # neither source uses and cannot accidentally depend on one of them.
    created = fields.pop("Creation Date", None) or fields.pop("Created Date", None)
    modified = fields.pop("Modified Date", None)

    return {
        **fields,
        "bubble_id": str(identifier),
        "email": email,
        "created_at": created,
        "modified_at": modified,
        "provider_identities": _identities(record),
    }


class JsonExportSource:
    """Reads ``<directory>/<thing>.json`` — a Bubble Data-tab export."""

    def __init__(self, directory: Path, *, timezone: tzinfo) -> None:
        self._directory = directory
        # Held rather than used here: the reader does not parse timestamps, the
        # transform does. Carrying it makes the export's missing offset an
        # explicit property of the source instead of something the transform has
        # to know to ask about.
        self.timezone = timezone

    def read(self, thing: str) -> Iterator[dict[str, Any]]:
        path = self._directory / f"{thing}.json"
        if not path.is_file():
            raise BubbleSourceError(f"no export for {thing!r} at {path}")

        # utf-8 explicitly: the field names contain emoji, and Windows defaults
        # to cp1252, which cannot decode them.
        records = json.loads(path.read_text(encoding="utf-8"))
        for record in records:
            yield _canonicalise(record, id_field=EXPORT_ID_FIELD)


class BubbleApiSource:
    """Reads the Bubble Data API, following its cursor to exhaustion."""

    #: Bubble caps a page at 100 regardless of what is asked for.
    PAGE_SIZE = 100

    def __init__(self, base_url: str, token: str, client: httpx.Client) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    def read(self, thing: str) -> Iterator[dict[str, Any]]:
        cursor = 0
        while True:
            payload = self._page(thing, cursor)
            for record in payload["results"]:
                yield _canonicalise(record, id_field=API_ID_FIELD)

            # `remaining` is what Bubble reports as still available after this
            # page. Trusting `len(results) < PAGE_SIZE` instead would silently
            # stop early on any page the API chose to trim.
            if not payload.get("remaining"):
                return
            cursor += len(payload["results"]) or self.PAGE_SIZE

    def _page(self, thing: str, cursor: int) -> dict[str, Any]:
        response = self._client.get(
            f"{self._base_url}/obj/{thing}",
            params={"cursor": cursor, "limit": self.PAGE_SIZE},
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The body may echo the token back on an auth failure, so only the
            # status is reported. A 401 here is unambiguous without it.
            raise BubbleSourceError(f"{thing}: Bubble returned {response.status_code}")
        return dict(response.json()["response"])
