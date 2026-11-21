"""The one-time notices the caller has seen (decision 356).

**Another router on `/api/v1/me`**, as `me_referrals` records: the prefix is
shared, the subject is not. Read back on `GET /me` → `seen_items`.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import DismissedNoticeDep
from app.api.errors import UNAUTHORISED
from app.domain.seen_items import MAX_SEEN_ITEMS

router = APIRouter(prefix="/api/v1/me", tags=["users"])


@router.put(
    "/seen-items/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Mark a notice as seen",
    description=(
        "Records that the caller has seen, finished or closed the notice named "
        '`key` (a "what\'s new" tour, an announcement, a nudge), so the app does '
        "not show it again on any device. `GET /me` → `seen_items` lists the keys.\n\n"
        "The backend gives a key no meaning: a new notice is a new key, with no "
        "backend change.\n\n"
        "**Idempotent**: a repeat answers `204` and keeps the first time it was "
        f"seen. The caller keeps at most {MAX_SEEN_ITEMS} keys; a new key past that "
        "drops their oldest. A key cannot be un-seen.\n\n"
        "`422` when `key` is not of the shape below. Refused in a preview, as "
        "every write is."
    ),
    responses=UNAUTHORISED,
)
async def mark_notice_seen(_: DismissedNoticeDep) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)
