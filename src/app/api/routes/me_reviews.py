"""Reviews written *about* the caller, and asking somebody to look at one.

**Its own module, and a sixth router on ``/api/v1/me``** — the prefix is shared,
the subject is not. `reviews.py` is the *author's* surface: writing one, editing
it inside the ten-minute window, and finding sessions still owed. This is the
subject's, and the two have opposite ideas of whose text is whose.

**Nothing here removes a review.** A mentor who could hide reviews they dislike
would make the rating meaningless, and a mentee reading a five-star profile
would be misled — which is precisely what the review system exists to prevent.
Reporting asks an admin to look; only they can uphold it.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import OwnReviewsDep, ReportedReviewDep
from app.api.schemas.common import Page
from app.api.schemas.review_reports import OwnReviewRead, ReviewReportRead

router = APIRouter(prefix="/api/v1/me", tags=["review-moderation"])

UNAUTHORISED: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    }
}


@router.get(
    "/reviews",
    response_model=Page[OwnReviewRead],
    summary="Reviews written about you",
    description=(
        "Newest first, and **including ones that have been withdrawn** — which "
        "the public profile cannot show, because absence is the whole point of "
        "withdrawal there. You are the one person who needs to see that a "
        "review went, and `report` says whether that was your doing or the "
        "author's.\n\n"
        "`report` is your own report on that review, or null. Null means not "
        "reported, which is a different state from reported-and-dismissed.\n\n"
        "The author is a first name and an initial, exactly as on the public "
        "page — you do not get a fuller name than a stranger does."
    ),
    responses=UNAUTHORISED,
)
async def list_own_reviews(page: OwnReviewsDep) -> Page[OwnReviewRead]:
    rows, cursor = page
    return Page(data=[OwnReviewRead.from_row(row) for row in rows], next_cursor=cursor)


@router.post(
    "/reviews/{review_id}/report",
    response_model=ReviewReportRead,
    status_code=status.HTTP_201_CREATED,
    summary="Ask somebody to look at a review of you",
    description=(
        "Files a report for an admin to adjudicate.\n\n"
        "**This hides nothing.** The review stays on your profile and in your "
        "average until an admin upholds the report — hiding on report would be "
        "the same power as hiding outright, exercised one complaint at a "
        "time.\n\n"
        "Only the subject of a review may report it, which the database "
        "enforces rather than this route: a review about somebody else answers "
        "`404`, not `403`, because confirming it exists would turn an "
        "authorization answer into a way to enumerate reviews.\n\n"
        "Reporting the same review twice answers `409`. A withdrawn review is "
        "still reportable — its author may have taken it back rather than "
        "moderation removing it, and you may still want the record examined."
    ),
    responses={
        **UNAUTHORISED,
        status.HTTP_404_NOT_FOUND: {"description": "No review of yours carries that id."},
        status.HTTP_409_CONFLICT: {"description": "You have already reported this review."},
    },
)
async def report_own_review(report: ReportedReviewDep, response: Response) -> ReviewReportRead:
    response.headers["Location"] = "/api/v1/me/reviews"
    return ReviewReportRead.model_validate(report)
