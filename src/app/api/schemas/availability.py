"""Request and response shapes for a mentor's availability.

**Times go out as UTC instants plus the mentor's IANA zone, never as a
server-rendered local string.** That string is what ``12hr-localStartTime-TXT``
was, and it disagrees with the stored time by five hours on half the legacy
rows. The browser knows the viewer's zone; the server does not, and a formatted
time in a response is a decision the server is not equipped to make.

The **rules** are the exception, and deliberately: a weekly rule is a wall clock
plus a zone, not an instant, so it goes out exactly as declared. Converting it
would require naming a date, and the whole point of a recurring rule is that it
has not named one.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.schemas.common import Normalised
from app.domain.availability import UnknownTimezoneError, normalise_timezone
from app.domain.enums import AvailabilityExceptionType

#: 0 = Sunday, matching `availability_rules.day_of_week` and the legacy
#: `dayOfWeekIn`. Named here so the API's documentation and the column's CHECK
#: cannot drift apart silently.
DayOfWeek = Annotated[int, Field(ge=0, le=6, description="0 = Sunday, 6 = Saturday")]


def _validated_zone(value: str) -> str:
    """Validated **at the boundary**, per settled decision #36.

    The column is `text` with no CHECK — `pg_timezone_names` is not immutable, so
    PostgreSQL will not accept it in one — which makes this the only thing
    between a request and a value that raises inside the projection later.
    `normalise_timezone` is shared with the ETL rather than reimplemented: two
    spellings of "is this a real zone" is exactly what #8 is about.
    """
    try:
        return normalise_timezone(value)
    except UnknownTimezoneError as exc:
        raise ValueError(str(exc)) from exc


class _ZoneMixin(Normalised):
    timezone: str = Field(
        description="IANA name, e.g. `Africa/Lagos`. Not an offset — an offset "
        "goes stale twice a year, which is the bug this whole surface exists to "
        "avoid.",
        examples=["Africa/Lagos"],
    )

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        return _validated_zone(value)


class AvailabilityRuleWrite(_ZoneMixin):
    """One recurring weekly window."""

    day_of_week: DayOfWeek
    start_time: dt.time
    end_time: dt.time
    is_active: bool = True

    @model_validator(mode="after")
    def _window_moves_forward(self) -> Self:
        """Refused here as well as by the column, and for a different reason.

        `CHECK (end_time > start_time)` is what guarantees it; this is what makes
        the refusal a 422 naming the field instead of a 500 carrying a constraint
        name. A window crossing midnight is two rows on two weekdays — the schema
        cannot split it, because which side of midnight the mentor meant is not
        something a validator can know.
        """
        if self.end_time <= self.start_time:
            raise ValueError(
                "end_time must be later than start_time on the clock; a window "
                "crossing midnight is two rules, one per weekday"
            )
        return self


class AvailabilityRulePatch(BaseModel):
    """Only the fields sent are changed. Absent is not null."""

    day_of_week: DayOfWeek | None = None
    start_time: dt.time | None = None
    end_time: dt.time | None = None
    timezone: str | None = None
    is_active: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone_if_sent(cls, value: str | None) -> str | None:
        """Absent is not null: a patch that never mentions the zone leaves it
        alone, and one that does gets exactly the check a create gets."""
        return None if value is None else _validated_zone(value)


class AvailabilityRuleRead(BaseModel):
    """A rule as declared — wall clock plus zone, not an instant."""

    id: str
    day_of_week: DayOfWeek
    start_time: dt.time
    end_time: dt.time
    timezone: str
    is_active: bool

    @classmethod
    def from_row(cls, row: dict[str, object]) -> AvailabilityRuleRead:
        return cls(
            id=str(row["id"]),
            day_of_week=int(str(row["day_of_week"])),
            start_time=row["start_time"],  # type: ignore[arg-type]
            end_time=row["end_time"],  # type: ignore[arg-type]
            timezone=str(row["timezone"]),
            is_active=bool(row["is_active"]),
        )


class AvailabilityExceptionWrite(_ZoneMixin):
    """A date range on which the weekly rules do not apply as written."""

    type: AvailabilityExceptionType
    start_date: dt.date
    #: Exclusive, matching the `daterange [)` the column stores. One blocked day
    #: is `start_date = d`, `end_date = d + 1`.
    end_date: dt.date
    start_time: dt.time | None = None
    end_time: dt.time | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.end_date <= self.start_date:
            raise ValueError("end_date is exclusive and must be after start_date")
        # Both or neither: a start with no end has no defensible reading —
        # open-ended, or midnight? — and refusing it is what keeps the
        # projection from having to guess.
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be given together, or neither")
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        return self


class AvailabilityExceptionRead(BaseModel):
    id: str
    type: AvailabilityExceptionType
    start_date: dt.date
    end_date: dt.date
    start_time: dt.time | None
    end_time: dt.time | None
    timezone: str
    reason: str | None

    @classmethod
    def from_row(cls, row: dict[str, object]) -> AvailabilityExceptionRead:
        # The store hands over `lower()` and `upper()` as plain dates. A
        # `daterange` is NOT NULL but its *bounds* are independently nullable —
        # `[2026-01-01,)` is legal — and nothing this project writes produces
        # one, so an absent bound is corrupt data rather than a case to render.
        if row["start_date"] is None or row["end_date"] is None:
            raise ValueError(f"availability exception {row['id']} has an unbounded date range")
        return cls(
            id=str(row["id"]),
            type=AvailabilityExceptionType(str(row["type"])),
            start_date=row["start_date"],  # type: ignore[arg-type]
            end_date=row["end_date"],  # type: ignore[arg-type]
            start_time=row["start_time"],  # type: ignore[arg-type]
            end_time=row["end_time"],  # type: ignore[arg-type]
            timezone=str(row["timezone"]),
            reason=row["reason"],  # type: ignore[arg-type]
        )


class AvailabilityWindow(BaseModel):
    """One projected span of real time.

    **UTC, with an offset, always.** A naive timestamp is the bug that makes a
    client render 13:00 for a mentor who said 08:00 — there is no way for the
    receiver to interpret one correctly. `timezone` is the *mentor's* zone,
    carried so a client can also show "their time" beside the viewer's; it is
    display context and never arithmetic.
    """

    start: dt.datetime
    end: dt.datetime
    timezone: str
