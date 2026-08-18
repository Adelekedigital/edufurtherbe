"""What a mentor offers — as a stranger sees it, and as its owner does.

**Two models, and the smaller one is the public contract.** `SessionTypeRead`
answers `GET /users/{user_id}/session-types` and is an allowlist that must not
grow; `OwnSessionTypeRead` answers `GET /me/session-types` and adds exactly three
fields. They are declared separately rather than by inheritance — see the second
class for why.


**A session type is the bookable thing** — this mentor's own product, with a
duration and a venue. It is *not* a `service_offering`, which is the closed
six-row taxonomy at `/api/v1/catalog/service-offerings` describing what **kind**
of help exists and is what matching joins on. Two different concepts, and the
word "service" belongs to the other one.

The fields here are an allowlist rather than a projection of the table.
`created_by` is internal attribution and null on every migrated row;
`category` and `application_stage` are free text with no constraint, no
vocabulary and no value in any row today, so publishing them would commit this
contract to a shape nobody has designed. Adding a field later is additive;
removing one is breaking.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import ConferencingProvider


class SessionTypeRead(BaseModel):
    """One offering, and everything needed to ask for its slots."""

    id: str = Field(
        description=(
            "Pass as `session_type_id` to "
            "`/api/v1/users/{user_id}/availability/slots`. A slot's length and "
            "notice come from this offering, so slots cannot be asked for "
            "without naming one."
        )
    )
    name: str
    description: str | None = None
    duration_minutes: int = Field(
        description="How long a session of this type runs, and the step between slots."
    )
    min_notice_minutes: int = Field(
        description=(
            "How far ahead a booking must be made. Slots starting sooner than "
            "this are not offered, which is why the next few hours can look "
            "empty on a mentor who is free."
        )
    )
    meeting_venue: ConferencingProvider = Field(
        description=(
            "Where the session happens, **resolved** from the mentor's "
            "conferencing options: this offering's own, else the mentor's "
            "default, else `google_meet`. Required, and never null — the "
            "platform fallback is the last step precisely so this field cannot "
            "be absent. The meeting **link** is generated per session and never "
            "appears here: a static room means back-to-back sessions share it "
            "and an early joiner walks into the previous one."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> SessionTypeRead:
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            description=str(row["description"]) if row["description"] else None,
            duration_minutes=int(str(row["duration_minutes"])),
            min_notice_minutes=int(str(row["min_notice_minutes"])),
            meeting_venue=ConferencingProvider(str(row["meeting_venue"])),
        )


class OwnSessionTypeRead(BaseModel):
    """One offering, as the mentor who owns it sees it.

    **A separate model rather than a subclass of `SessionTypeRead`, deliberately.**
    Inheritance would express "the public shape plus three", which is true today
    and is exactly the coupling worth refusing: the public model is an allowlist
    whose whole job is not to grow, and a field added to it would arrive here
    silently — or, worse, tempt somebody to add `is_active` to the parent because
    that is where the other five live. Two declarations is the cost of keeping the
    two contracts independently reviewable, and a test asserts each key set.

    The three fields below are the entire difference.
    """

    id: str = Field(
        description=(
            "Stable across a rename, and the id `sessions.session_type_id` "
            "records. Also what the public `/users/{user_id}/session-types` "
            "returns for the same offering — the two endpoints describe the same "
            "rows, not two populations."
        )
    )
    name: str
    description: str | None = None
    duration_minutes: int = Field(
        description="How long a session of this type runs, and the step between slots."
    )
    min_notice_minutes: int = Field(
        description="How far ahead a booking must be made against this offering."
    )
    meeting_venue: ConferencingProvider = Field(
        description=(
            "Where this offering is held. Every offering carries its own — there "
            "is no cascade from the mentor to resolve. The meeting **link** is "
            "generated per session and never appears here."
        ),
    )
    is_active: bool = Field(
        description=(
            "Whether this offering is currently on offer. **`false` covers off, "
            "closed and hidden alike** — a switched-off offering is invisible in "
            "search *and* unbookable by direct link.\n\n"
            "This is the field the public endpoint has no way to express: it "
            "returns only active offerings, so a paused one is simply absent "
            "there. Here it is present and flagged, which is what a management "
            "list needs in order to offer switching it back on."
        )
    )
    # Neither description states how many rows currently hold a value. Three
    # places in this repository already say these columns have "no value anywhere
    # in the data", and the migration that gives `category` a foreign key makes
    # all three false — a published contract is the worst place to add a fourth
    # copy of a fact with an expiry date on it. What is described here is the
    # field's meaning and its current constraint, both of which a client acts on.
    category: str | None = Field(
        default=None,
        description=(
            "The mentor's own grouping for this offering. Free text today, with "
            "no vocabulary behind it. Withheld from the public contract for that "
            "reason; returned here because it is the caller's own value."
        ),
    )
    application_stage: str | None = Field(
        default=None,
        description=(
            "The stage of an application this offering is aimed at. Free text "
            "today, like `category`, and public only to its owner."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> OwnSessionTypeRead:
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            description=str(row["description"]) if row["description"] else None,
            duration_minutes=int(str(row["duration_minutes"])),
            min_notice_minutes=int(str(row["min_notice_minutes"])),
            meeting_venue=ConferencingProvider(str(row["meeting_venue"])),
            is_active=bool(row["is_active"]),
            category=str(row["category"]) if row["category"] else None,
            application_stage=(str(row["application_stage"]) if row["application_stage"] else None),
        )
