"""The admin Emails pages: list, edit and values (PRD 28, decision 321)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import PRIVATE_NO_STORE
from app.api.deps.admin_emails import (
    AdminEmailDep,
    AdminEmailListDep,
    AdminEmailSaveDep,
    AdminEmailValuesDep,
)
from app.api.routes.admin import ADMIN_RESPONSES
from app.api.schemas.admin_emails import EmailDetailRead, EmailListItem, EmailValueRead
from app.api.schemas.common import Page

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": "`/problems/email-not-found`: no such email, or no such variant."
    }
}


@router.get(
    "/emails",
    response_model=Page[EmailListItem],
    summary="List every email admins can see",
    description=(
        "Every email the platform sends, in list order: a shared template's emails "
        "together. `status` is `live` once the email's template is the shared one "
        "that shows the backend's lines; otherwise its lines are read only. "
        "`last_edited` is the newest saved change, or null.\n\n"
        "Every live admin grant may read. Never cached (`private, no-store`)."
    ),
    responses=ADMIN_RESPONSES,
)
async def admin_emails(page: AdminEmailListDep, response: Response) -> Page[EmailListItem]:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return page


@router.get(
    "/emails/{key}",
    response_model=EmailDetailRead,
    summary="One email, for the editor",
    description=(
        "The list's fields, how many other emails share its template, the values its "
        "lines may use, and each variant with its label, sample values, sample "
        "recipient and its subject, heading and button. An email with no lines of "
        "the backend's has its lines empty with version 0.\n\n"
        "Never cached (`private, no-store`)."
    ),
    responses={**ADMIN_RESPONSES, **NOT_FOUND},
)
async def admin_email(email: AdminEmailDep, response: Response) -> EmailDetailRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return email


@router.put(
    "/emails/{key}/variants/{variant}",
    response_model=EmailDetailRead,
    summary="Save one variant's subject, heading and button",
    description=(
        "Saves all three lines of the variant together, each with the `version` the "
        "editor read. A line equal to its default goes back to the default; a line "
        "equal to what is saved changes nothing, unless it is in `keep_edit` and its "
        "default has moved. Every changed line is written to the edit history. A "
        "save that changes nothing writes nothing. Returns the email as the GET does."
    ),
    responses={
        **ADMIN_RESPONSES,
        **NOT_FOUND,
        status.HTTP_409_CONFLICT: {
            "description": "`/problems/email-line-conflict`: another admin saved first. "
            "Carries `updated_by_name` and `updated_at`; nothing was written."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`/problems/email-line-invalid`: `errors[]` has `pointer`, "
            "`message`, `line`, `code` (`empty`, `unknown_value`, `value_not_sent`, "
            "`too_long`) and, where it applies, `value` and `suggestion`."
        },
        status.HTTP_423_LOCKED: {
            "description": "`/problems/email-not-set-up`: the email's template is not "
            "the shared one yet, so its lines are read only."
        },
    },
)
async def save_admin_email(email: AdminEmailSaveDep, response: Response) -> EmailDetailRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return email


@router.get(
    "/email-values",
    response_model=Page[EmailValueRead],
    summary="The values a line may use",
    description=(
        "Every value an email's lines may name as `{name}`: what it means, an "
        "example, what an empty one says, and the emails that carry it.\n\n"
        "Never cached (`private, no-store`)."
    ),
    responses=ADMIN_RESPONSES,
)
async def admin_email_values(
    values: AdminEmailValuesDep, response: Response
) -> Page[EmailValueRead]:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return values
