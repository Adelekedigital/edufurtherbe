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
columns encode, is still Proposed with two behaviours it names as untested.
Settled decision #21 governs: it ships with the phase that first needs it.
"""

import datetime
import uuid

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Time, Uuid, text
from sqlalchemy.dialects.postgresql import DATERANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AvailabilityExceptionType
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


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
        pg_enum(AvailabilityExceptionType), nullable=False
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
