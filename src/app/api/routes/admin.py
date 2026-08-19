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

from fastapi import APIRouter, status

from app.api.deps import (
    ApprovedInstitutionDep,
    DecidedMentorDep,
    ListedMentorDep,
    MentorHistoryDep,
    MergedInstitutionDep,
    PendingInstitutionsDep,
    PendingMentorsDep,
)
from app.api.schemas.admin import (
    PendingInstitutionRead,
    PendingMentorRead,
    StatusEventRead,
)
from app.api.schemas.common import Page
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
