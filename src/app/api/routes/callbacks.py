"""Endpoints a machine calls, authenticated by signature rather than by token.

**The first surface in this codebase with no user behind it**, and that is the
whole reason it is its own module. Every other route resolves a caller and
scopes to them; here the caller is QStash, the control is a signature, and there
is no row-level scope because there is no row-level actor. Putting one of these
among the authenticated routes is how somebody later assumes a token is present.

**Nothing here trusts its own body.** The payload names a session and a kind,
and both are re-read before anything is written — a callback for a request that
has since been answered does nothing at all. That is what makes scheduling
ahead safe without ever cancelling anything (ADR 0025).

**Verification is two checks, and the second is the load-bearing one.** A valid
signature proves QStash issued *a* token; the body hash proves it issued one for
*this payload*. Without the hash, anybody who observed one callback could replay
its signature against a body of their choosing — and this endpoint queues
messages, so that would be a send-anything primitive.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import ReminderCallbackDep

router = APIRouter(prefix="/api/v1/callbacks", tags=["callbacks"])

CALLBACK_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": (
            "The `Upstash-Signature` header is absent, malformed, signed with a "
            "key we do not hold, minted for a different endpoint, or does not "
            "match this body.\n\n"
            "**Not `403`.** A caller who cannot prove who they are has not been "
            "refused permission — there is nobody to refuse."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The body was not a reminder callback."},
}


@router.post(
    "/reminders",
    summary="Fire a scheduled response reminder",
    description=(
        "**Called by QStash, never by a person.** Authenticated by the "
        "`Upstash-Signature` header rather than a bearer token, and verified "
        "against both the endpoint it was minted for and a hash of this exact "
        "body.\n\n"
        "**It re-reads the session before doing anything**, so a callback for a "
        "request that has since been accepted, declined, withdrawn or expired "
        "queues nothing and reports so. That is deliberate: the alternative "
        "makes four transitions responsible for cancelling scheduled work, and "
        "the bug is the reminder that fires for a request answered through the "
        "one path somebody forgot.\n\n"
        "**Safe to call twice**, which QStash does on retry — a unique index "
        "makes the second enqueue a no-op rather than a second identical email."
        "\n\n"
        "`queued: false` is a success, not a failure. It means the request no "
        "longer needs a nudge."
    ),
    responses=CALLBACK_RESPONSES,
)
async def fire_reminder(queued: ReminderCallbackDep, request: Request) -> dict[str, bool]:
    """Always `200` once the signature checks out.

    **A callback that reports failure is a callback QStash retries**, and there
    is nothing here worth retrying: the request was either still pending, in
    which case the message is queued and the outbox owns delivery from there, or
    it was not, in which case retrying will not change that. Reporting anything
    else would turn a settled request into a scheduled job that never stops.
    """
    del request
    return {"queued": queued}
