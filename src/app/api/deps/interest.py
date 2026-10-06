"""Waiting to be told when a feature ships: the caller's own list, and its writes.

**Any signed-in account**, with no mentor gate — Explore's no-mentors state is a
mentee's (#365). `CurrentUserDep` is the only identity involved: nothing here
takes a user from the path or the body, so there is no id to tamper with, and
the store puts `user_id` in the `WHERE` on every read and write.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from app.api.deps.core import CurrentUserDep, SessionDep
from app.api.schemas.interest import InterestWrite
from app.core.errors import NotFoundError
from app.infra.db.interest_store import own_interests, register_interest, withdraw_interest

__all__ = [
    "OwnInterestsDep",
    "RegisteredInterestDep",
    "WithdrawnInterestDep",
]


async def registered_interest(
    body: InterestWrite, user: CurrentUserDep, session: SessionDep
) -> None:
    """Record the caller's interest. Commits.

    The key is already lowercased and shape-checked by `InterestWrite`, so a
    `422` happens before this runs and the store never sees a key the column
    would refuse.
    """
    await register_interest(session, user["id"], body.feature)
    await session.commit()


async def own_interest_list(user: CurrentUserDep, session: SessionDep) -> list[dict[str, Any]]:
    """Everything the caller is waiting for. Empty list, never `404`.

    **An account that has asked for nothing is not an error**, and the page that
    reads this renders a button either way — a `404` would make "no interests"
    and "no such person" the same answer, which is both wrong and useless to a
    client deciding what to show.
    """
    return await own_interests(session, user["id"])


async def withdrawn_interest(feature: str, user: CurrentUserDep, session: SessionDep) -> None:
    """Stop waiting for one feature. Commits. `404` if they were not.

    **Stated rather than treated as an idempotent `204`**, the same rule
    calendar disconnect follows: somebody who believes they turned something off
    needs to know if they did not.

    The path value is **not** validated against the slug pattern. A malformed
    key cannot be stored, so it matches no row and answers `404` — which is the
    truthful answer, and a `422` here would tell a client its spelling is wrong
    when what it actually is, is absent.
    """
    if not await withdraw_interest(session, user["id"], feature.lower()):
        raise NotFoundError("you are not waiting for that")
    await session.commit()


RegisteredInterestDep = Annotated[None, Depends(registered_interest)]
OwnInterestsDep = Annotated[list[dict[str, Any]], Depends(own_interest_list)]
WithdrawnInterestDep = Annotated[None, Depends(withdrawn_interest)]
