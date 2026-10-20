"""The caller's own links: the list and its three writes (decision 275).

`CurrentUserDep` is the only identity involved: nothing takes a user from the
path or the body, and the store puts `user_id` in every `WHERE`, so a link id
that is somebody else's is the same `404` as one that does not exist.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from app.api.deps.core import CurrentUserDep, SessionDep
from app.api.schemas.links import LinkCreate, LinkPatch
from app.core.errors import NotFoundError
from app.domain.social_links import Link
from app.infra.db.user_links import add_link, change_link, delete_link, links_of

__all__ = ["ChangedLinkDep", "CreatedLinkDep", "DeletedLinkDep", "OwnLinksDep"]

_NO_SUCH_LINK = "you have no link with that id"


async def own_links(user: CurrentUserDep, session: SessionDep) -> list[Link]:
    """The caller's links in their order. An empty list, never `404`."""
    return await links_of(session, user["id"], published=False)


async def created_link(body: LinkCreate, user: CurrentUserDep, session: SessionDep) -> Link:
    """Add a link. Commits."""
    link = await add_link(session, user["id"], body.platform, body.value)
    await session.commit()
    return link


async def changed_link(
    link_id: UUID, body: LinkPatch, user: CurrentUserDep, session: SessionDep
) -> Link:
    """Change a link's value. Commits."""
    link = await change_link(session, user["id"], link_id, body.value)
    if link is None:
        raise NotFoundError(_NO_SUCH_LINK)
    await session.commit()
    return link


async def deleted_link(link_id: UUID, user: CurrentUserDep, session: SessionDep) -> None:
    """Delete a link. Commits. `404` if the caller has no such link."""
    if not await delete_link(session, user["id"], link_id):
        raise NotFoundError(_NO_SUCH_LINK)
    await session.commit()


OwnLinksDep = Annotated[list[Link], Depends(own_links)]
CreatedLinkDep = Annotated[Link, Depends(created_link)]
ChangedLinkDep = Annotated[Link, Depends(changed_link)]
DeletedLinkDep = Annotated[None, Depends(deleted_link)]
