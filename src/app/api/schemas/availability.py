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
from app.domain.availability import (
    CALENDAR_FAILURE_REASONS,
    UnknownTimezoneError,
    normalise_timezone,
)
from app.domain.enums import AvailabilityExceptionType

#: 0 = Sunday, matching `availability_rules.day_of_week` and the legacy
#: `dayOfWeekIn`. Named here so the API's documentation and the column's CHECK
#: cannot drift apart silently.
DayOfWeek = Annotated[int, Field(ge=0, le=6, description="0 = Sunday, 6 = Saturday")]


def validated_zone(value: str) -> str:
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
        return validated_zone(value)


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
        return None if value is None else validated_zone(value)


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


#: Built from `CALENDAR_FAILURE_REASONS` rather than retyping it, so the
#: published contract cannot describe a set the writers no longer produce. A test
#: pins the two together for the case this cannot catch: a reason added and never
#: described.
_LAST_ERROR_DESCRIPTION = (
    "Why it stopped working. Null while it works.\n\n"
    "**One of a fixed set of our own sentences, never a provider message**: "
    + ", ".join(f"`{reason}`" for reason in CALENDAR_FAILURE_REASONS)
    + ". Bounded to 500 characters.\n\n"
    "Safe to render as it stands, but copy written per value reads better than "
    "the string relayed — and fall back to a generic line for a value you do "
    "not recognise, so one added later degrades rather than shows nothing."
)


class CalendarConnectionRead(BaseModel):
    """A mentor's calendar grant, as they see it.

    **No token, and no field that could carry one.** The credential is stored
    encrypted and read only at the moment it is used; a read model that could
    return it would make every future endpoint one mistake away from doing so.

    **`account_email` names the connected account**, which it could not until
    ADR 0012 was amended on 2026-10-06 to ask for `openid email` beside
    `calendar.freebusy`. Before that this docstring explained why the field was
    absent; the reasoning was sound and the conclusion was overtaken, because
    the two added scopes are non-sensitive and so change no verification
    requirement, while a mentor who authorised the wrong Google account had
    nothing on the page telling them so (#180).

    **A broken connection is shown rather than hidden.** It used to read as
    *never connected*, which left a mentor with nothing to act on and is the gap
    ADR 0004 calls out. `status` says which it is; a client should not have to
    infer it from `last_error` being set.

    `last_error` is **our own sentence, not the provider's** — one of
    `CALENDAR_FAILURE_REASONS`, which is where they are written down and the only
    place they are. A Google body never reaches it: the one failure that carries
    one is transient, and is logged rather than stored, because writing it would
    turn a rate limit into a re-consent.

    This claimed "Google's words rather than ours" until a client asked whether
    the field was safe to render. It is — nothing interpolates an exception, a
    URL, a client id or an address into it, and the write bounds it to 500
    characters. But not for the reason given, so the argument is restated: a
    reason is returned rather than withheld because "your calendar is
    disconnected" with nothing to act on is a support ticket. A client is better
    off writing copy per value than relaying the string.
    """

    connected_at: dt.datetime
    status: str = Field(
        description=(
            "`active` while it works, `error` once it stopped. A connection you "
            "disconnected yourself is not returned at all — this endpoint "
            "answers `null` for that, because you already know."
        ),
    )
    last_synced_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When the health check last confirmed this connection works. Null "
            "until it has run — it is a scheduled sweep, not something a page "
            "view triggers."
        ),
    )
    last_error: str | None = Field(
        default=None,
        description=_LAST_ERROR_DESCRIPTION,
    )
    account_email: str | None = Field(
        default=None,
        description=(
            "The Google account this grant belongs to, for showing *which* "
            "account is connected.\n\n"
            "**Null means not known, never none.** A grant made before the "
            "consent asked for it carries no address and cannot be backfilled "
            "— the value lives in a token that mentor no longer holds, so it "
            "returns only if they reconnect. It is also null when the lookup "
            "failed at consent time, which deliberately does not refuse the "
            "connection.\n\n"
            "So degrade to `Connected` on null rather than treating it as an "
            "error or as an account without an address."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> CalendarConnectionRead:
        return cls(
            connected_at=row["connected_at"],  # type: ignore[arg-type]
            status=str(row["status"]),
            last_synced_at=row.get("last_synced_at"),  # type: ignore[arg-type]
            last_error=str(row["last_error"]) if row.get("last_error") else None,
            account_email=(
                str(row["external_account_email"]) if row.get("external_account_email") else None
            ),
        )
