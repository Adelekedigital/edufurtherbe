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
from uuid import UUID

from fastapi import APIRouter, Response, status

from app.api.deps import (
    AddedNoteDep,
    AdminCreditGrantDep,
    AdminGrantHistoryDep,
    ApprovedInstitutionDep,
    BulkDecidedDep,
    DecidedMentorDep,
    DecidedReportDep,
    FeaturedWeekRemovedDep,
    FeaturedWeeksDep,
    FeaturedWeekSetDep,
    ListedMentorDep,
    MentorHistoryDep,
    MentorNotesDep,
    MergedInstitutionDep,
    ModerationQueueDep,
    PendingInstitutionsDep,
    PendingMentorsDep,
    RestoredAccountDep,
    SessionMissedReasonsDep,
    SessionTypeLimitSetDep,
)
from app.api.routes.sessions import REPLAYED_HEADER
from app.api.schemas.admin import (
    AdminNoteRead,
    BulkDecisionRead,
    FeaturedWeekRead,
    FeaturedWeekSetRead,
    InstitutionApprovedRead,
    InstitutionMergedRead,
    MentorDecidedRead,
    MentorListingChangedRead,
    PendingInstitutionRead,
    PendingMentorRead,
    SessionTypeLimitRead,
    StatusEventRead,
)
from app.api.schemas.admin_credits import AdminCreditGrantRead, AdminGrantHistoryRead
from app.api.schemas.common import Page
from app.api.schemas.review_reports import ModeratedReportRead, ModeratedReviewRead
from app.api.schemas.sessions import AdminMissedReasonRead
from app.core.errors import NotFoundError
from app.domain.session_type_limit import DEFAULT_SESSION_TYPE_LIMIT, SESSION_TYPE_LIMIT_CEILING

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
        "Ordered by **how many education entries and goal schools point at "
        "each one** (`uses`), then oldest first.\n\n"
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
    response_model=InstitutionApprovedRead,
    summary="Approve an institution",
    description=(
        "Makes it selectable in search. Approving one that is already approved "
        "changes nothing and reports so, rather than failing — a reviewer "
        "double-clicking is not an error."
    ),
    responses=ADMIN_RESPONSES,
)
async def approve(changed: ApprovedInstitutionDep) -> InstitutionApprovedRead:
    if not changed:
        raise NotFoundError("no institution awaiting review with that id")
    return InstitutionApprovedRead(approved=True)


@router.post(
    "/institutions/{institution_id}/merge",
    response_model=InstitutionMergedRead,
    summary="Merge a duplicate institution",
    description=(
        "Moves every education entry and goal school from the duplicate onto the "
        "institution in `winning_id`, marks the duplicate merged, and records "
        "where it went — "
        "**in one transaction**.\n\n"
        "The entries are repointed rather than resolved at read time, so the "
        "chain collapses here and no later query has to follow it. Merging into "
        "a row that has itself been merged is refused with 409: merge into its "
        "target instead. So is merging something into itself.\n\n"
        "Recoverable if wrong — `school_name_raw` on each entry and school still "
        "holds what the user originally typed. `entries_moved` counts education "
        "entries only."
    ),
    responses=ADMIN_RESPONSES
    | {
        status.HTTP_409_CONFLICT: {
            "description": "The target is itself merged, is the same row, or does not exist."
        }
    },
)
async def merge(moved: MergedInstitutionDep) -> InstitutionMergedRead:
    return InstitutionMergedRead(entries_moved=moved)


@router.get(
    "/mentors/pending",
    response_model=Page[PendingMentorRead],
    summary="Mentor applications awaiting a decision",
    description=(
        "Submitted applications only, oldest `submitted_at` first — a queue "
        "rather than a stack. A mentor profile saved partway through onboarding "
        "is not an application until the mentor submits it."
    ),
    responses=ADMIN_RESPONSES,
)
async def mentor_queue(rows: PendingMentorsDep) -> Page[PendingMentorRead]:
    return Page(data=[PendingMentorRead(**row) for row in rows])


