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
`created_by` is internal attribution and null on every migrated row and stays
out.

**`service_offering` and `application_stage` are now published, and that
reverses the reason they were withheld.** They were free text with no
constraint, no vocabulary and no value in any row, so publishing them would have
committed this contract to a shape nobody had designed. Both have a designed
shape now — a reference to the closed six-row taxonomy, and a five-value closed
set — so the argument lapsed rather than being overruled. Adding a field is
additive; it is removing one that is breaking, which is why the bar for adding
was ever high.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import Normalised
from app.api.schemas.intake import QuestionWrite
from app.api.schemas.profile import LookupRef
from app.domain.enums import ApplicationStage, ConferencingProvider, SessionTypeIcon
from app.domain.intake import MAX_QUESTIONS
from app.domain.sessions import MAX_SESSION_TYPE_OFFERINGS


def _offerings(row: dict[str, object]) -> list[LookupRef]:
    """The service offerings a type covers, in the mentor's order (#200).

    **`LookupRef`s rather than bare slugs**, matching `MentorProfileRead.
    offerings`: a slug alone would make every client join against
    `/catalog/service-offerings` to render a chip, for a six-row table the
    response can carry inline at no cost.
    """
    return [LookupRef(**o) for o in row.get("service_offerings") or []]  # type: ignore[attr-defined]


def _taxonomy(row: dict[str, object]) -> LookupRef | None:
    """The single `service_offering` kept this release: **the first of the set**
    (#200), so the old field and the new list can never disagree."""
    offerings = _offerings(row)
    return offerings[0] if offerings else None


def _refuse_two_offering_fields(model: BaseModel) -> None:
    """`service_offering_ids` is the set and `service_offering_id` a set of one;
    a request sending both has two answers and no rule for which wins."""
    fields = model.model_fields_set
    if "service_offering_ids" in fields and "service_offering_id" in fields:
        raise ValueError("send service_offering_ids or service_offering_id, not both")
    ids = getattr(model, "service_offering_ids", None)
    if ids is not None and len(set(ids)) != len(ids):
        raise ValueError("service_offering_ids: an offering appears twice")


