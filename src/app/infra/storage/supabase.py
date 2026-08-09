"""Supabase Storage, for the objects we re-host off Bubble.

**The bucket is not created here.** A migration script that can create a *public*
bucket is a migration script that can publish a private one by typo. The operator
creates it once in the dashboard; this adapter checks that it exists and refuses
to run otherwise, which turns a setup mistake into a message instead of 1,200
objects in the wrong place.

Holds the **service-role key**, like the Admin API client, and with the same
discipline: never logged, never in an error message, never leaves this process.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from app.core.errors import AppError
from app.infra.http.retry import send_with_backoff

#: Upload. `x-upsert` lets a re-run overwrite rather than 409 — which matters
#: because the object name is a content hash, so an "overwrite" writes identical
#: bytes to an identical path.
OBJECT = "/storage/v1/object"
#: What a browser fetches. The bucket must be public for this to resolve.
PUBLIC_OBJECT = "/storage/v1/object/public"
BUCKET = "/storage/v1/bucket"


class StorageError(AppError):
    """Storage refused, or was unreachable."""


class SupabaseStorage:
    """Uploads objects and says where they ended up."""

    def __init__(
        self,
        base_url: str,
        service_role_key: str,
        bucket: str,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = service_role_key
        self._bucket = bucket
        self._client = client
        self._sleep = sleep

    @property
    def _headers(self) -> dict[str, str]:
        return {"apikey": self._key, "Authorization": f"Bearer {self._key}"}

    @property
    def host(self) -> str:
        """The hostname objects are served from.

        This is what ``domain.assets.is_ours`` compares against, so the
        allowlist is derived from the configured project rather than written down
        a second time.
        """
        return httpx.URL(self._base_url).host

    def ensure_bucket(self) -> None:
        """Refuse to start unless the bucket exists.

        Checked once per run rather than per object: 1,200 objects failing one at
        a time on a missing bucket is 1,200 identical error lines and no headline.
        """
        response = send_with_backoff(
            lambda: self._client.get(
                f"{self._base_url}{BUCKET}/{self._bucket}", headers=self._headers
            ),
            self._sleep,
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            raise StorageError(
                f"the bucket {self._bucket!r} does not exist. Create it in the "
                "Supabase dashboard as a PUBLIC bucket, then re-run. It is not "
                "created here on purpose — a script that can create a public "
                "bucket can publish a private one by typo."
            )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise StorageError(f"could not read bucket {self._bucket!r}: {response.status_code}")

    def upload(self, path: str, payload: bytes, content_type: str) -> str:
        """Put an object at ``path`` and return the URL it is served from.

        The content type is the caller's sniffed value, never one guessed from a
        filename — the dev export contains a ``.gif`` and an ``.avif`` that both
        hold JPEG bytes.
        """
        response = send_with_backoff(
            lambda: self._client.post(
                f"{self._base_url}{OBJECT}/{self._bucket}/{path}",
                headers={
                    **self._headers,
                    "Content-Type": content_type,
                    # A re-run writes identical bytes to an identical path, so
                    # overwriting is a no-op rather than a risk.
                    "x-upsert": "true",
                },
                content=payload,
            ),
            self._sleep,
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # Status only. The response body can echo the request, and the
            # request carried the service-role key.
            raise StorageError(f"upload of {path} failed with {response.status_code}")

        return self.public_url(path)

    def public_url(self, path: str) -> str:
        return f"{self._base_url}{PUBLIC_OBJECT}/{self._bucket}/{path}"

    def path_of(self, url: str) -> str | None:
        """The object path inside this bucket, or ``None`` if the URL is not ours.

        Used to remove an image somebody replaced. It refuses anything it did not
        build — a stored URL predating this scheme, or a value from somewhere
        else — because deriving a delete target from an unrecognised string is
        how a delete reaches the wrong object.
        """
        prefix = f"{self._base_url}{PUBLIC_OBJECT}/{self._bucket}/"
        return url[len(prefix) :] if url.startswith(prefix) else None

    def delete(self, path: str) -> bool:
        """Remove an object. ``False`` if it was not there.

        **Best effort by contract.** The caller removes a *replaced* image after
        the profile already points at the new one, so a failure here leaves an
        orphan and nothing broken — where raising would fail an upload that has
        already succeeded. A missing object is not an error for the same reason:
        two replacements racing both try to remove the same predecessor.
        """
        response = send_with_backoff(
            lambda: self._client.delete(
                f"{self._base_url}{OBJECT}/{self._bucket}/{path}", headers=self._headers
            ),
            self._sleep,
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # Status only: the body can echo a request that carried the
            # service-role key.
            raise StorageError(f"delete of {path} failed with {response.status_code}")
        return True