@router.post(
    "/mentors/{user_id}/decision",
    response_model=MentorDecidedRead,
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
async def decide(changed: DecidedMentorDep) -> MentorDecidedRead:
    if not changed:
        raise NotFoundError("no mentor application for that user")
    return MentorDecidedRead(decided=True)


@router.post(
    "/mentors/{user_id}/listing",
    response_model=MentorListingChangedRead,
    summary="List or unlist a mentor",
    description=(
        "Moves a mentor's listing **without touching their approval** — the "
        "transition this log exists for. Unlisting an approved mentor takes them "
        "out of the directory and leaves the approval intact; listing puts them "
        "back.\n\n"
        "Recorded as its own event, so who unlisted somebody is answerable even "
        "though it no longer follows from the approval decision.\n\n"
        "**Unlisting needs a `reason`**, which other admins read and the mentor "
        "never does: without one it is a `422` at `/reason` and nothing changes. "
        "When it pauses an approved mentor who is listed or paused by "
        "themselves, the mentor gets the `mentor_paused_by_admin` email, without "
        "the reason. Listing again sends nothing.\n\n"
        "**A mentor who paused themselves is theirs to resume**: listing them is "
        "`409` `/problems/mentor-paused-themselves`."
    ),
    responses=ADMIN_RESPONSES
    | {
        status.HTTP_409_CONFLICT: {
            "description": (
                "`/problems/mentor-paused-themselves`: the mentor paused "
                "themselves, and only they can resume."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Unlisting without a `reason`, or a reason over 1000 characters."
        },
    },
)
async def set_mentor_listing(changed: ListedMentorDep) -> MentorListingChangedRead:
    if not changed:
        raise NotFoundError("no mentor profile for that user")
    return MentorListingChangedRead(changed=True)


@router.post(
    "/mentors/approvals",
    response_model=BulkDecisionRead,
    summary="Approve several applications",
    description=(
        "Approves each of `user_ids` (1 to 50) as `POST /mentors/{user_id}/decision` "
        "approves one: listed on Explore at once, and the approved email sent. "
        "**Each mentor in its own transaction**, so one failure undoes nothing "
        "else.\n\n"
        "Only an application still in review is approved; any other id comes "
        "back in `failed` (no such mentor, a profile not yet submitted, one "
        "already approved or declined). A repeated id counts once. A retry is "
        "safe: an approved mentor is no longer in review, so nobody is approved "
        "or emailed twice. Answers `200` whatever the split. Approval only: an "
        "unknown field is a `422`.\n\n"
        "Mentor-approval admins and super admins."
    ),
    responses=ADMIN_RESPONSES,
)
async def bulk_decide(decided: BulkDecidedDep) -> BulkDecisionRead:
    approved, failed = decided
    return BulkDecisionRead(approved=approved, failed=failed)


@router.get(
    "/mentors/{user_id}/notes",
    response_model=Page[AdminNoteRead],
    summary="A mentor's internal notes",
    description=(
        "Every note admins left on this mentor, oldest first, with who wrote it. "
        "**Only admins ever read these**: no endpoint shows them to the mentor. "
        "One page, `next_cursor` always null. `404` for a user who is not a "
        "mentor, or whose profile or account was deleted.\n\n"
        "Every live admin grant may look."
    ),
    responses=ADMIN_RESPONSES,
)
async def mentor_notes(rows: MentorNotesDep) -> Page[AdminNoteRead]:
    return Page(data=[AdminNoteRead.from_row(row) for row in rows])


@router.post(
    "/mentors/{user_id}/notes",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminNoteRead,
    summary="Add an internal note on a mentor",
    description=(
        "Adds a note other admins see in the drawer: `body` is 1 to 2000 "
        "characters, trimmed. Notes cannot be edited or removed. `Location` "
        "names the mentor's notes. `404` for a user who is not a mentor, or "
        "whose profile or account was deleted.\n\n"
        "Mentor-approval admins and super admins."
    ),
    responses=ADMIN_RESPONSES,
)
async def add_mentor_note(user_id: UUID, note: AddedNoteDep, response: Response) -> AdminNoteRead:
    response.headers["Location"] = f"{router.prefix}/mentors/{user_id}/notes"
    return AdminNoteRead.from_row(note)


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
    response.headers[REPLAYED_HEADER] = "true" if replayed else "false"
    return body


@router.get(
    "/credits",
    response_model=Page[AdminGrantHistoryRead],
    summary="Every credit an admin has granted",
    description=(
        "Newest first. **The record `POST /credits` exists to leave** — credits "
        "that appear in somebody's balance with nobody accountable for them are "
        "the state the table was added to prevent.\n\n"
        "**Granted and remaining are both here**, because they answer different "
        "questions. *We gave them three* and *they still have three* are not the "
        "same fact, and a history showing only the first cannot say whether a "
        "goodwill gesture reached anybody — or whether it expired unspent.\n\n"
        "Every admin sees every grant, their own and each other's: an audit only "
        "one person can read is not an audit. `granted_by` narrows to one admin "
        "for whoever wants to check their own.\n\n"
        "**Recipients who have since closed their account are still listed.** A "
        "credit granted to somebody now gone is exactly what an audit is for, "
        "and hiding them would quietly shorten the history rather than answer "
        "the question asked of it."
    ),
    responses=ADMIN_RESPONSES,
)
async def admin_credit_history(page: AdminGrantHistoryDep) -> Page[AdminGrantHistoryRead]:
    rows, cursor = page
    return Page(data=[AdminGrantHistoryRead.from_row(row) for row in rows], next_cursor=cursor)


@router.put(
    "/featured-mentor/{week_start}",
    response_model=FeaturedWeekSetRead,
    summary="Choose the featured mentor for a week",
    description=(
        "Overrides the automatic rotation for one week: the current one, or up to "
        "eight ahead. `week_start` is the week's **Monday** (UTC); anything else, a "
        "past week, or a week further out is a `422`, as is a mentor who cannot be "
        "booked now.\n\n"
        "**The choice counts as that mentor's rotation turn**, and a mentor the "
        "rotation had already picked for the week **gets their turn back**. A "
        "second choice for the same week replaces the first.\n\n"
        "If the chosen mentor pauses before or during their week they are skipped "
        "and the rotation fills the week, so the public card is never empty "
        "because of an override. The public `/featured-mentor` caches for 60 "
        "seconds, so a change shows within a minute.\n\n"
        "Mentor-approval admins and super admins (settled decision #188)."
    ),
    responses=ADMIN_RESPONSES,
)
async def choose_featured(chosen: FeaturedWeekSetDep) -> FeaturedWeekSetRead:
    return FeaturedWeekSetRead(week_start=chosen)


@router.delete(
    "/featured-mentor/{week_start}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw a week's featured choice",
    description=(
        "Removes an admin's choice; the rotation picks for that week as usual. "
        "`404` when the week holds no admin choice. Past weeks are history and "
        "are refused with a `422`."
    ),
    responses=ADMIN_RESPONSES,
)
async def withdraw_featured(removed: FeaturedWeekRemovedDep) -> Response:
    if not removed:
        raise NotFoundError("no admin choice for that week")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/featured-mentors",
    response_model=Page[FeaturedWeekRead],
    summary="The featured-mentor schedule",
    description=(
        "Each week from `since` (default: eight weeks ago), newest first: who, "
        "whether the rotation or an admin chose them, which admin, and whether "
        "they can still be booked."
    ),
    responses=ADMIN_RESPONSES,
)
async def featured_weeks_list(rows: FeaturedWeeksDep) -> Page[FeaturedWeekRead]:
    return Page(data=[FeaturedWeekRead(**row) for row in rows])


@router.post(
    "/users/{user_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Restore a deleted account",
    description=(
        "Brings back an account its owner deleted with `DELETE /me`, while its "
        "**30-day window** is open; the owner signs in again as before. "
        "**Super admin only.**\n\n"
        "Only the account comes back: sessions the deletion ended stay ended, "
        "expired credits stay expired, and a mentor stays unlisted until listed "
        "again. `404` once the window has closed (the account is purged), for an "
        "account that was never self-deleted, and for a caller without the grant. "
        "`409` when the account's email or handle now belongs to a live account."
    ),
    responses=ADMIN_RESPONSES
    | {status.HTTP_409_CONFLICT: {"description": "The email or handle is now taken."}},
)
async def restore_deleted_account(restored: RestoredAccountDep) -> None:
    if not restored:
        raise NotFoundError("no restorable account")


@router.put(
    "/mentors/{user_id}/session-type-limit",
    response_model=SessionTypeLimitRead,
    summary="Set a mentor's session-type limit",
    description=(
        "Raises or lowers how many session types one mentor may hold; "
        '`{"limit": null}` resets it to the platform default '
        f"({DEFAULT_SESSION_TYPE_LIMIT}). "
        "Every session type not deleted counts, hidden and scheduled for deletion "
        "included. **Super admin only.**\n\n"
        f"**A number is pinned, even the default's**: `{DEFAULT_SESSION_TYPE_LIMIT}` "
        "keeps this mentor there if the default changes, and answers "
        "`is_default: false`; `null` follows "
        "the default.\n\n"
        "**Lowering it below what the mentor holds deletes nothing**: their "
        "session types stay as they are, and only new ones are refused until they "
        "are under the limit again. `404` for a user who is not a mentor, and for a "
        "caller without the grant. `422` for a limit outside 1 to "
        f"{SESSION_TYPE_LIMIT_CEILING}, or a body without `limit`."
    ),
    responses=ADMIN_RESPONSES
    | {
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`limit` is missing, not an integer, or outside the range."
        }
    },
)
async def set_session_type_limit(limit: SessionTypeLimitSetDep) -> SessionTypeLimitRead:
    value, is_default = limit
    return SessionTypeLimitRead(limit=value, is_default=is_default)


@router.get(
    "/sessions/{session_id}/missed-reasons",
    response_model=Page[AdminMissedReasonRead],
    summary="Why a session's parties missed it",
    description=(
        "Every reason a party sent for missing this session, oldest first, "
        "with who sent it and on which side (`mentor` or `mentee`). An empty "
        "page when nobody has; `404` for a session that does not exist. One "
        "page, since a session has at most two.\n\n"
        "Every live admin grant may look. The parties never read these back."
    ),
    responses=ADMIN_RESPONSES,
)
async def session_missed_reasons(rows: SessionMissedReasonsDep) -> Page[AdminMissedReasonRead]:
    return Page(data=[AdminMissedReasonRead(**row) for row in rows])
