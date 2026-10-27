"""Which state a session may move to, who may move it, and why.

Pure, and in ``domain`` because every line here is a product rule rather than a
storage detail. ``SessionStatus``'s own docstring already says the transitions
are enforced "at the endpoint that writes the status" and that nothing in the
schema holds them; this module is that rule, written once so the four endpoints
are four names for one table rather than four implementations.

**The ten-minute cancellation cutoff is here rather than in a trigger**, and the
build plan floated a trigger because the rule is time-relative and a ``CHECK``
predicate must be ``IMMUTABLE`` while ``now()`` is ``STABLE``. That reasoning
correctly rules out a ``CHECK`` and does not by itself argue for a trigger. The
project already settled where a rule like this lives: *the database refuses what
is impossible; the application refuses what is disallowed* — which is why the
24-hour booking notice is a Pydantic bound over a wide sanity ``CHECK`` rather
than a constraint carrying the product's current mind. A cutoff the product will
want to change is the same shape, and a trigger would make every change a
migration.

**The reason codes are restricted per actor so that what they count stays
true**: a mentee cannot file their own cancellation as ``MENTOR_UNAVAILABLE``.
They do not decide refunds — decision 229 does, in ``domain/refunds.py``, from
the actor's side and the notice given.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.domain.enums import ApplicationStage, SessionReasonCode, SessionRole, SessionStatus

__all__ = [
    "CANCELLATION_CUTOFF",
    "EXPIRY_GRACE",
    "EXPIRY_KIND",
    "MAX_SESSION_TYPE_OFFERINGS",
    "TRANSITIONS",
    "Transition",
    "first_stage",
    "named_stages",
    "records_unavailability",
    "request_deadline",
    "respond_by",
    "response_window",
    "response_window_hours",
    "stage_set_problem",
    "too_late_to_cancel",
]

#: How close to the start a confirmed session stops being cancellable.
#:
#: **Not a grace period and not the join window.** Those are the two things it
#: sits between and is neither: the join window (from the configured lead, at
#: most ten, to ``+15`` minutes) decides *attendance*, and the response window
#: decides when an unanswered *request* dies. This decides when calling off a session stops being a
#: cancellation and starts being not turning up — which is the honest record,
#: because by then the other party is already on their way to the call.
CANCELLATION_CUTOFF = dt.timedelta(minutes=10)

#: The statuses a session is still *live* in: awaiting a decision, or agreed.
#: The one definition: the SQL predicate `LIVE_STATUSES` and the extract's
#: overlap check are built from it. **The exclusion constraint and the partial
#: indexes are not** — they live in migrations, so changing this set needs a new
#: migration, and `test_models` fails until one restates it. Ordered, so the SQL
#: text is stable.
LIVE_SESSION_STATUSES = (SessionStatus.PENDING_MENTOR_APPROVAL, SessionStatus.CONFIRMED)


#: **What expires a request at its deadline** (owner, 2026-10-13): booking
#: schedules this callback kind for each request it makes, so an unanswered one
#: is expired within seconds of its deadline instead of at the next hourly
#: sweep, which stays as the backstop. Prefixed `x` beside the reminders' `t`,
#: `s`, `c`, `r` and `h`, so no reminder rule can be mistaken for it.
EXPIRY_KIND = "x0"

#: How long after the deadline the expiry fires. A delivery a moment early would
#: find the request not yet lapsed and do nothing, so it lands just after.
EXPIRY_GRACE = dt.timedelta(seconds=30)


def response_window(settings: Settings) -> dt.timedelta:
    """How long before the session an unanswered request dies: the one reader of
    `response_window_hours` (decision 278).

    **Measured backwards from the session, not forwards from the request**, and
    the guarantee is to the *mentee*: they will know before the session, early
    enough for the answer to still be useful. Forwards from `created_at` would
    let a request made three weeks out sit unanswered until the morning of.

    **The notice floor bounds it.** The mentor's time to answer is the notice
    given less this window, so the setting's ceiling is derived from the 24-hour
    floor in `core.config` and checked at boot. Reminders count back from the
    deadline (`domain.notifications.reminders_for`).
    """
    return dt.timedelta(hours=response_window_hours(settings))


def response_window_hours(settings: Settings) -> int:
    """The window in whole hours, as the mentor profile publishes it: the one
    place the setting is read, so the published number and the deadline agree."""
    return settings.response_window_hours


def respond_by(
    starts_at: dt.datetime, *, window: dt.timedelta, requires_confirmation: bool
) -> dt.datetime | None:
    """The deadline for answering a request, or ``None`` when none is awaited.

    **Null is the domain rule, not a default.** An auto-confirming offering has
    no response window at all: nothing is waiting on the mentor, so there is
    nothing to elapse. Writing a deadline anyway would put confirmed sessions in
    the sweep's path and make the partial index meaningless.

    **Stored once, at booking**, and never recomputed: a change to the setting
    applies to new requests only, and a pending one keeps the deadline it was
    told. Can return an instant already in the past only for a notice shorter
    than ``window``, which the 24-hour floor and the window's ceiling make
    unreachable through the API; the sweep expires such a request anyway.
    """
    return starts_at - window if requires_confirmation else None


def request_deadline(respond_by: dt.datetime | None, starts_at: dt.datetime) -> dt.datetime:
    """When an unanswered request lapses: its ``respond_by``, or its start when
    it has none, as every migrated request does (decision 121).

    `pending_requests._past_deadline` is the same rule in SQL, which the sweep,
    accept, decline and the reminder decide on; a test pins the two together.
    """
    return respond_by if respond_by is not None else starts_at


@dataclass(frozen=True, slots=True)
class Transition:
    """One named action: who may take it, from where, to where, and why.

    ``reasons`` is keyed by the actor's role because the permitted set differs
    by side. A blank set means the action takes no reason at all — accepting a
    request explains itself, and offering a field nobody can fill is a contract
    that invites a client to fill it.
    """

    to: SessionStatus
    #: The roles that may take this action. Two entries means either party.
    by: frozenset[SessionRole]
    #: The states it may be taken from. A single entry today for every action,
    #: and a set because the shape has to survive `no_show` and `completed`
    #: arriving with the attendance sweep.
    allowed_from: frozenset[SessionStatus]
    reasons: dict[SessionRole, frozenset[SessionReasonCode]]
    #: Whether :data:`CANCELLATION_CUTOFF` applies. Only cancelling is close
    #: enough to a start for it to bite — a *pending* request being withdrawn
    #: was never agreed, so there is nobody about to join a call.
    honours_cutoff: bool = False


#: Reasons a mentor may give. `MENTEE_NO_LONGER_NEEDED` is absent because it is
#: not the mentor's to assert, and `MENTOR_NO_SHOW` / `MENTEE_NO_SHOW` because
#: they belong to the attendance sweep — nobody is a no-show until the session
#: has run.
_MENTOR_REASONS = frozenset(
    {
        SessionReasonCode.MENTOR_UNAVAILABLE,
        SessionReasonCode.SCHEDULING_CONFLICT,
        SessionReasonCode.TECHNICAL_ISSUE,
        # "None of these", the last choice in the list (owner, 2026-10-10).
        SessionReasonCode.OTHER,
    }
)

#: And the mentee's. `MENTOR_UNAVAILABLE` is absent because it says the mentor
#: could not make it, which is not the mentee's to report.
_MENTEE_REASONS = frozenset(
    {
        SessionReasonCode.MENTEE_NO_LONGER_NEEDED,
        SessionReasonCode.SCHEDULING_CONFLICT,
        SessionReasonCode.TECHNICAL_ISSUE,
        SessionReasonCode.OTHER,
    }
)

#: `EXPIRED_NO_RESPONSE`, `RESCHEDULED`, `ADMIN_ACTION` and `ACCOUNT_DELETED`
#: appear in no set here, deliberately. The first is written by the expiry
#: producer, which is a system actor and not a person; the second is written
#: by the move (decision 300) on the session it ends; the third belongs to an admin surface;
#: the fourth is written by `DELETE /me` for each session it ends. Each is a
#: value a party could otherwise assert about themselves.


def records_unavailability(action: str, role: SessionRole, *, release_slot: bool) -> bool:
    """Whether this transition should record the mentor as unavailable.

    **Only a mentor, only on `cancel`, and only when they say they are not
    free.** A mentee cancelling says nothing about the mentor's availability —
    the mentor never became busy, so holding their hour would be the
    `FREES_THE_HOUR` defect in a new place, and mentee-driven exactly as that
    one was. Whatever a mentee sends here is ignored rather than refused: the
    field is not theirs to answer, and a `422` would teach a client to send a
    value it should never have had an opinion about.

    **Withdrawing and declining never reach this**, because neither ends an
    agreement — nothing was ever on the mentor's calendar to protect.
    """
    return action == "cancel" and role is SessionRole.MENTOR and not release_slot


TRANSITIONS: dict[str, Transition] = {
    "accept": Transition(
        to=SessionStatus.CONFIRMED,
        by=frozenset({SessionRole.MENTOR}),
        allowed_from=frozenset({SessionStatus.PENDING_MENTOR_APPROVAL}),
        # Agreeing needs no explanation, and a reason field on it would be one
        # more thing for a client to send and for policy to have to ignore.
        reasons={},
    ),
    "decline": Transition(
        to=SessionStatus.DECLINED,
        by=frozenset({SessionRole.MENTOR}),
        allowed_from=frozenset({SessionStatus.PENDING_MENTOR_APPROVAL}),
        reasons={SessionRole.MENTOR: _MENTOR_REASONS},
    ),
    "withdraw": Transition(
        to=SessionStatus.WITHDRAWN,
        by=frozenset({SessionRole.MENTEE}),
        allowed_from=frozenset({SessionStatus.PENDING_MENTOR_APPROVAL}),
        reasons={SessionRole.MENTEE: _MENTEE_REASONS},
    ),
    "cancel": Transition(
        to=SessionStatus.CANCELLED,
        # **The only action either party may take**, and the difference from
        # `withdraw` is the state rather than the person: a confirmed session
        # was agreed, so calling it off breaks an agreement whoever does it.
        by=frozenset({SessionRole.MENTOR, SessionRole.MENTEE}),
        allowed_from=frozenset({SessionStatus.CONFIRMED}),
        reasons={SessionRole.MENTOR: _MENTOR_REASONS, SessionRole.MENTEE: _MENTEE_REASONS},
        honours_cutoff=True,
    ),
}


def reason_required(action: str, *, offers_another_time: bool) -> bool:
    """Whether ending a session this way must carry a ``reason_code``.

    **Every action that takes a reason requires one** (owner, 2026-10-10), so the
    reasons can be analysed; ``other`` covers "none of these". Accepting takes
    none. **Offering another time instead is exempt** (#339, owner the same day):
    the offer is the mentor's answer, and the app asks no reason on that path.
    """
    return bool(TRANSITIONS[action].reasons) and not offers_another_time


def role_of(*, mentor_id: object, actor_id: object) -> SessionRole:
    """Which side of a session the actor is on. The caller has already scoped
    the session to them, so not the mentor means the mentee."""
    return SessionRole.MENTOR if mentor_id == actor_id else SessionRole.MENTEE


def ending_for(status: SessionStatus, role: SessionRole) -> str:
    """The action this side takes to end a session in ``status``, read off
    :data:`TRANSITIONS`: allowed from it, theirs to take, and not `accept`,
    the one action that starts a session rather than ending it. Account
    deletion asks this, so it ends a session only the way that side may.
    Raises :class:`LookupError` for a status nothing ends."""
    for action, rule in TRANSITIONS.items():
        if (
            status in rule.allowed_from
            and role in rule.by
            and rule.to is not SessionStatus.CONFIRMED
        ):
            return action
    raise LookupError(f"no action ends a {status} session for the {role}")


def too_late_to_cancel(starts_at: dt.datetime, now: dt.datetime) -> bool:
    """Whether the start is inside :data:`CANCELLATION_CUTOFF`.

    **True for a session that has already begun**, which is not incidental: the
    comparison is one-sided on purpose. A session an hour into the past is past
    cancelling, and it becomes `completed` or `no_show` through the attendance
    sweep rather than through either party changing their mind about it.

    ``now`` is passed in rather than read here, following every other
    clock-dependent function in this project: one that reads its own clock
    cannot be tested at a boundary without moving the machine's time.

    **Exactly the cutoff is already too late** (`<=`). The join window is
    half-open from `start - lead`, and the lead may be the whole cutoff, so
    that instant belongs to joining: with `<` it was both, and one party could
    be marked present while the other released the session (#391 review).
    """
    return starts_at - now <= CANCELLATION_CUTOFF


#: How many service offerings one session type may cover (#205). The owner's
#: number (2026-09-28); a product rule, so it lives here and both the boundary
#: and the store read it.
MAX_SESSION_TYPE_OFFERINGS = 3


def named_stages(fields: Mapping[str, object]) -> list[ApplicationStage] | None:
    """The stage set a write names, or ``None`` when it names none (#215).

    Asked by the boundary and by the store. **`application_stages: null` is
    refused at the boundary** — `[]` means any stage and leaving it out means
    unchanged — so a `None` reaching here is a field nobody sent, which is how
    a full `model_dump()` spells it.
    """
    stages = fields.get("application_stages")
    if stages is not None:
        return [ApplicationStage(value) for value in stages]  # type: ignore[attr-defined]
    return None


def first_stage(stages: Sequence[ApplicationStage]) -> ApplicationStage | None:
    """The first of a stage set: what `session_types.application_stage` holds
    while that deprecated column is kept (decision 293), so a rollback still
    reads a stage."""
    return stages[0] if stages else None


def stage_set_problem(stages: Sequence[ApplicationStage]) -> tuple[str, str] | None:
    """What is wrong with this stage set, as ``(pointer, message)``, or ``None``
    (#215): each stage once. Asked at the boundary and again before the store
    writes, because the pair index would otherwise answer with a 500. There is no
    label to judge since the design's six (decision 288).
    """
    if len(set(stages)) != len(stages):
        return ("/application_stages", "a stage appears twice")
    return None
