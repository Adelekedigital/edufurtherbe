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

from app.api.deps import (
    BIO_DRAFT_CALLBACK_PATH,
    MESSAGE_EMAIL_CALLBACK_PATH,
    BioDraftCallbackDep,
    DailyPresenceDep,
    MessageEmailCallbackDep,
    ReminderCallbackDep,
)
from app.api.schemas.jobs import (
    BioDraftRead,
    MessageEmailRead,
    PresenceRecordedRead,
    ReminderQueuedRead,
)

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
    response_model=ReminderQueuedRead,
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
async def fire_reminder(queued: ReminderCallbackDep, request: Request) -> ReminderQueuedRead:
    """Always `200` once the signature checks out.

    **A callback that reports failure is a callback QStash retries**, and there
    is nothing here worth retrying: the request was either still pending, in
    which case the message is queued and the outbox owns delivery from there, or
    it was not, in which case retrying will not change that. Reporting anything
    else would turn a settled request into a scheduled job that never stops.
    """
    del request
    return ReminderQueuedRead(queued=queued)


DAILY_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": (
            "`X-Webhook-Signature` is absent, not base64, made with a secret we "
            "do not hold, or does not match `{X-Webhook-Timestamp}.{body}`; or "
            "no secret is configured, in which case every delivery is refused."
        )
    },
}


@router.post(
    "/daily",
    response_model=PresenceRecordedRead,
    summary="Record who Daily saw in a room",
    description=(
        "**Called by Daily, never by a person** (#382). Authenticated by "
        "`X-Webhook-Signature`, an HMAC-SHA256 over `{X-Webhook-Timestamp}.{body}`, "
        "verified against the raw body before anything is read.\n\n"
        "A `participant.joined` marks that party present in that session, "
        "matched by the session's own room **and** the `user_id` minted into the "
        "party's meeting token, together; a sighting for anybody else matches "
        "nothing. Only the earliest sighting is kept, so a repeated or "
        "out-of-order delivery changes nothing, and one after arrivals stop is "
        "kept but never counted.\n\n"
        "Both `participant.joined` and `participant.left` also keep who is in "
        "the room **now** (#394), one stay per Daily `session_id`: a join opens "
        "it, a leave closes it at `joined_at + duration`, and a leave that "
        "arrives first is not reopened by its join. A leave is also its join's "
        "sighting, so a lost join still counts.\n\n"
        "**Every verified delivery gets `200`**, including Daily's creation "
        "check and events this ignores, because refusing counts as a failed "
        "delivery and three of those switch the webhook off. `recorded` says "
        "whether a party's row matched."
    ),
    responses=DAILY_RESPONSES,
)
async def record_daily_presence(recorded: DailyPresenceDep) -> PresenceRecordedRead:
    return PresenceRecordedRead(recorded=recorded)


@router.post(
    BIO_DRAFT_CALLBACK_PATH.removeprefix(router.prefix),
    response_model=BioDraftRead,
    summary="Draft a new mentee's bio",
    description=(
        "**Called by QStash, never by a person** (decision 269), queued once "
        "when a mentee first completes onboarding. Authenticated by "
        "`Upstash-Signature`, verified against this endpoint and a hash of "
        'this exact body; the body is `{"user_id"}`.\n\n'
        "It re-reads the person first and drafts only while their bio is empty, "
        "no draft was ever recorded, and they are still a mentee with a goal. "
        "The draft is written from their onboarding answers alone, and saved "
        "only if the bio is still empty at that moment, so text the person "
        "typed meanwhile wins. Safe to deliver twice.\n\n"
        "**Every handled outcome is `200`**, `drafted: false` included, so "
        "QStash stops: retrying would not change any of them."
    ),
    responses=CALLBACK_RESPONSES,
)
async def draft_mentee_bio(drafted: BioDraftCallbackDep) -> BioDraftRead:
    return BioDraftRead(drafted=drafted)


@router.post(
    MESSAGE_EMAIL_CALLBACK_PATH.removeprefix(router.prefix),
    response_model=MessageEmailRead,
    summary="Email a person their unread messages",
    description=(
        "**Called by QStash, never by a person** (decision 303), queued by each "
        "new send outside a session's window for 15 minutes later. "
        "Authenticated by `Upstash-Signature`, "
        "verified against this endpoint and a hash of this exact body; the body "
        'is `{"recipient_id"}`.\n\n'
        "It re-reads everything: the recipient's messages still unread, not "
        "deleted, not yet emailed and sent within the last day. Nothing left, "
        "the recipient switched these emails off, or their account is closed: "
        "nothing is queued. If a message email went to them in "
        "the last hour, it queues itself again for when the hour ends. Otherwise "
        "one or two messages queue the few email and three or more the many "
        "email, and those messages are marked emailed in the same transaction. "
        "**Safe to deliver twice**: a second delivery finds them marked.\n\n"
        "**Every handled outcome is `200`**, `queued: false` included."
    ),
    responses=CALLBACK_RESPONSES,
)
async def send_unread_message_email(queued: MessageEmailCallbackDep) -> MessageEmailRead:
    return MessageEmailRead(queued=queued)
