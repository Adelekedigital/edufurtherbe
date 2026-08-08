"""Fetching the university catalogue.

**Over HTTPS, from the repository — never from the API.** ``universities.hipolabs.com``
serves plain HTTP with no TLS, so a browser on an HTTPS page cannot call it at
all (mixed content is blocked, with no opt-out) and a server calling it would be
sending an unencrypted request on behalf of our users. The same data sits in the
project's GitHub repository, which is HTTPS. That asymmetry is what makes ADR
0020's mirror clean: we take the data securely and never touch the endpoint.

The commit the file came from is recorded alongside it, so a snapshot can be
traced to an upstream revision rather than only to a timestamp.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.errors import AppError

#: The raw file, over HTTPS. Pinned to `master` deliberately: a tag would freeze
#: the mirror at a release the upstream project does not actually cut, and the
#: file is the product rather than the code around it.
CATALOGUE_URL = (
    "https://raw.githubusercontent.com/Hipo/university-domains-list/"
    "master/world_universities_and_domains.json"
)

#: The commits API for the same path, so a snapshot names the revision it came
#: from. Best-effort: a snapshot with no commit is still a usable snapshot.
COMMITS_URL = (
    "https://api.github.com/repos/Hipo/university-domains-list/commits"
    "?path=world_universities_and_domains.json&per_page=1"
)


class CatalogueUnavailableError(AppError):
    """The catalogue could not be fetched."""


@dataclass(frozen=True, slots=True)
class Catalogue:
    records: list[dict[str, Any]]
    #: The upstream commit, or ``None`` when the commits API was unreachable.
    source_commit: str | None


class HipolabsCatalogue:
    """Reads the published catalogue over HTTPS."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self) -> Catalogue:
        try:
            response = self._client.get(CATALOGUE_URL)
            response.raise_for_status()
            records = json.loads(response.content)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise CatalogueUnavailableError(f"could not fetch the catalogue: {exc}") from exc

        if not isinstance(records, list) or not records:
            raise CatalogueUnavailableError("the catalogue is not a non-empty list")

        return Catalogue(records=records, source_commit=self._latest_commit())

    def _latest_commit(self) -> str | None:
        """Best-effort, and deliberately so.

        The commit is provenance, not correctness. Failing the whole sync because
        an unrelated API was slow would turn a nice-to-have into a dependency,
        and the fetch above has already proved the data is good.
        """
        try:
            response = self._client.get(COMMITS_URL)
            response.raise_for_status()
            commits = response.json()
        except httpx.HTTPError, ValueError:
            return None
        if isinstance(commits, list) and commits:
            sha = commits[0].get("sha")
            return str(sha)[:12] if sha else None
        return None


class FileCatalogue:
    """A catalogue read from a local file.

    For a rehearsal against a snapshot somebody already took, and for a cutover
    where reaching GitHub during the freeze is one dependency more than the
    window can afford. Same shape as the Bubble source pair — one canonical
    record whichever adapter produced it, so nothing downstream learns which.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def fetch(self) -> Catalogue:
        try:
            records = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogueUnavailableError(f"could not read {self._path}: {exc}") from exc
        if not isinstance(records, list) or not records:
            raise CatalogueUnavailableError(f"{self._path} is not a non-empty list")
        return Catalogue(records=records, source_commit=None)
