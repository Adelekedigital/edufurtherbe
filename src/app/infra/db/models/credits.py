"""The credit ledger: what a user holds, and every movement that got them there.

Two tables, and the split is D8's argument rather than a normalisation habit:

    credit_lots           a grant, its origin, and what is left of it
    credit_transactions   why a balance moved, appended and never edited

**A lot is not a counter.** The package's own D8 chose a ledger over a balance
column because a counter "leaves no record" — it cannot answer *"I was charged
for a session that never ran"*, which is the first of the four reasons the
ledger exists. A lot carries its own expiry because the four things that grant
credits do not agree on one: the starter never dies, everything else dies at the
reset, and a refund inherits the semantics of the lot it replaces.

**Deletion is `RESTRICT`, where the canonical DDL says `CASCADE`.** ADR 0013:
restrict where the child is evidence. A ledger whose rows vanish with the
account cannot answer the question it was built for, and "never `ON DELETE
CASCADE` in a soft-delete system" is a standing rule here. Both tables are named
in `RETAINED_ON_USER_DELETE`, so the transitive-closure test goes red on the
first future migration that tries to route a cascade to them.

**`credit_transactions` carries no `updated_at` and no trigger.** It is
append-only: a row states what happened at a moment, and a fact that can be
edited is not a log. `session_events` and `mentor_status_events` already settled
this shape, and `APPEND_ONLY` in `tests/unit/test_models.py` is what holds it.
The canonical DDL gives the column while calling the table append-only a few
lines above.

**`unit_cost_cents` and `currency` are deferred.** D7 argues for shipping them at
zero so the payments work stays additive. Settled decision #21 governs: a column
is cheap to add later in a way an enum member is not, and nothing writes them.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import CreditReason, CreditSource
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum

#: The two reasons that hand a credit back. Written once because the partial
#: index below and the sweep that depends on it must mean the same thing by
#: construction — non-negotiable #8 applied before the second copy exists.
#:
#: The migration spells the same pair out as a literal, because no migration
#: imports from ``app``; `test_every_converted_enum_has_a_check_naming_its_values`
#: is what keeps the two honest.
REFUND_REASONS = (
    CreditReason.SESSION_CANCELLED_REFUND,
    CreditReason.SESSION_NO_SHOW_REFUND,
    CreditReason.REQUEST_UNFULFILLED,
)

#: Reasons that describe something that happened *to a session*. Rendered from
#: the enum for the same reason the refund set is: a member added without being
#: classified here would silently become one that may carry no session.
SESSION_BEARING = (
    CreditReason.SESSION_BOOKED,
    *REFUND_REASONS,
)

_SESSION_BEARING = ", ".join(f"'{reason.value}'" for reason in SESSION_BEARING)

_REFUND_PREDICATE = "reason IN ({})".format(
    ", ".join(f"'{reason.value}'" for reason in REFUND_REASONS)
)


class CreditLot(Base, TimestampMixin):
    """One grant: where it came from, how much it was, and what is left.

    **Quantities are two columns, not one.** ``quantity_granted`` is history and
    never moves; ``quantity_remaining`` is the balance and only falls. Keeping
    the original is what lets a reconciliation ask whether the sum of the ledger
    agrees with the sum of the lots — the ETL's Definition of Done — and a single
    mutable column cannot answer it.

    ``expires_at`` is nullable, and null means *never*. Today only the starter
    holds a null; once payments land every purchased lot joins it, which is why
    the read filter is written as ``expires_at IS NULL OR expires_at > now()``
    rather than against a sentinel date.
    """

    __tablename__ = "credit_lots"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    source: Mapped[CreditSource] = mapped_column(str_enum(CreditSource), nullable=False)

    #: ``smallint``: the largest grant this product can produce is three, and the
    #: opening balance's ceiling is the legacy maximum of five.
    quantity_granted: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    quantity_remaining: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    #: Null means never. Exclusive — the 1st of the next month at midnight UTC,
    #: so an August lot survives all of 31 August and dies as September opens.
    #: The card promises ``Next reset date: 1st, Sep 2026`` and an inclusive
    #: end-of-August would kill it a day before the date on screen.
    expires_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        # Zero is not a grant, it is a row saying nothing happened.
        CheckConstraint("quantity_granted > 0", name="quantity_granted_positive"),
        # The guard behind every spend. The advisory lock in PR 6 serialises one
        # user's bookings; this is what catches the case where it does not.
        CheckConstraint("quantity_remaining >= 0", name="quantity_remaining_not_negative"),
        # A refund crediting the wrong lot would otherwise inflate it silently.
        CheckConstraint("quantity_remaining <= quantity_granted", name="remaining_lte_granted"),
        CheckConstraint(check_is_known("source", CreditSource), name="source_is_known"),
        # **Redundant against the primary key, and deliberately so.** `id` alone
        # already identifies a lot; this exists solely so
        # `credit_transactions` can point a *composite* key at
        # `(user_id, id)` and make "a movement against somebody else's lot"
        # unrepresentable. Non-negotiable #10's second sentence — an invariant a
        # composite key would have carried is re-declared as `UNIQUE`.
        #
        # `mentor_conferencing_options` carries the identical pair for the
        # identical reason; this is that pattern, not a new one.
        UniqueConstraint("user_id", "id", name="uq_credit_lots_user_id_id"),
        # **The predicate is the point.** Onboarding completes once, and its
        # producer is a transition somebody will eventually make idempotent by
        # retrying it — so "one starter ever" is structural rather than checked
        # in application code. Keyed on `user_id` *with* the predicate: on
        # `user_id` alone it would stop a user ever receiving a second monthly
        # grant.
        Index(
            "uq_credit_lots_one_starter_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(f"source = '{CreditSource.PROFILE_COMPLETED.value}'"),
        ),
        # **One opening balance per user, ever.** The migration's loader runs
        # more than once by design — a rehearsal, a partial load retried, then
        # the real cutover — and without this every migrated user would collect
        # a second lot and their balance would silently double. The ETL's
        # Definition of Done requires a second run to produce identical rows;
        # this is what makes that true rather than hoped for.
        #
        # A partial unique index rather than the `legacy_bubble_id` column every
        # other ETL-loaded table carries: that column makes a *row* re-runnable,
        # while the invariant here is one lot per user — which is also exactly
        # what PR 10's reconciliation checks. Same shape as the starter index
        # two lines up.
        Index(
            "uq_credit_lots_one_opening_balance_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(f"source = '{CreditSource.OPENING_BALANCE.value}'"),
        ),
        # **One monthly grant per user per period.** A scheduled job runs
        # twice — a retry, a manual trigger beside the cron — and the second
        # run would hand every unlocked mentee another three credits, which
        # nothing downstream would notice because the balance is a `SUM` that
        # would simply be right about the wrong number.
        #
        # Keyed on the expiry because every monthly lot granted in one month
        # shares one, so the pair *is* the period — and a job that fires late
        # on the 3rd still collides with the one that fired on the 1st.
        # `date_trunc` on a `timestamptz` is only STABLE, so PostgreSQL would
        # refuse it here; this column carries the same fact.
        Index(
            "uq_credit_lots_one_monthly_grant_per_period",
            "user_id",
            "expires_at",
            unique=True,
            postgresql_where=text(f"source = '{CreditSource.MONTHLY_FREE.value}'"),
        ),
        # The balance read: this user's live lots, oldest expiry first, which is
        # also the order a spend consumes them in.
        Index("ix_credit_lots_user_expiry", "user_id", "expires_at"),
    )


class CreditTransaction(Base):
    """One movement of one balance, appended and never overwritten.

    **No ``TimestampMixin``, and no ``updated_at``** — see the module docstring.
    ``created_at`` alone, because a row states what happened at a moment.

    ``session_id`` is nullable: a grant and an expiry belong to no session. A
    debit and its refund both carry one, and *that is the subtlety this table's
    index has to survive* — see below.
    """

    __tablename__ = "credit_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    #: Denormalised from the lot deliberately. Every read of a ledger is "this
    #: user's movements", and joining through `credit_lots` to answer it makes
    #: the balance query depend on a table the transaction already implies.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: **No single-column foreign key here.** The composite in `__table_args__`
    #: does the work: a single-column key is satisfied by *any* lot row,
    #: including another user's, and this column is where that would land.
    credit_lot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: Signed. Negative spends, positive credits, and zero is refused — a row
    #: claiming a balance changed when it did not is worse than no row.
    delta: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    reason: Mapped[CreditReason] = mapped_column(str_enum(CreditReason), nullable=False)

    #: Null on a grant and on an expiry sweep, which belong to no session.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="RESTRICT")
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # **The guard no single-column key can give.** `user_id` is denormalised
        # from the lot so that every "this user's ledger" read is one table; the
        # cost of that copy is that the two can disagree, and nothing above the
        # database would notice — a lot-scoped balance and a
        # transaction-scoped balance would each look correctly filtered and
        # return different numbers.
        #
        # `session_types.conferencing_option_id` met this exact problem and
        # states the rule: "a single-column key is satisfied by *any* option
        # row, including another mentor's". Substitute one noun.
        #
        # Both columns are `NOT NULL`, so `MATCH SIMPLE` never skips the check —
        # unlike the precedent, where a nullable option means "use my default".
        ForeignKeyConstraint(
            ["user_id", "credit_lot_id"],
            ["credit_lots.user_id", "credit_lots.id"],
            name="fk_credit_transactions_lot_belongs_to_user",
            ondelete="RESTRICT",
        ),
        # The same guard for the *session* half. A movement that names a session
        # must name one the spender was the mentee of — otherwise a debit can be
        # recorded against somebody else's booking, and a per-user ledger and a
        # per-session ledger disagree while both look correctly filtered.
        #
        # `MATCH SIMPLE` is what keeps grants and expiries legal: they carry a
        # null `session_id`, and a composite key with any null column is not
        # checked. Raised as a MEDIUM by `security-checker` against PR 1 and
        # closed here, where the writer that could exploit it lands.
        ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["sessions.mentee_id", "sessions.id"],
            name="fk_credit_transactions_session_belongs_to_user",
            ondelete="RESTRICT",
        ),
        CheckConstraint("delta <> 0", name="delta_is_not_zero"),
        CheckConstraint(check_is_known("reason", CreditReason), name="reason_is_known"),
        # **A NULL `session_id` bypasses the refund index entirely**, because
        # PostgreSQL treats nulls as distinct — so two refunds with no session
        # both insert and the sweep's guarantee evaporates. The same hole lets a
        # `session_booked` debit name nothing, which makes D8's first question
        # unanswerable.
        #
        # An equivalence, so it cuts both ways: a grant or an expiry sweep
        # belongs to no session and may not name one.
        CheckConstraint(
            f"(reason IN ({_SESSION_BEARING})) = (session_id IS NOT NULL)",
            name="session_matches_reason",
        ),
        # **The one subtlety that would pass every rejecting test.** The sweep
        # commits per batch and is scheduled, so a cancel followed by a sweep —
        # or a sweep that runs twice — would otherwise pay twice for one session.
        #
        # The predicate is what makes it correct, not the column list. A booking
        # debit and the refund that reverses it **share a `session_id`**, so an
        # index keyed on `session_id` alone would reject the very refund this
        # exists to permit — and every *rejecting* test would still pass. Only
        # refund rows enter the index; the debit is not in it.
        Index(
            "uq_credit_transactions_one_refund_per_session",
            "session_id",
            unique=True,
            postgresql_where=text(_REFUND_PREDICATE),
        ),
        # This user's ledger, newest first — the order a statement renders in.
        Index("ix_credit_transactions_user_created", "user_id", text("created_at DESC")),
    )


class AdminCreditGrant(Base):
    """Who authorised an ``admin_grant`` lot, and why if they said.

    **A table beside the ledger rather than columns on it**, the way
    `review_reports` sits beside `reviews`. `credit_transactions` records
    *movements*; who authorised one is metadata about a single kind of movement,
    and two nullable columns used by one source in six would be dead weight on
    every other row.

    It also keeps the ledger's shape honest. A ``granted_by`` on
    `credit_transactions` could only be constrained by the *lot's* source, which
    lives in another table — so the column would be nullable with nothing
    stopping a booking debit carrying one.

    **Append-only, like the ledger.** No ``updated_at`` and no trigger: a row
    states who authorised what at a moment, and an authorisation that can be
    edited is not one.
    """

    __tablename__ = "admin_credit_grants"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    #: **No single-column foreign key.** The composite below does the work.
    credit_lot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: The recipient, denormalised from the lot so the composite key has both
    #: halves — the same shape, and the same reason, as `credit_transactions`.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: The admin who did it. **``NOT NULL`` although the note is optional**: a
    #: record that cannot say who is not a record, which is the rule
    #: `review_reports.resolved_by` already states for a moderation decision.
    granted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: Why, in the admin's own words, or nothing. Optional deliberately: an admin
    #: may grant credits nobody asked for — a goodwill gesture after an outage
    #: reaches people who never complained — and requiring prose for that is how
    #: a field fills with "goodwill".
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # **The guard no single-column key gives.** On `credit_lots.id` alone,
        # this row could name user A while pointing at user B's lot — and a
        # per-user audit and a per-lot audit would each look correctly filtered
        # and disagree. Sixth time this shape has been the right answer here.
        ForeignKeyConstraint(
            ["user_id", "credit_lot_id"],
            ["credit_lots.user_id", "credit_lots.id"],
            name="fk_admin_credit_grants_lot_belongs_to_user",
            ondelete="RESTRICT",
        ),
        # One authorisation per lot. Two would mean two admins each believing
        # they granted it, and the audit could not say which.
        UniqueConstraint("credit_lot_id", name="uq_admin_credit_grants_credit_lot_id"),
        # Every grant one admin made, newest first — the question an audit asks.
        Index("ix_admin_credit_grants_granted_by", "granted_by", text("created_at DESC")),
    )
