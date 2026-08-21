"""Platform infrastructure: tables that serve every feature and belong to none.

Two tables now, and the module exists rather than the rows joining
``sessions.py`` because the subject is different (#33). ``idempotency_keys``
does not describe a booking; it describes *a request that must not happen
twice*, and the next two tables the canonical package puts beside it —
``outbox_events`` and ``feature_flags`` — are the same kind of thing.
``outbox_events`` has since arrived, which is the argument holding: it carries
notifications today and the package designed it for analytics dispatch, and
neither is a session.

Named for `08_features_platform.sql`, which is where the package puts all three.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin

#: How long a stored answer stays replayable, as SQL.
#:
#: **Written the way PostgreSQL renders it rather than the way a human would**
#: — ``'24:00:00'::interval``, not ``interval '24 hours'``. The two are the same
#: value and the database normalises to the first, and
#: ``test_every_declared_server_default_matches_the_database`` compares the
#: declared text against the stored one. That guard exists because
#: ``alembic check`` does not diff server defaults at all, so a model drifting
#: from its column is otherwise silent until somebody autogenerates from it.
#:
#: Named once and imported by the reclaim path in ``infra/db/idempotency.py``,
#: which sets ``expires_at`` explicitly: two windows that disagree would be a
#: defect nobody could see, because the row would simply live for whichever
#: wrote last.
TTL_SQL = "'24:00:00'::interval"


class IdempotencyKey(TimestampMixin, Base):
    """One client-supplied key, the request it stood for, and the answer given.

    **A flaky connection retrying a booking must not create two sessions.** The
    client sends ``Idempotency-Key``; the first request stores its response here
    and every replay is served from it. See ADR 0024.

    **Surrogate ``id``, where the package makes ``key`` the primary key.** ADR
    0015 admits no exception and non-negotiable #10 prescribes the resolution —
    the invariant the natural key carried is re-declared as ``UNIQUE`` below.

    **That unique is ``(user_id, key)``, not ``(key)``, and this is a second and
    separate divergence from the package.** It follows from the lookup being
    scoped to the caller, which it must be: a key row holds a stored *response
    body*, so an unscoped read would serve one user another user's booking. Once
    the read is scoped, a global unique key space is a defect rather than a
    stricter rule — user B sending a key user A already holds finds nothing on
    the scoped select and then collides on the insert, which is a failure with
    no correct answer to give. Scoping per caller is also what Stripe does, and
    the reason is the same.

    ``NULLS NOT DISTINCT`` because ``user_id`` is nullable — the package leaves
    room for an idempotent endpoint that takes no token, and under the default
    ``NULLS DISTINCT`` two anonymous requests sharing a key would both be
    inserted, which is the one case the table exists to prevent.

    **``expires_at`` is enforced by the queries, not by a job.** A row past it is
    invisible to the lookup and is *reclaimed in place* by the next reservation
    for the same key, so the table self-heals without a sweep. The retention job
    the runbook lists still earns its place — it keeps the table small — but
    nothing depends on it having run.
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    key: Mapped[str] = mapped_column(Text, nullable=False)
    #: Nullable, per the package. Every writer today is authenticated, so this
    #: is null on no row that exists — but making it `NOT NULL` would be a
    #: divergence bought with nothing, and `NULLS NOT DISTINCT` already makes
    #: the anonymous case behave.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )

    #: `"POST /api/v1/sessions"`. Informational: the fingerprint below already
    #: covers the endpoint, so a key reused across two endpoints is caught as a
    #: mismatch rather than by comparing this column. It is kept because a
    #: replay served from the wrong endpoint is the failure this table would be
    #: blamed for, and a human reading the row needs to see which one it was.
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    #: A hash of the request, not the request. Booking bodies are small, but a
    #: stored body is a second copy of user input with its own retention
    #: question, and the only thing this column is ever asked is *is it the
    #: same*.
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)

    #: Null until the work finishes. The pair `(response_body, completed_at)` is
    #: what separates a reservation from an answer: a row with `locked_at` and
    #: no `completed_at` is a request in flight, and a replay of it is refused
    #: rather than answered with a null body.
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    status_code: Mapped[int | None] = mapped_column()

    locked_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text(f"now() + {TTL_SQL}"),
    )

    __table_args__ = (
        # The invariant `key text PRIMARY KEY` carried in the package, scoped to
        # the caller for the reason in the class docstring. `postgresql_nulls_not_
        # distinct` is PostgreSQL 15+; this project is on 17.
        Index(
            "uq_idempotency_keys_user_key",
            "user_id",
            "key",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        # The retention job's read, and nothing else uses it. Kept from the
        # package: a table nothing sweeps grows without bound, and the job that
        # sweeps it needs this to not scan.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )


class OutboxEvent(TimestampMixin, Base):
    """Something that happened, and somebody who is owed a message about it.

    **Written in the same transaction as the fact it describes.** That is the
    whole reason the table exists: a session and the intent to tell somebody
    about it commit together or neither does, so there is no window where a
    booking exists and nobody will ever hear, and none where somebody hears
    about a booking that rolled back.

    **The package designed this for analytics dispatch and it fits unchanged.**
    ``destination`` defaults to ``'posthog'`` there and answers *where does this
    go*; a channel is the same question, so notifications write ``'email'`` and
    the analytics consumer the package anticipates can write ``'posthog'`` into
    the same table without either knowing about the other.

    **No divergence from the canonical DDL** — the first table this milestone
    where the package already gave the surrogate ``id`` ADR 0015 requires, so
    there was nothing to reconcile.

    **No foreign key on ``entity_id``**, and the package has none either. The
    column points at whichever table ``entity_type`` names, so a key could only
    reference one of them — and an outbox that cannot describe a second entity
    type is an outbox for one feature.
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    #: The `Notification` member, as text. Not a `CHECK`ed vocabulary: this
    #: column also carries analytics event names the product has not written
    #: yet, and constraining it to today's notifications would make the
    #: package's own use of the table illegal.
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: The recipient's id and the template's variables. **An id rather than an
    #: address**, so the drain resolves it at send time and somebody who changes
    #: their email between the enqueue and the send is written to at the new
    #: one.
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )

    destination: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'posthog'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))

    #: Incremented on every attempt whatever the outcome, which is what makes
    #: the retry bound a bound rather than a suggestion.
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    sent_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    error_detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="status_is_known",
        ),
        # **Partial on pending**, where the package's index is not. The drain
        # asks for exactly this and nothing else reads the table by date, and
        # this table keeps every row it has ever written — so a full index would
        # be almost entirely rows the query discards.
        Index(
            "ix_outbox_events_pending",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # **At most one reminder per session per kind per recipient, whatever
        # QStash does.**
        # A retried callback would otherwise queue an identical second email;
        # nothing else in this table stops it, because two rows differing only
        # by id are exactly what it is normally for. ADR 0015 prescribes this
        # shape — a natural key re-declared as `UNIQUE` — so the index is the
        # sanctioned form rather than a workaround.
        #
        # Partial on the reminder shape, so it constrains nothing else: every
        # other message writes no `kind` and is skipped entirely.
        Index(
            "uq_outbox_events_reminder",
            "entity_id",
            "event_type",
            text("(payload ->> 'kind')"),
            # **The recipient, and it was missing until a reminder went to two
            # people.** The outbox writes one row per person so a send failing
            # for one is retried for that one; without this the second row
            # conflicts with the first and is dropped in silence — one party
            # reminded, the other not, nothing saying so.
            text("(payload ->> 'recipient_id')"),
            unique=True,
            postgresql_where=text("payload ? 'kind'"),
        ),
    )
