"""Your own links on your profile (`/me/links`, decision 275).

X, YouTube, Instagram, Facebook and TikTok take one link each; `website` takes
up to three. **LinkedIn is not here**: it stays `social_linkedin` on the
profile, because the mentor application and its review read it.

Every link is stored and returned in one canonical `https://` form, and `value`
is what follows the platform's prefix, so a settings page can show the prefix
and an input without parsing anything.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps.links import ChangedLinkDep, CreatedLinkDep, DeletedLinkDep, OwnLinksDep
from app.api.schemas.common import Page
from app.api.schemas.links import LinkRead
from app.domain.social_links import MAX_WEBSITES

router = APIRouter(prefix="/api/v1/me/links", tags=["links"])

RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}
NOT_YOURS: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": "You have no link with that id. Somebody else's link is the same answer."
    }
}
INVALID: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The body failed validation. A value that is not a handle or link on "
            "the platform is reported at `/value`."
        )
    }
}


@router.get(
    "",
    response_model=Page[LinkRead],
    summary="Your links",
    description=(
        "Your links in your order (`position`, then the oldest first). An empty "
        "`data` means you have none, never a `404`. `next_cursor` is always "
        f"`null`: there are at most {5 + MAX_WEBSITES}, so the list comes whole."
    ),
    responses=RESPONSES,
)
async def list_own_links(links: OwnLinksDep) -> Page[LinkRead]:
    return Page(data=[LinkRead.of(link) for link in links], next_cursor=None)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=LinkRead,
    summary="Add a link",
    description=(
        "Adds a link at the end of your list and returns it. Send the handle as "
        "typed after the platform's prefix, or a full link on the platform's own "
        "site; it is stored in one canonical form."
    ),
    responses=RESPONSES
    | INVALID
    | {
        status.HTTP_409_CONFLICT: {
            "description": (
                "`/problems/link-platform-taken`: you already have a link on this "
                "platform; edit or delete it instead. "
                "`/problems/website-limit-reached`: you already list "
                f"{MAX_WEBSITES} websites; the problem's `limit` says how many."
            )
        }
    },
)
async def add_own_link(link: CreatedLinkDep, response: Response) -> LinkRead:
    response.headers["Location"] = f"/api/v1/me/links/{link.id}"
    return LinkRead.of(link)


@router.patch(
    "/{link_id}",
    response_model=LinkRead,
    summary="Change a link",
    description=(
        "A new `value` for one of your links. The platform is fixed once the "
        "link exists: to move platforms, delete it and add another."
    ),
    responses=RESPONSES | NOT_YOURS | INVALID,
)
async def change_own_link(link: ChangedLinkDep) -> LinkRead:
    return LinkRead.of(link)


@router.delete(
    "/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a link",
    description="Removes one of your links. The others keep their order.",
    responses=RESPONSES | NOT_YOURS,
)
async def delete_own_link(_: DeletedLinkDep) -> None:
    return None
