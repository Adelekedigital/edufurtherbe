"""The admin user picker: search the users an admin may preview (decision 310)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import PRIVATE_NO_STORE, AdminUsersDep
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_users import PAGE_SIZE, AdminUserRead
from app.api.schemas.common import MAX_QUERY_LENGTH, Page

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get(
    "/users",
    response_model=Page[AdminUserRead],
    summary="Search the users an admin may preview",
    description=(
        f"Live users who hold no live admin grant, {PAGE_SIZE} a page, ordered by name: first "
        "and last joined, ignoring case, or the email for a user with no name. "
        "A deleted account and anyone with a live admin grant are never listed; "
        "a user whose grants were all revoked is listed like anyone else.\n\n"
        "`q` narrows to users whose email, first name, last name or full name "
        "contains it, ignoring case; `%` and `_` match themselves. A blank `q` "
        "is everyone. `role` filters on `primary_role` (`all`, the default, does "
        "not).\n\n"
        "Every live admin grant may search. Never cached (`private, no-store`)."
    ),
    responses={
        **ADMIN_RESPONSES,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`cursor` is not one this endpoint issued, `role` is not `all`, "
                f"`mentor` or `mentee`, or `q` is over {MAX_QUERY_LENGTH} characters."
            )
        },
    },
)
async def admin_users(page: AdminUsersDep, response: Response) -> Page[AdminUserRead]:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return page
