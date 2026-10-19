"""Deleting your own account (`DELETE /me`)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps.core import CurrentUserDep, SessionDep, _configured, bearer
from app.api.deps.sessions import RefundPolicyDep, _calendar
from app.api.schemas.user import AccountDeletionWrite
from app.core.errors import NotFoundError
from app.infra.auth.admin import AdminApiError, SupabaseAdminClient, auth_admin_for
from app.infra.db.account_deletion import delete_account

logger = logging.getLogger(__name__)


def _auth_admin(request: Request) -> SupabaseAdminClient | None:
    """The Admin API, off `app.state` first like every other adapter seam."""
    wired: SupabaseAdminClient | None = getattr(request.app.state, "auth_admin", None)
    if wired is not None:
        return wired
    return auth_admin_for(_configured(request))


async def deleted_account(
    payload: AccountDeletionWrite,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
    refunds: RefundPolicyDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    """Delete the caller's account, commit, then sign them out everywhere.

    **Sign-out after the commit, and its failure is not the request's.** The
    deletion is what was asked, and it holds whether or not Supabase answers: a
    refresh token that survives reaches no account here, because every
    authenticated request already refuses a deleted one. So a failure is logged,
    never with the token, and the answer is still `204`.
    """
    del payload  # validated by FastAPI: only `DELETE` reaches here
    deleted = await delete_account(
        session,
        user["id"],
        now=dt.datetime.now(dt.UTC),
        refunds=refunds,
        calendar=_calendar(request),
    )
    if not deleted:  # pragma: no cover - the caller was resolved as live above
        raise NotFoundError("no account is linked to this identity")
    await session.commit()
    logger.info("account %s deleted by its owner", user["id"])

    admin = _auth_admin(request)
    if admin is None or credentials is None:
        logger.warning("sign-out skipped for deleted account %s: no Admin API", user["id"])
        return
    try:
        await asyncio.to_thread(admin.sign_out_everywhere, credentials.credentials)
    except AdminApiError as exc:
        logger.warning("sign-out after deleting account %s failed: %s", user["id"], exc)


DeletedAccountDep = Annotated[None, Depends(deleted_account)]
