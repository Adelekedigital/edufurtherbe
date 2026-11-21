"""The caller's dismissed notices (decision 356).

`CurrentUserDep` is the only identity: nothing takes a user from the path or
the body. Preview as user: the PUT is refused by the central read-only guard.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path

from app.api.deps.core import CurrentUserDep, SessionDep
from app.domain.seen_items import KEY_MAX_LENGTH, KEY_PATTERN
from app.infra.db.notice_store import dismiss_notice

__all__ = ["DismissedNoticeDep"]

NoticeKey = Annotated[
    str,
    Path(
        pattern=KEY_PATTERN,
        max_length=KEY_MAX_LENGTH,
        description=(
            "The frontend's name for the notice, e.g. `whats-new:2026-11-migration`: "
            f"lower-case letters and digits, then any of `:._-`, at most {KEY_MAX_LENGTH} "
            "characters, starting with a letter or digit."
        ),
        examples=["whats-new:2026-11-migration"],
    ),
]


async def dismissed_notice(key: NoticeKey, user: CurrentUserDep, session: SessionDep) -> str:
    """Record that the caller dismissed ``key``. Commits."""
    await dismiss_notice(session, user["id"], key)
    await session.commit()
    return key


DismissedNoticeDep = Annotated[str, Depends(dismissed_notice)]
