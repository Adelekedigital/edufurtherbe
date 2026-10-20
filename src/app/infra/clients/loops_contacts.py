"""Loops contacts, for the one thing the platform does with them: delete one.

The platform sends transactional email only (`notifications.LoopsNotifier`
keeps `addToAudience` false), so it never creates a contact. Migrated accounts
arrived with one, though, holding their real email and name, and a purged
account must not leave that behind at the email provider (Mentee Settings PRD).
"""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.infra.http.client import shared

#: `POST /api/v1/contacts/delete` with `{"email": ...}`; 404 is "no such contact"
#: (Loops' OpenAPI, `deleteContact`).
CONTACTS_DELETE = "https://app.loops.so/api/v1/contacts/delete"

TIMEOUT = httpx.Timeout(15.0)


class ContactsError(AppError):
    """Loops refused, or was unreachable."""


class LoopsContacts:
    """Deletes a Loops contact by email. The key travels on each request (#370)."""

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or shared(TIMEOUT)
        self._auth = {"Authorization": f"Bearer {api_key}"}

    def delete(self, email: str) -> None:
        """Remove the contact with this address. **None to remove is done**, so a
        purge retried after a partial run finishes. Raises :class:`ContactsError`
        naming only the status or the error's type: never the address or key."""
        try:
            response = self._client.post(CONTACTS_DELETE, headers=self._auth, json={"email": email})
        except httpx.HTTPError as exc:
            raise ContactsError(f"loops contact delete failed: {type(exc).__name__}") from None
        if response.status_code == httpx.codes.NOT_FOUND:
            return
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ContactsError(f"loops contact delete failed with {response.status_code}")


def loops_contacts_for(settings: Settings) -> LoopsContacts | None:
    """The contacts client, or ``None`` where Loops is not configured."""
    key = settings.loops_api_key
    return None if key is None else LoopsContacts(key.get_secret_value())
