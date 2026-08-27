"""The review surface: the two queues, and the actions that drain them.

**Every other endpoint group protects a user's rows from other users. This one
protects an action from callers who may not take it.** The control is the
caller's grant rather than a row scope, so the failure to chase is the opposite
one — and each route has four cases rather than two: no token, a user with no
grant, a *revoked* admin, and a live one.

A caller without the grant receives **404, not 403**. Same reasoning the rest of
the service uses: 403 confirms the endpoint exists and that somebody is
permitted to use it, which is precisely what an unprivileged caller should not
learn from asking.

`AdminRole` distinguishes what a grant is *for* — `mentor_approval` exists to
approve mentors and says nothing about curating the catalogue — so the routes
below ask for the role they need rather than for "an admin". `super_admin` is
admitted everywhere without being named at each one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import (
    AdminCreditGrantDep,
    ApprovedInstitutionDep,
    DecidedMentorDep,
    DecidedReportDep,
    ListedMentorDep,
    MentorHistoryDep,
    MergedInstitutionDep,
    ModerationQueueDep,
    PendingInstitutionsDep,
    PendingMentorsDep,
)
from app.api.schemas.admin import (
    PendingInstitutionRead,
    PendingMentorRead,
    StatusEventRead,
)
from app.api.schemas.admin_credits import AdminCreditGrantRead
from app.api.schemas.common import Page
from app.api.schemas.review_reports import ModeratedReportRead, ModeratedReviewRead
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ADMIN_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such endpoint — **or** you do not hold a grant that may use it, "
            "**or** the record does not exist. Deliberately indistinguishable: a "
            "403 would tell a caller without a grant that the endpoint is real "
            "and that somebody may call it."
        )
    },
}


@router.get(
    "/institutions/pending",
    response_model=Page[PendingInstitutionRead],
    summary="Institutions awaiting review",
    description=(
        "Ordered by **how many education entries point at each one**, then "
        "oldest first.\n\n"
        "That count is the decision: eight entries on one spelling means eight "
        "people typed the same school and it should be approved; one entry that "
        "closely resembles an approved name is a typo and should be merged.\n\n"
        "A row nobody references yet still appears — it is the ordinary state of "
        "something typed a minute ago, and a queue that hides new entries until "
        "a second person picks the same school is not a queue."
    ),
    responses=ADMIN_RESPONSES,
)
async def institution_queue(rows: PendingInstitutionsDep) -> Page[PendingInstitutionRead]:
    return Page(data=[PendingInstitutionRead(**row) for row in rows])


@router.post(
    "/institutions/{institution_id}/approve",
    summary="Approve an institution",
    description=(
        "Makes it selectable in search. Approving one that is already approved "
        "changes nothing and reports so, rather than failing — a reviewer "
        "double-clicking is not an error."
    ),
    responses=ADMIN_RESPONSES,
)
async def approve(changed: ApprovedInstitutionDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no institution awaiting review with that id")
    return {"approved": True}


@router.post(
    "/institutions/{institution_id}/merge",
    summary="Merge a duplicate institution",
    description=(
        "Moves every education entry from the duplicate onto the institution in "
        "`winning_id`, marks the duplicate merged, and records where it went — "
        "**in one transaction**.\n\n"
        "The entries are repointed rather than resolved at read time, so the "
        "chain collapses here and no later query has to follow it. Merging into "
        "a row that has itself been merged is refused with 409: merge into its "
        "target instead. So is merging something into itself.\n\n"
        "Recoverable if wrong — `school_name_raw` on each entry still holds what "
        "the user originally typed."
    ),
    responses=ADMIN_RESPONSES
    | {
        status.HTTP_409_CONFLICT: {
            "description": "The target is itself merged, is the same row, or does not exist."
        }
    },
)
async def merge(moved: MergedInstitutionDep) -> dict[str, int]:
    return {"entries_moved": moved}


@router.get(
    "/mentors/pending",
    response_model=Page[PendingMentorRead],
    summary="Mentor applications awaiting a decision",
    description="Oldest first — a queue rather than a stack.",
    responses=ADMIN_RESPONSES,
)
async def mentor_queue(rows: PendingMentorsDep) -> Page[PendingMentorRead]:
    return Page(data=[PendingMentorRead(**row) for row in rows])


@router.post(
    "/mentors/{user_id}/decision",
    summary="Approve or decline a mentor application",
    description=(
        "**Approval and listing move together.** Approving sets both, so the "
        "mentor is live in search immediately; declining unlists them and "
        "records `never_approved` as the reason. Splitting the two would allow a "
        "mentor who is approved and invisible, with nothing to say which was "
        "meant.\n\n"
        "A reason is optional on decline — better to give one, but requiring it "
        "turns a clear-cut decision into a form to argue with.\n\n"
        "`approved_by` records **who decided**, including when an admin decides "
        "their own application. That is permitted; it is visible rather than "
        "prevented, because on a small team blocking it means the only admin can "
        "never be approved at all."
    ),
    responses=ADMIN_RESPONSES,
)
async def decide(changed: DecidedMentorDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no mentor application for that user")
    return {"decided": True}


@router.post(
    "/mentors/{user_id}/listing",
    summary="List or unlist a mentor",
    description=(
        "Moves a mentor's listing **without touching their approval** — the "
        "transition this log exists for. Unlisting an approved mentor takes them "
        "out of the directory and leaves the approval intact; listing puts them "
        "back.\n\n"
        "Recorded as its own event, so who unlisted somebody is answerable even "
        "though it no longer follows from the approval decision."
    ),
    responses=ADMIN_RESPONSES,
)
async def set_mentor_listing(changed: ListedMentorDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no mentor profile for that user")
    return {"changed": True}


@router.get(
    "/mentors/{user_id}/history",
    response_model=Page[StatusEventRead],
    summary="A mentor's status history",
    description=(
        "Every approval and listing transition, newest first, with who made it "
        "and why.\n\n"
        "**Filter by kind, by date, or both.** `status` repeats to widen — "
        "`?status=approved&status=declined` — and omitting it means every kind "
        "rather than none. `since` and `until` are UTC instants forming a "
        "half-open window `[since, until)`, so two adjacent ranges partition "
        "the log with no event in both and none missed; a closed upper bound "
        "would return an event landing exactly on midnight in both March and "
        "April.\n\n"
        "Instants rather than dates, and a timezone-less value is refused "
        "rather than guessed at: this log has no owning mentor whose day it "
        "could mean, and the reviewer's own zone is not something the server "
        "knows.\n\n"
        "`created_by` is null on rows written by the migration that introduced "
        "this log — nobody made those decisions at that moment, and inventing an "
        "actor would be worse than an honest null."
    ),
    responses=ADMIN_RESPONSES,
)
async def mentor_status_history(rows: MentorHistoryDep) -> Page[StatusEventRead]:
    return Page(data=[StatusEventRead(**row) for row in rows])


@router.get(
    "/reviews",
    response_model=Page[ModeratedReviewRead],
    summary="Reviews, for moderation",
    description=(
        "Every review, newest first — **not only the reported ones**. A "
        "moderator judging a complaint needs the surrounding record; one review "
        "in isolation says nothing about whether an author is a pattern. Pass "
        "`reported=true` to narrow it to reviews carrying a report.\n\n"
        "Withdrawn reviews are included, including ones this queue withdrew. "
        "Hiding them would make the only view of a moderation decision the one "
        "that cannot show its result.\n\n"
        "Carries `private_review`, which no other read model does: it is "
        "feedback about the platform and the mentor never sees it, but a "
        "complaint about text the moderator cannot read is unanswerable.\n\n"
        "Every live admin grant may look. Deciding is `super_admin` only."
    ),
    responses=ADMIN_RESPONSES,
)
async def read_moderation_queue(page: ModerationQueueDep) -> Page[ModeratedReviewRead]:
    rows, cursor = page
    return Page(data=[ModeratedReviewRead.from_row(row) for row in rows], next_cursor=cursor)


@router.post(
    "/review-reports/{report_id}/decision",
    response_model=ModeratedReportRead,
    summary="Rule on a report",
    description=(
        "**Upholding removes the review from the profile and the average**, by "
        "setting `deleted_at` — the column that already exists for moderation, "
        "and which the profile's partial index already honours. Dismissing "
        "changes nothing about the review and records that somebody looked.\n\n"
        "The decision and its effect are one transaction: an upheld report "
        "whose review is still on the profile would be a moderator being told "
        "they acted when they did not.\n\n"
        "Deciding twice answers `409`. A second decision would rewrite the "
        "first, and the record of who decided what would be whatever the last "
        "admin clicked."
    ),
    responses={
        **ADMIN_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "No report carries that id."},
        status.HTTP_409_CONFLICT: {"description": "That report has already been decided."},
    },
)
async def decide_review_report(decision: DecidedReportDep) -> ModeratedReportRead:
    """No 201: this creates nothing. It resolves a row that already existed,
    which is an update however it reads."""
    return ModeratedReportRead.model_validate(decision)


@router.post(
    "/credits",
    response_model=AdminCreditGrantRead,
    summary="Give credits to one or more users",
    description=(
        "**Support's way to make somebody whole.** Every other credit on the "
        "platform is automatic — onboarding, a qualifying invite, the monthly "
        "grant, a refund, the migration — so this is the only path that does "
        "not require a database console.\n\n"
        "**Every live admin grant may use it.** Crediting somebody is support "
        "work that whoever is on shift has to be able to do, which is a "
        "deliberate widening from the `super_admin` gate on moderation.\n\n"
        "**Bulk, because the real case is bulk.** An outage costs a cohort, not "
        "a person. Ids that name nobody — or a deleted account — come back in "
        "`unresolved` rather than failing the request: an admin correcting six "
        "people should not lose five because one id was stale. A repeated id is "
        "credited once.\n\n"
        "**The credit expires like any other**, at the end of the month. Only "
        "the onboarding starter never expires, and that is because of what it "
        "is for rather than because grants are permanent.\n\n"
        "`quantity` is capped at the configured monthly grant, and an over-cap "
        "request is refused rather than clamped — an admin who typed a larger "
        "number meant it, and quietly granting less would leave them believing "
        "otherwise.\n\n"
        "**Answers `200`, not `201`.** A bulk grant creates one lot per "
        "recipient, in as many places, so there is no single `Location` to "
        "point at — and the body is a report of what landed rather than a "
        "representation of one created thing.\n\n"
        "**`Idempotency-Key` is required.** Retries must reuse it: a "
        "double-submitted grant is not something an admin can notice and undo, "
        "because the credits are spendable the moment they land."
    ),
    responses={
        **ADMIN_RESPONSES,
        status.HTTP_409_CONFLICT: {
            "description": "A request with this `Idempotency-Key` is still in flight."
        },
    },
)
async def grant_credits_to_users(
    granted: AdminCreditGrantDep, response: Response
) -> dict[str, Any]:
    body, status_code, replayed = granted
    response.status_code = status_code
    # **Says so on a replay**, the way booking does: an admin retrying after a
    # timeout needs to know whether this attempt created the credits or merely
    # found them, because the difference decides whether they grant again.
    response.headers["Idempotent-Replay"] = "true" if replayed else "false"
    return body
