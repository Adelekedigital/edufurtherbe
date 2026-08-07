"""The Supabase Admin API, for provisioning accounts.

**The most important thing in this file is an endpoint it never calls.**

Supabase offers three ways to bring a user into existence, and two of them send
email:

    POST /auth/v1/admin/users            creates silently  <- the only one used
    POST /auth/v1/invite                 sends an invitation email
    POST /auth/v1/admin/generate_link    can send a magic link

Provisioning runs against 1,200 real people during a cutover freeze. Reaching
the wrong endpoint does not fail a test or roll back — it delivers 1,200 emails
nobody agreed to send, and there is no undo. ``INVITE_ENDPOINTS`` exists so a
test can assert this client never touches one, which is a weaker guarantee than
being unable to and the strongest one available.

``email_confirm=True`` marks the address confirmed without a round trip. These
users verified their email in Bubble years ago; asking again would be asking
them to re-prove something we already migrated.

That silence is a product decision rather than an implementation detail — ADR
0018 §4 records why 1,200 people are given accounts without being told.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.core.errors import AppError
from app.infra.http.retry import send_with_backoff

# Create, list and fetch all hang off this one path. Named for the resource
# rather than for one verb, because it is used for three.
ADMIN_USERS = "/auth/v1/admin/users"

# Never called. Named so a test can assert the absence rather than trust it.
INVITE_ENDPOINTS = ("/auth/v1/invite", "/auth/v1/admin/generate_link")

# Supabase returns 422 with this code when the address already has an account.
# It is the expected outcome of resuming an interrupted run, not an error: the
# previous attempt created the account and failed before recording it.
ALREADY_REGISTERED = "email_exists"

# Supabase rate-limits the Admin API, and provisioning makes ~2,400 calls in one
# run. The policy itself lives in `infra/http/retry.py`: the asset uploader is its
# second caller, and a retry policy is exactly the thing that gets copied and then
# diverges silently, because both copies keep working and only one gets the fix.
LOOKUP_PAGE_SIZE = 200
MAX_LOOKUP_PAGES = 20


class AdminApiError(AppError):
    """The Admin API refused or was unreachable."""


@dataclass(frozen=True, slots=True)
class AuthUser:
    """A Supabase auth account, as much of it as provisioning needs."""

    id: UUID
    email: str


class SupabaseAdminClient:
    """Creates and looks up auth accounts.

    Holds the **service-role key**, which bypasses row-level security and can
    delete any user. It is never logged, never returned in an error, and never
    leaves this process — an error message quoting a failed request body would
    put it in a terminal and then in a CI log.
    """

    def __init__(
        self,
        base_url: str,
        service_role_key: str,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = service_role_key
        self._client = client
        # Injected so a retry test asserts the backoff without spending it.
        self._sleep = sleep

    def _send(self, request: Callable[[], httpx.Response]) -> httpx.Response:
        """Retry a 429, and nothing else. See `infra/http/retry.py`."""
        return send_with_backoff(request, self._sleep)

    @property
    def _headers(self) -> dict[str, str]:
        # Supabase wants the key in both places: `apikey` routes the request and
        # the bearer token authorises it.
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    def create_user(self, email: str) -> AuthUser:
        """Create a confirmed account, silently.

        Returns the existing account instead if the address already has one,
        which is what makes an interrupted run resumable: the second attempt
        finds what the first created rather than failing on it.
        """
        response = self._send(
            lambda: self._client.post(
                f"{self._base_url}{ADMIN_USERS}",
                headers=self._headers,
                json={"email": email, "email_confirm": True},
            )
        )

        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
            body = self._safe_json(response)
            if ALREADY_REGISTERED in str(body.get("error_code", "")) or "already" in str(
                body.get("msg", "")
            ):
                found = self.find_by_email(email)
                if found is not None:
                    return found
                raise AdminApiError(
                    "Supabase reports this address already has an account and a "
                    "lookup for it returned nothing. The account exists and cannot "
                    "be linked — check the Admin API's user list for this address."
                )

        if response.status_code >= httpx.codes.BAD_REQUEST:
            # Status only. The response body can echo the request, and the
            # request carried the service-role key.
            raise AdminApiError(f"create user failed with {response.status_code}")

        return self._to_user(self._safe_json(response))

    def _lookup_page(self, email: str, page: int) -> httpx.Response:
        """One page of the filtered user list.

        A method rather than a closure in the loop: a lambda capturing the loop
        variable is re-read when `_send` retries, and ruff flags the shape for
        exactly that reason.
        """
        return self._send(
            lambda: self._client.get(
                f"{self._base_url}{ADMIN_USERS}",
                headers=self._headers,
                params={"page": page, "per_page": LOOKUP_PAGE_SIZE, "filter": email},
            )
        )

    def find_by_email(self, email: str) -> AuthUser | None:
        """Look one account up by address, walking pages until it is found.

        Used on the resume path and by ``--verify``. Returns ``None`` rather than
        raising: "no such account" is an answer here, not a failure.

        **The paging is not an optimisation, it is the correctness.** Supabase's
        `filter` is a substring match returning newest-first, so `ada@x.com` can
        be answered with `xada@x.com` — and a single-row page would then contain
        no trace of the account being asked about. The exact comparison below
        correctly rejects the neighbour, `None` comes back, the caller creates,
        Supabase answers `email_exists`, and the user is stranded on the one path
        the whole design calls its recovery story. Asking for one row was the
        original defect; a green suite and a 43-user run both missed it, because
        the test fake ignored `per_page`.

        The ceiling is the other half. If a future Admin API ignored `filter`
        entirely, an unbounded walk would never terminate on a large project — and
        a `None` returned by giving up quietly is read upstream as "no account
        exists", which creates a duplicate. So it raises.
        """
        needle = email.lower()

        for page in range(1, MAX_LOOKUP_PAGES + 1):
            response = self._lookup_page(email, page)
            if response.status_code >= httpx.codes.BAD_REQUEST:
                raise AdminApiError(f"lookup failed with {response.status_code}")

            # `_safe_json` returns `dict[str, object]`, so the list has to be
            # narrowed before iterating — an Admin API that returned something
            # other than a list here would otherwise crash on the loop rather
            # than report a lookup failure.
            users = self._safe_json(response).get("users")
            if not isinstance(users, list):
                return None

            for candidate in users:
                if not isinstance(candidate, dict):
                    continue
                # Exact, because the server side was a substring match:
                # `ada@x.com` must not match `ada@xy.com`.
                if str(candidate.get("email", "")).lower() == needle:
                    return self._to_user(candidate)

            if len(users) < LOOKUP_PAGE_SIZE:
                # A short page is the end of the result set. This is the ordinary
                # exit, and it is why a normal lookup costs exactly one call.
                return None

        raise AdminApiError(
            f"a lookup for one address walked {MAX_LOOKUP_PAGES} full pages without "
            "an exact match; the Admin API filter is not narrowing the result"
        )

    def get(self, auth_id: UUID) -> AuthUser | None:
        """Fetch one account by id. ``None`` when Supabase does not have it.

        This is what ``--verify`` uses to close the gap ADR 0014 names as its
        weakest point: nothing else checks that an ``auth_id`` we stored still
        refers to a real account.

        ADR 0018 §3 closes that gap in **one direction only** — ours to the
        provider. An account the provider holds that no live row references is
        still invisible.
        """
        response = self._send(
            lambda: self._client.get(
                f"{self._base_url}{ADMIN_USERS}/{auth_id}", headers=self._headers
            )
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise AdminApiError(f"fetch failed with {response.status_code}")
        return self._to_user(self._safe_json(response))

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, object]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _to_user(payload: dict[str, object]) -> AuthUser:
        identifier = payload.get("id")
        email = payload.get("email")
        if not identifier or not email:
            raise AdminApiError("Admin API returned a user with no id or email")
        try:
            return AuthUser(id=UUID(str(identifier)), email=str(email))
        except ValueError as exc:
            raise AdminApiError("Admin API returned a non-uuid id") from exc
