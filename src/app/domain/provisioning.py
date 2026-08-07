"""What provisioning should do about one user, decided without a network.

The CLI's real complexity is not the HTTP — it is knowing, for each of 1,200
rows, whether to create an account, link one that already exists, or leave the
row alone. That decision is pure, so it lives here and is tested without a
Supabase project or a database.

**Provisioning is eager, and that is ADR 0018's decision, not this module's.**
The one-line version: eager is the only variant you can prove works *before* the
freeze. The argument, the rejected alternatives and what recovers a half-finished
run all live in the record.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: What the identity provider holds for an address, or nothing. A callable rather
#: than a client, so this module stays free of HTTP and of ``infra``.
type Lookup = Callable[[str], UUID | None]


class Action(StrEnum):
    """What provisioning will do about one row."""

    #: Already linked. Re-running must not touch it — that is what makes the
    #: whole operation resumable after a partial failure.
    SKIP = "skip"
    #: No account exists for this address; create one.
    CREATE = "create"
    #: An account exists but the row does not reference it. This is the *normal*
    #: resume path, not an anomaly: the previous run created the account and
    #: failed before writing `auth_id`.
    LINK = "link"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A user row, as provisioning sees it."""

    user_id: UUID
    email: str
    auth_id: UUID | None


@dataclass(frozen=True, slots=True)
class Plan:
    """The decision for one candidate."""

    candidate: Candidate
    action: Action
    #: Set only for ``LINK`` — the account that already exists.
    existing_auth_id: UUID | None = None


def decide(candidate: Candidate, existing_auth_id: UUID | None) -> Plan:
    """Choose an action for one row.

    ``existing_auth_id`` is what Supabase reports for this address, or ``None``.

    The ordering matters. A row that is already linked is skipped **without
    consulting Supabase at all**, which is what keeps a re-run cheap: 1,200 rows
    already provisioned cost zero API calls the second time.
    """
    if candidate.auth_id is not None:
        return Plan(candidate=candidate, action=Action.SKIP)
    if existing_auth_id is not None:
        return Plan(candidate=candidate, action=Action.LINK, existing_auth_id=existing_auth_id)
    return Plan(candidate=candidate, action=Action.CREATE)


def plan_for(candidate: Candidate, lookup: Lookup) -> Plan:
    """Decide one candidate, consulting the provider only when the decision needs it.

    ``lookup`` answers "what account does the provider hold for this address" and
    is injected, so this stays pure and testable with no network — the same shape
    as ``domain.bubble.BubbleSource``, which the ETL already reads through.

    **It is typed on ``UUID``, not on the adapter's user object.** Depending on
    ``infra.auth.admin.AuthUser`` for the one field it reads is what kept this
    function stranded in the script; the composition root adapts.

    The early return repeats ``decide``'s first branch, and that is the point of
    having both in one file rather than one of them in a caller: an already-linked
    row is skipped **without consulting the provider at all**, so a re-run of a
    provisioned population costs zero API calls. Splitting the two across a layer
    boundary meant the condition existed twice with nothing holding it together.
    """
    if candidate.auth_id is not None:
        return decide(candidate, None)
    return decide(candidate, lookup(candidate.email))


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a run did, for the operator watching it."""

    created: int = 0
    linked: int = 0
    skipped: int = 0
    failed: tuple[str, ...] = ()

    def summary(self) -> str:
        """The counts, for stdout.

        **Every user lands in exactly one of these four**, so they sum to the
        population the run reported. A case that quietly fell through — an account
        created that the row would not take, say — would leave an operator
        reconciling "1,200 users, 1,200 provisioned" against a total that does not
        add up, with no line explaining the gap.
        """
        return "\n".join(
            [
                f"created {self.created}",
                f"linked  {self.linked}",
                f"skipped {self.skipped} (already provisioned)",
                f"failed  {len(self.failed)}",
            ]
        )

    def failures(self) -> list[str]:
        """One line per failed user, for stderr.

        Separate from ``summary`` because they go to different streams, matching
        ``load_identity.py``: a caller keeping stdout for the counts still sees
        what needs attention. By address, because the operator's next action is to
        look at each one — a count says a run went wrong and nothing about where.
        """
        return [f"FAILED {failure}" for failure in self.failed]