def _stage(row: dict[str, object]) -> ApplicationStage | None:
    value = row.get("application_stage")
    return None if value is None else ApplicationStage(str(value))


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
    service_offering: LookupRef | None = Field(
        default=None,
        description=(
            "What *kind* of help this is — one row of the closed taxonomy at "
            "`/api/v1/catalog/service-offerings`, which is the axis mentee needs "
            "and mentor offers are matched on. Null when the mentor has not "
            "classified this offering, which is not an error: it simply matches "
            "no filter."
        ),
    )
    service_offerings: list[LookupRef] = Field(
        default_factory=list,
        description=(
            "Every service offering this type covers, at most three, in the "
            "mentor's order. `service_offering` is the first of these."
        ),
    )
    application_stage: ApplicationStage | None = Field(
        default=None,
        description=(
            "Which stage of an application this offering is aimed at. Null means "
            "any stage. `other` always carries `custom_stage_label`, and no other "
            "value ever does — the two are tied in the database, so a client can "
            "render the label whenever the value is `other` without checking."
        ),
    )
    custom_stage_label: str | None = Field(
        default=None,
        description=(
            "The mentor's own wording, and **only** when `application_stage` is "
            "`other`. Render it in place of the stage name."
        ),
    )
    icon: SessionTypeIcon | None = Field(
        default=None,
        description=(
            "The icon to show, one of the design's Material Symbols names; "
            "`null` means pick one automatically (from the first topic)."
        ),
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
            service_offering=_taxonomy(row),
            service_offerings=_offerings(row),
            application_stage=_stage(row),
            custom_stage_label=(
                str(row["custom_stage_label"]) if row.get("custom_stage_label") else None
            ),
            icon=SessionTypeIcon(str(row["icon"])) if row.get("icon") else None,
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
            "Where this offering is held, **resolved** the same way the public "
            "endpoint resolves it: this offering's own conferencing option, "
            "else your default, else `google_meet`. Never null. The meeting "
            "**link** is generated per session and never appears here."
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
    # No description states how many rows hold a value. Three places in this
    # repository said these columns had "no value anywhere in the data", and the
    # migration that gave one a foreign key made all three false — a published
    # contract is the worst place to keep a fact with an expiry date on it.
    service_offering: LookupRef | None = Field(
        default=None,
        description=(
            "What kind of help this offering is, from the closed taxonomy at "
            "`/api/v1/catalog/service-offerings`. **Was `category`, a free-text "
            "string of your own** — it is now a reference to the axis mentees "
            "are matched on, so classifying an offering is what makes it "
            "findable rather than a private note."
        ),
    )
    service_offerings: list[LookupRef] = Field(
        default_factory=list,
        description=(
            "Every service offering this type covers, at most three, in your "
            "order. `service_offering` is the first of these."
        ),
    )
    application_stage: ApplicationStage | None = Field(
        default=None,
        description="Which stage of an application this offering is aimed at.",
    )
    custom_stage_label: str | None = Field(
        default=None,
        description=(
            "Your own wording, and only when `application_stage` is `other`. "
            "Sending one with any other stage is refused, and so is `other` "
            "without one."
        ),
    )
    icon: SessionTypeIcon | None = Field(
        default=None,
        description=(
            "The icon to show, one of the design's Material Symbols names; "
            "`null` means pick one automatically (from the first topic)."
        ),
    )

    requires_booking_confirmation: bool | None = Field(
        default=None,
        description=(
            "This offering's own approval setting; `null` means it follows your mentor profile's."
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
            requires_booking_confirmation=(
                None
                if row["requires_booking_confirmation"] is None
                else bool(row["requires_booking_confirmation"])
            ),
            service_offering=_taxonomy(row),
            service_offerings=_offerings(row),
            application_stage=_stage(row),
            custom_stage_label=(
                str(row["custom_stage_label"]) if row.get("custom_stage_label") else None
            ),
            icon=SessionTypeIcon(str(row["icon"])) if row.get("icon") else None,
        )


def _refuse_mismatched_label[Write: MentorSessionTypeWrite | MentorSessionTypePatch](
    model: Write,
) -> Write:
    """Mirror the symmetric `CHECK` at the boundary.

    **One function, called from both write models**, because the rule is one rule
    — a copy in each would be non-negotiable #8 in its plainest form, and the
    copy somebody forgets is the one that stops matching the database.

    **Only when both fields are present.** On a `PATCH` an absent field means
    *leave it alone*, so a request naming only `custom_stage_label` cannot be
    judged here — the database still refuses the combination the row would end
    up in, which is the guarantee. Judging it here anyway would refuse a legal
    edit: setting the label on an offering that is already `other`.
    """
    stage = model.application_stage
    label = model.custom_stage_label
    if stage is None or (label is None and stage is not ApplicationStage.OTHER):
        return model
    if stage is ApplicationStage.OTHER and label is None:
        raise ValueError("application_stage 'other' needs custom_stage_label")
    if stage is not ApplicationStage.OTHER and label is not None:
        raise ValueError(
            f"custom_stage_label belongs only to 'other'; this stage is {stage.value!r}"
        )
    return model


class MentorSessionTypeWrite(Normalised):
    """A new offering, as its mentor describes it.

    **`meeting_venue` is deliberately absent.** An offering is held on one of the
    mentor's `mentor_conferencing_options`, and nothing yet lists or creates
    those — so a value here could only be a provider name this endpoint would
    have to turn into a row, inventing a `custom_url` it has no way to ask for.
    A new offering leaves `conferencing_option_id` null, which resolves to the
    mentor's default and then to the platform fallback, so it is never null on
    the way out. Per-offering venue arrives with the surface that manages
    options (settled decision #21).

    **`is_active` is absent too, and that is not the same reason.** A new
    offering is active; there is no draft state, and `POST {"is_active": false}`
    would be a client asking to create something invisible. It is writable on
    `PATCH`, where switching one off is the whole point.
    """

    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    #: The `CHECK` on the column, restated at the boundary so a bad value is a
    #: 422 naming the field rather than a 500 naming a constraint. Pinned against
    #: the constraint by a test, per non-negotiable #8 — the copy is real and
    #: this is the mechanism that keeps it honest.
    duration_minutes: int = Field(ge=5, le=480)
    #: **The product rule, and the boundary is where it lives** (settled decision
    #: #104). 24 hours is the platform floor and 72 the current ceiling; the
    #: column's `CHECK` is sanity only — `BETWEEN 0 AND 43200` — because a
    #: database refuses what is *impossible* and an application refuses what is
    #: *disallowed*. When `booking_policies` lands this range moves there and
    #: becomes a config change rather than a migration.
    #:
    #: The default is the floor rather than a value a mentor picked, which is the
    #: same thing the column default says and the reason it says it.
    min_notice_minutes: int = Field(default=1440, ge=1440, le=4320)
    #: The taxonomy row, by id. Optional: an unclassified offering is bookable
    #: and simply matches no filter, and forcing a mentor to classify before they
    #: can sell would put a required field in front of the thing they came to do.
    service_offering_id: UUID | None = None
    #: The set, in order, at most `MAX_SESSION_TYPE_OFFERINGS` (#200). Its first is
    #: what `service_offering_id` reports. `[]` clears it; absent leaves it.
    service_offering_ids: list[UUID] | None = Field(
        default=None,
        max_length=MAX_SESSION_TYPE_OFFERINGS,
        description=(
            f"The service offerings this type covers, at most "
            f"{MAX_SESSION_TYPE_OFFERINGS}, in the order to show them; each once. "
            "`[]` clears them. Send this or `service_offering_id`, not both."
        ),
    )
    application_stage: ApplicationStage | None = None
    #: Only with `OTHER`, and required by it. Enforced here **and** by a symmetric
    #: `CHECK`: the database refuses what is impossible, and this turns the same
    #: refusal into a 422 naming the field rather than a 500 naming a constraint.
    custom_stage_label: str | None = Field(default=None, max_length=100)
    #: `null` inherits the mentor's own setting; `true` asks the mentor to accept
    #: each request, `false` confirms bookings at once (#199). Booking already
    #: resolves it with `COALESCE`, so this only makes it writable.
    requires_booking_confirmation: bool | None = Field(
        default=None,
        description=(
            "Whether bookings of this offering wait for your approval. `null` "
            "follows your own setting on your mentor profile; `true` or `false` "
            "overrides it for this offering."
        ),
    )
    #: One of the design's icons, or `null` for the automatic pick (#198).
    icon: SessionTypeIcon | None = None
    #: The intake questions, created **in the same transaction** as the offering
    #: (#196): a question refused refuses the whole create, so a mentor never
    #: ends up with a live offering and half its form. Each is a `QuestionWrite`,
    #: exactly as `POST .../questions` takes one, at most `MAX_QUESTIONS`.
    questions: list[QuestionWrite] = Field(
        default_factory=list,
        max_length=MAX_QUESTIONS,
        description=(
            f"The offering's intake questions, at most {MAX_QUESTIONS}, created with it "
            "in one transaction — any invalid question refuses the whole request."
        ),
    )

    @model_validator(mode="after")
    def _label_matches_stage(self) -> Self:
        _refuse_two_offering_fields(self)
        return _refuse_mismatched_label(self)


class MentorSessionTypePatch(Normalised):
    """A change to one offering. Every field optional; absent is not null.

    **`is_active` appears here and not on the create model.** Deactivating is a
    bare boolean with no cascade — it hides the offering from new bookings and
    leaves existing ones alone — so industry practice keeps it a field rather
    than an action endpoint, which is reserved for transitions with side effects.
    Draft-to-publish, when it lands, *is* such a transition and gets its own
    endpoint; it must not be modelled as `PATCH {"status": ...}`.

    **Nothing refuses a deactivation any more.** While
    `trg_refuse_retiring_a_primary_offering` existed this toggle needed the same
    `409` mapping as `DELETE` or it returned a 500. The trigger went with the
    pointer, so the toggle is now plain — see `test_offering_retirement.py`.
    """

    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    min_notice_minutes: int | None = Field(default=None, ge=1440, le=4320)
    #: The taxonomy row, by id. Optional: an unclassified offering is bookable
    #: and simply matches no filter, and forcing a mentor to classify before they
    #: can sell would put a required field in front of the thing they came to do.
    service_offering_id: UUID | None = None
    #: The set, in order, at most `MAX_SESSION_TYPE_OFFERINGS` (#200). Its first is
    #: what `service_offering_id` reports. `[]` clears it; absent leaves it.
    service_offering_ids: list[UUID] | None = Field(
        default=None,
        max_length=MAX_SESSION_TYPE_OFFERINGS,
        description=(
            f"The service offerings this type covers, at most "
            f"{MAX_SESSION_TYPE_OFFERINGS}, in the order to show them; each once. "
            "`[]` clears them. Send this or `service_offering_id`, not both."
        ),
    )
    application_stage: ApplicationStage | None = None
    #: Only with `OTHER`, and required by it. Enforced here **and** by a symmetric
    #: `CHECK`: the database refuses what is impossible, and this turns the same
    #: refusal into a 422 naming the field rather than a 500 naming a constraint.
    custom_stage_label: str | None = Field(default=None, max_length=100)
    #: `null` inherits the mentor's own setting; `true` asks the mentor to accept
    #: each request, `false` confirms bookings at once (#199). Booking already
    #: resolves it with `COALESCE`, so this only makes it writable.
    requires_booking_confirmation: bool | None = Field(
        default=None,
        description=(
            "Whether bookings of this offering wait for your approval. `null` "
            "follows your own setting on your mentor profile; `true` or `false` "
            "overrides it for this offering."
        ),
    )
    #: One of the design's icons, or `null` for the automatic pick (#198).
    icon: SessionTypeIcon | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _label_matches_stage(self) -> Self:
        _refuse_two_offering_fields(self)
        return _refuse_mismatched_label(self)


class SessionTypeCreated(BaseModel):
    """What creating a session type answers (#196)."""

    id: UUID
    question_ids: list[UUID] = Field(
        description="The ids of the `questions` sent, in the order they were sent."
    )
