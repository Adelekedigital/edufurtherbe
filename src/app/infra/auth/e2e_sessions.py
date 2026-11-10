"""Real Supabase sessions for the seeded E2E accounts, and nobody else.

The end-to-end runners (docs/e2e/README.md) sign in as seeded test accounts on
staging. They get a **real** session from Supabase rather than a shortcut on a
deployed server: `POST /auth/v1/admin/generate_link` makes a magic link and its
hashed token without sending any email (Supabase: "Generates email links and
OTPs to be sent via a custom email provider"; the Auth OpenAPI lists
`action_link` and `hashed_token` in its answer). The browser runner opens the
link; the API runner exchanges the hash at `POST /auth/v1/verify`
(`type=magiclink`, `token_hash`) for an access token, as `verifyOtp` does.

**Why this is not the provisioning client.** `SupabaseAdminClient` never calls
`generate_link` (`INVITE_ENDPOINTS`), because provisioning runs over 1,200 real
people. This subclass reuses its transport and refuses, before any request,
every address that is not a seeded E2E account on the reserved `.test` domain,
so no real person can be reached through it. `refuse_e2e_session` holds the
environment guards the script applies first.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings, explicit_environment
from app.infra.auth.admin import AdminApiError, AuthUser, SupabaseAdminClient
from app.infra.db.demo_seed import e2e_ref

__all__ = [
    "E2E_ENVIRONMENTS",
    "GENERATE_LINK",
    "VERIFY",
    "E2ESessionClient",
    "MagicLink",
    "NotAnE2EAccountError",
    "refuse_e2e_session",
]

GENERATE_LINK = "/auth/v1/admin/generate_link"
VERIFY = "/auth/v1/verify"
#: Supabase's verification type for a magic link (Auth OpenAPI, `/verify`).
MAGIC_LINK = "magiclink"


#: Where an E2E session may be made. Production is not, and never can be, here.
E2E_ENVIRONMENTS = frozenset({"staging"})


def refuse_e2e_session(settings: Settings, declared: str) -> str | None:
    """Why no session may be made here, or `None` when one may.

    Four locks, each closing a different mistake: `E2E_ALLOW` (nobody meant
    to), the operator's `--env` (meant somewhere else), the environment the
    settings were *explicitly* given (a shell that never set it defaults to
    `local`, which a plain "not production" check would pass), and
    `E2E_SUPABASE_PROJECT`, which `SUPABASE_URL` must name (a staging label on
    production credentials).
    """
    if not settings.e2e_allow:
        return "E2E_ALLOW is not set; refusing"
    if declared not in E2E_ENVIRONMENTS:
        return f"--env {declared!r} is not one E2E sessions may be made in"
    environment = explicit_environment(settings)
    if environment is None:
        return "EDUFURTHER_ENVIRONMENT is not set; refusing to guess where this is"
    if environment != declared:
        return f"EDUFURTHER_ENVIRONMENT is {environment!r}, not {declared!r}; refusing"
    if settings.supabase_url is None or settings.supabase_service_role_key is None:
        return "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are needed"
    expected = (settings.e2e_supabase_project or "").strip()
    if not expected:
        return "E2E_SUPABASE_PROJECT is not set; refusing"
    if not _is_project_url(settings.supabase_url, expected):
        # Neither ref is printed: the repository and its logs are public.
        return "SUPABASE_URL is not exactly the E2E_SUPABASE_PROJECT project's URL; refusing"
    return None


def _is_project_url(url: str, ref: str) -> bool:
    """`https://<ref>.supabase.co` and nothing else: no other host that merely
    contains it, and no path, query or fragment to hide it in. Stricter than
    `supabase_project_ref`, which finds a ref anywhere in a value."""
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == f"{ref}.supabase.co"
        and parts.port is None
        and not (parts.path or parts.query or parts.fragment)
        and not (parts.username or parts.password)
    )


class NotAnE2EAccountError(AdminApiError):
    """The address is not a seeded E2E account; nothing was sent."""


@dataclass(frozen=True, slots=True)
class MagicLink:
    """What `generate_link` answers that a runner uses. Both are credentials."""

    action_link: str
    hashed_token: str


class E2ESessionClient(SupabaseAdminClient):
    """Sessions for allowlisted E2E accounts. Never logs a link or a token."""

    def _refuse_unless_allowlisted(self, email: str) -> str:
        address = email.strip().lower()
        if e2e_ref(address) is None:
            raise NotAnE2EAccountError("not an E2E account; refusing")
        return address

    def ensure_account(self, email: str) -> AuthUser:
        """The account's Supabase identity, created silently if it has none."""
        return self.create_user(self._refuse_unless_allowlisted(email))

    def magic_link(self, email: str, redirect_to: str | None = None) -> MagicLink:
        """A one-time sign-in link for an allowlisted account. Sends no email."""
        address = self._refuse_unless_allowlisted(email)
        payload: dict[str, str] = {"type": MAGIC_LINK, "email": address}
        if redirect_to:
            payload["redirect_to"] = redirect_to
        response = self._send(
            lambda: self._client.post(
                f"{self._base_url}{GENERATE_LINK}", headers=self._headers, json=payload
            )
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise AdminApiError(f"generate link failed with {response.status_code}")
        body = self._safe_json(response)
        link, hashed = body.get("action_link"), body.get("hashed_token")
        if not isinstance(link, str) or not isinstance(hashed, str) or not link or not hashed:
            raise AdminApiError("generate link answered without a link or a token hash")
        return MagicLink(action_link=link, hashed_token=hashed)

    def access_token(self, link: MagicLink) -> str:
        """Exchange a magic link's hash for an access token, as a browser would."""
        response = self._send(
            lambda: self._client.post(
                f"{self._base_url}{VERIFY}",
                headers=self._headers,
                json={"type": MAGIC_LINK, "token_hash": link.hashed_token},
            )
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise AdminApiError(f"verify failed with {response.status_code}")
        token = self._safe_json(response).get("access_token")
        if not isinstance(token, str) or not token:
            raise AdminApiError("verify answered without an access token")
        return token
