"""Availability: when a mentor can be booked, and the dates that override it.

Two tables, and one rule that governs both:

    wall-clock time + IANA zone  ->  for RULES (recurring, DST-aware)
    timestamptz                  ->  for INSTANTS (a specific session)

Storing a pre-formatted local string for a recurring rule is what breaks across
DST, twice a year, silently. That is not a hypothetical here: legacy
``CalendarSettings`` carried four such columns — ``12hr-`` and ``24hr-``, each
with a start and an end
— and the export shows them disagreeing with the stored time by five hours on
half the rows. All four are dropped (package D18).

**Slots are never stored.** A bookable slot is rules for that weekday, minus
exceptions overlapping the date, minus confirmed sessions, sliced into the
session type's duration and filtered by notice — computed at query time and
converted to the viewer's zone for display. Storing them would recreate exactly
the drift that made the legacy front-search table untrustworthy (D19), and at
192 rules there is nothing to gain.

``calendar_connections`` is **not** here. Its DDL sits in the package's
availability file and the M1 migration promised it for M3, but nothing in M3
reads or writes it — ADR 0004 puts the free/busy read at slot render and at
confirmation, both M4 — and ADR 0012, which decides the OAuth arrangement its
columns encode, is still Proposed. Settled decision #21 governs: it ships with
the phase that first needs it.

The two behaviours ADR 0012 named as untested were **measured on 2026-08-16**,
and the answer moved the calendar into EduFurther's own Google account rather
than each mentor's. A mentor's row therefore records a ``calendar.freebusy``
grant and nothing else, and **booking no longer depends on this table at all** —
the grant buys conflict detection, not the ability to hold a session. Read ADR
0012 before building it: three columns in the package DDL are wrong for that
decision, including two PostgreSQL enum types that settled decision #100 forbids.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Time, Uuid, text
from sqlalchemy.dialects.postgresql import DATERANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AvailabilityExceptionType
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class AvailabilityRule(TimestampMixin, Base):
    """One recurring weekly window in which a mentor can be booked.

    **Several rows per mentor per weekday, deliberately.** Split availability —
    morning and afternoon with a lunch gap — is a shape the legacy
    one-row-per-day structure could not express, and it is not hypothetical: the
    dev export holds 6 weekdays carrying more than one row, up to 3 on one day.
    There is therefore **no unique constraint on** ``(mentor_user_id,
    day_of_week)``, and adding one would reject real migrated rows.

    Overlapping windows on one weekday **are** forbidden, by the partial
    ``EXCLUDE`` below. An earlier version of this docstring said the opposite —
    that there was no such constraint and the ETL merely reported overlaps —
    which was true when it was written and stopped being true sixty lines later
    in the same file. The ETL now *merges* overlapping legacy windows into their
    union before insert, because an unmerged pair does not land badly: it aborts
    the load.

    ``timezone`` is per row rather than per mentor, which is what the legacy
    ``timeZone`` column was. A mentor whose rows disagree is possible and is
    reported by the transform rather than silently collapsed.
    """

    __tablename__ = "availability_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # References `mentor_profiles(user_id)`, not `users(id)` — the same value,
    # a different guarantee, and the choice `mentor_status_events` already
    # records. Availability belonging to a user with no mentor profile is not a
    # state this schema should be able to represent.
    #
    # CASCADE by ADR 0013's method rather than by copying the package: a rule is
    # meaningless without its mentor and records no auditable fact. The negative
    # half — that no cascade path reaches a table which must be retained — is
    # asserted in `test_schema_parity`.
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentor_profiles.user_id", ondelete="CASCADE"), nullable=False
    )

    #: 0 = Sunday, matching the legacy `dayOfWeekIn`. Verified against the export
    #: rather than assumed: `dayOfWeekIn` and `daysOfWeek-O/S` agree on all 24
    #: dev rows and 0 is Sunday in every one. An off-by-one moves every migrated
    #: mentor's availability by a day, and nothing downstream would notice.
    day_of_week: Mapped[int] = mapped_column(nullable=False)

    #: Wall clock, not an instant. `Time` without a timezone is the point — a
    #: `time with time zone` would carry a fixed UTC offset, which is the DST bug
    #: in a different costume.
    start_time: Mapped[datetime.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[datetime.time] = mapped_column(Time, nullable=False)

    #: IANA name, e.g. `Africa/Lagos`. Validated in the domain against the real
    #: tz database, never by a CHECK: `pg_timezone_names` is not immutable, so
    #: PostgreSQL will not accept it in one. Same position as `users.timezone`.
    timezone: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="day_of_week_valid"),
        # `>` and not `>=`. A zero-length window is the case a reader assumes
        # "ordered" already covers, and it does not unless the operator is
        # strict.
        #
        # This constraint also forbids a window crossing midnight (22:00-02:00),
        # which would have to be stored as two rows on two weekdays. The dev
        # export contains none — checked, not assumed — and the ETL reports any
        # it finds rather than splitting them silently.
        CheckConstraint("end_time > start_time", name="availability_window_ordered"),
        # Two windows overlapping on one weekday say the same thing twice, so
        # the pair is a mistake rather than a state. Declared here as well as in
        # the migration because the model is the source of truth for shape —
        # though `alembic check` is blind to exclusion constraints, so the six
        # tests are what actually hold it.
        #
        # `'[)'` is load-bearing: it lets 09:00-12:00 and 12:00-14:00 touch
        # without colliding, which is how the legacy rows are shaped.
        ExcludeConstraint(
            ("mentor_user_id", "="),
            ("day_of_week", "="),
            (text("timerange(start_time, end_time, '[)')"), "&&"),
            name="availability_rules_no_overlap",
            using="gist",
            where=text("is_active AND deleted_at IS NULL"),
        ),
        # The only read: this mentor's windows for a weekday. Partial, because
        # inactive and soft-deleted rules are never part of an answer — and
        # `alembic check` cannot compare the predicate, so a test asserts it.
        Index(
            "ix_availability_rules_mentor",
            "mentor_user_id",
            "day_of_week",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
    )


class AvailabilityException(TimestampMixin, Base):
    """A date range on which the recurring rules do not apply as written.

    ``BLOCK`` subtracts from the rules — a holiday, an exam period. ``OVERRIDE``
    adds a window no rule describes. Null ``start_time`` and ``end_time`` mean
    the whole day, which is what every migrated row is.

    **``legacy_bubble_id`` is not the bare Bubble id here, and that is forced by
    the data.** One ``CalendarExtra`` row holds a *list* of discontiguous dates —
    the dev export has one carrying Jan 13, Jan 19, Jan 21, Jun 21 and Jul 5 —
    and those cannot become one ``daterange`` without blocking the six months
    between them. So one legacy row fans out to one exception per date, and the
    anchor is ``{bubble_id}:{iso_date}``. It stays unique and stays re-runnable,
    because the date is derived from the source rather than generated. The
    package's field mapping reads ``block-Date(s) (list) -> date_range``, which
    implies 1:1; it is 1:N, and this is where that is recorded.
    """

    __tablename__ = "availability_exceptions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentor_profiles.user_id", ondelete="CASCADE"), nullable=False
    )

    type: Mapped[AvailabilityExceptionType] = mapped_column(
        str_enum(AvailabilityExceptionType), nullable=False
    )
    #: Half-open `[lo, hi)`. A single blocked day is `[d, d+1)`, which is what
    #: the fan-out writes — inclusive-upper would make "one day" ambiguous
    #: between `[d, d]` and `[d, d+1)` and the two answer overlap differently.
    date_range: Mapped[Range[datetime.date]] = mapped_column(DATERANGE, nullable=False)

    start_time: Mapped[datetime.time | None] = mapped_column(Time)
    end_time: Mapped[datetime.time | None] = mapped_column(Time)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    #: Legacy `meetingDailySessions`. A daily cap is closer to booking policy
    #: (B2) than to an exception, and parking it here means it applies only on
    #: dates that happen to carry an exception row. It is kept because the
    #: package's DDL has it and moving it is B2's decision to make with data;
    #: the dev export populates it on 0 of 2 rows, so nothing validates it yet.
    max_sessions_per_day: Mapped[int | None] = mapped_column()

    #: Soft delete, where the package's DDL has none. Rules carry `deleted_at`
    #: and exceptions do not, which is an inconsistency in the package against
    #: its own "soft delete everywhere" rule rather than a decision it took.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        # A start with no end has no defensible reading — open-ended, or
        # midnight? Refusing the pair keeps the projection from having to guess.
        CheckConstraint("(start_time IS NULL) = (end_time IS NULL)", name="exception_times_paired"),
        CheckConstraint(
            "start_time IS NULL OR end_time > start_time", name="exception_window_ordered"
        ),
        # Settled decision #100. `OVERRIDE` ships with no legacy source, and this
        # is the constraint that will let it be *removed* if the product never
        # writes one — the thing a PostgreSQL enum could never do.
        CheckConstraint(
            check_is_known("type", AvailabilityExceptionType),
            name="type_is_known",
        ),
        # GiST over `(uuid, daterange)`, which needs `btree_gist` — uuid has no
        # GiST operator class without it. Verified present on the local
        # container, on the CI image and on Supabase (already installed, v1.7,
        # in `public`) before this was relied on.
        Index(
            "ix_availability_exceptions_range",
            "mentor_user_id",
            "date_range",
            postgresql_using="gist",
        ),
    )


class SessionTypeSchedulingWindow(TimestampMixin, Base):
    """One recurring weekly window in which **a single offering** can be booked.

    **Windows replace a mentor's general availability; they do not intersect
    it.** An offering with windows is bookable in *those* and nowhere else; an
    offering with none uses `availability_rules`, as everything did before.

    Intersecting was the obvious reading and the mock's own example shows why it
    is wrong: Wednesday 5-8pm and Thursday 9am-1pm read as deliberate evening and
    morning slots, and intersected with normal working hours they yield **zero**
    slots — an empty calendar with nothing to explain it.

    **`availability_exceptions` still subtract, always.** Windows replace
    *availability*, not *unavailability*: a mentor who blocked a date blocked it
    for every offering. That is derived rather than stated in the decision, and
    is written here so "replace" is not read as replacing everything.

    **A mentor with windows and no general availability is bookable**, which is
    newly reachable and is not a misconfiguration.

    Same shape as `AvailabilityRule` deliberately — `bookable()` takes a list of
    weekly windows and does not care which table they came from, which is what
    makes "replace" a swap of the source rather than a second code path through
    the slot maths.
    """

    __tablename__ = "session_type_scheduling_windows"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    #: The offering, which is also what carries ownership: a window is reached
    #: through the session type, and `session_type_of()` is what scopes the
    #: writes. `CASCADE` because a window has no meaning without its offering and
    #: records no fact about anybody (ADR 0013).
    #: Named by hand. The `fk_%(table)s_%(column)s_%(referred_table)s`
    #: convention renders 64 characters here — one over the limit, where
    #: SQLAlchemy silently truncates and appends a hash. This is the **fourth**
    #: table in this milestone to trip it, which is what long table names cost.
    session_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "session_types.id",
            ondelete="CASCADE",
            name="fk_session_type_scheduling_windows_session_type_id",
        ),
        nullable=False,
    )

    day_of_week: Mapped[int] = mapped_column(nullable=False)
    start_time: Mapped[datetime.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[datetime.time] = mapped_column(Time, nullable=False)
    #: The mentor's IANA zone, stored per row exactly as `availability_rules`
    #: does. A window is declared in the zone the mentor was thinking in, and
    #: resolving it later against a profile column would silently move every
    #: window the day they travel.
    timezone: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="day_of_week_valid"),
        CheckConstraint("end_time > start_time", name="scheduling_window_ordered"),
        # **Scoped to the offering, not the mentor** — that is the one meaningful
        # difference from `availability_rules_no_overlap`. Two offerings may
        # legitimately cover the same hours; two windows on *one* offering
        # covering the same hours is a duplicate the slot grid would count twice.
        #
        # `'[)'` is load-bearing, letting 09:00-12:00 and 12:00-14:00 touch
        # without colliding, and the partial `WHERE` keeps a switched-off or
        # deleted window from blocking the slot it used to occupy.
        ExcludeConstraint(
            ("session_type_id", "="),
            ("day_of_week", "="),
            (text("timerange(start_time, end_time, '[)')"), "&&"),
            name="session_type_scheduling_windows_no_overlap",
            using="gist",
            where=text("is_active AND deleted_at IS NULL"),
        ),
        Index(
            "ix_session_type_scheduling_windows_offering",
            "session_type_id",
            "day_of_week",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
    )


class CalendarConnection(TimestampMixin, Base):
    """A mentor's grant to read when they are busy. One per provider, per mentor.

    **Not the calendar the platform writes to.** ADR 0012 splits the two grants:
    ``calendar.app.created`` is given once by EduFurther's own account and
    creates every session's event — that is configuration, and it needs no table
    at all. ``calendar.freebusy`` is given by each mentor, reads only *when* they
    are busy, and is what this row holds. An earlier docstring claimed the event
    writer needed this table; it never did, and conflating the two is how that
    happened.

    **Deferred since M1 and arriving with its consumer** (#21). Two earlier
    migrations recorded its absence on purpose; what needed it is free/busy
    conflict detection.

    **The token is encrypted, and the column name says so.** A reader who sees
    ``refresh_token`` and writes a plaintext one has made a mistake nothing can
    catch — a string is a string. This is the only credential the service stores
    on somebody else's behalf, and the only reason it is in the database rather
    than in configuration is that it arrives per mentor at a moment nobody
    chooses.

    **A disconnect revokes rather than deletes.** *That they once connected*
    stays answerable, which is what a mentor asking why their calendar stopped
    being consulted actually needs. The unique index is partial on ``active`` so
    a revoked row never blocks a reconnection — which is the ordinary case, not
    the exception.
    """

    __tablename__ = "calendar_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    #: Straight to `users`, not `mentor_profiles`. Only mentors connect today,
    #: but the grant is a fact about a Google account rather than about a
    #: mentoring profile — and a mentor who later loses their profile has not
    #: withdrawn their consent.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    #: `text` + `CHECK`, not an enum type (#100). One value today; the CHECK is
    #: what stops a typo becoming a connection nothing can read.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which Google account consented. **Null on every row today**, and that
    #: is the narrow ask rather than an omission: the value would come from an
    #: `id_token`, which Google issues only when `openid` is among the scopes,
    #: and ADR 0012 asks for `calendar.freebusy` alone. The column is carried
    #: because it is in the canonical package and because the day the ask
    #: widens it is what gets filled.
    external_account_id: Mapped[str | None] = mapped_column(Text)

    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    last_synced_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    #: Why it stopped working, in Google's words. Shown to the mentor, because
    #: "your calendar is disconnected" without a reason is a support ticket.
    last_error: Mapped[str | None] = mapped_column(Text)

    connected_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("provider IN ('google')", name="provider_is_known"),
        CheckConstraint("status IN ('active', 'revoked', 'error')", name="status_is_known"),
        # One live grant per provider per mentor, verbatim from the canonical
        # package. Partial, so reconnecting after a disconnect is not refused by
        # the row recording the disconnect.
        Index(
            "uq_calendar_connections_active",
            "user_id",
            "provider",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
