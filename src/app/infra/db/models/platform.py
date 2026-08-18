"""Platform infrastructure: tables that serve every feature and belong to none.

One table so far, and the module exists rather than the row joining
``sessions.py`` because the subject is different (#33). ``idempotency_keys``
does not describe a booking; it describes *a request that must not happen
twice*, and the next two tables the canonical package puts beside it —
``outbox_events`` and ``feature_flags`` — are the same kind of thing. Filing it
under sessions would make the first non-booking user of it look misplaced.

Named for `08_features_platform.sql`, which is where the package puts all three.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text, Uuid, text
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
