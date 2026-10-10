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

import datetime as dt
from typing import Self, cast
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import Normalised, publish_window_minimum
from app.api.schemas.intake import QuestionRead, QuestionWrite
from app.api.schemas.profile import LookupRef
from app.core.config import BOOKING_WINDOW_CEILING, MIN_BOOKING_WINDOW_DAYS
from app.domain.availability import (
    BREAK_AFTER_MINUTES,
    DEFAULT_DURATION_MINUTES,
    MIN_NOTICE_MINUTES,
    SESSION_DURATION_MINUTES,
)
from app.domain.enums import ApplicationStage, ConferencingProvider, SessionTypeIcon
from app.domain.intake import MAX_QUESTIONS
from app.domain.meetings import PLATFORM_DEFAULT_PROVIDER
from app.domain.sessions import (
    MAX_SESSION_TYPE_OFFERINGS,
    first_stage,
    named_stages,
    stage_label_problem,
)


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(str(value))


def _offerings(row: dict[str, object]) -> list[LookupRef]:
    """The service offerings a type covers, in the mentor's order (#205).

    **`LookupRef`s rather than bare slugs**, matching `MentorProfileRead.
    offerings`: a slug alone would make every client join against
    `/catalog/service-offerings` to render a chip, for a six-row table the
    response can carry inline at no cost.
    """
    return [LookupRef(**o) for o in row.get("service_offerings") or []]  # type: ignore[attr-defined]


def _taxonomy(row: dict[str, object]) -> LookupRef | None:
    """The single `service_offering` kept this release: **the first of the set**
    (#205), so the old field and the new list can never disagree."""
    offerings = _offerings(row)
    return offerings[0] if offerings else None


def _refuse_two_set_fields(model: BaseModel) -> None:
    """Each set and its legacy single field: `service_offering_ids` beside
    `service_offering_id` (#205), `application_stages` beside `application_stage`
    (#215). A request sending both of a pair has two answers and no rule for
    which wins."""
    fields = model.model_fields_set
    for plural, single in (
        ("service_offering_ids", "service_offering_id"),
        ("application_stages", "application_stage"),
    ):
        if plural in fields and single in fields:
            raise ValueError(f"send {plural} or {single}, not both")
    # `[]` is any stage and absent is unchanged; `null` would be a third
    # spelling of one of them, so it is refused rather than guessed at (#215).
    if "application_stages" in fields and getattr(model, "application_stages", None) is None:
        raise ValueError("application_stages: send [] for any stage, or leave it out")
    ids = getattr(model, "service_offering_ids", None)
    if ids is not None and len(set(ids)) != len(ids):
        raise ValueError("service_offering_ids: an offering appears twice")


def _stages(row: dict[str, object]) -> list[ApplicationStage]:
    """The stages a type is aimed at, in the mentor's order (#215); empty is any."""
    return [ApplicationStage(str(v)) for v in row.get("application_stages") or []]  # type: ignore[attr-defined]


def _stage(row: dict[str, object]) -> ApplicationStage | None:
    """The single `application_stage` kept this release: **the first of the set**
    (#215), so the old field and the new list can never disagree."""
    return first_stage(_stages(row))


#: What a set of stages may hold: each stage once, so at most all of them.
MAX_STAGES = len(ApplicationStage)


#: One description for both writes. The static bound is the ceiling the column
#: holds; the configured maximum is checked where settings are known
#: (`deps.core.refuse_window_out_of_range`), because a `Field(le=...)` is fixed at import.
WINDOW_WRITE_DESCRIPTION = (
    f"How many days ahead this offering can be booked: {MIN_BOOKING_WINDOW_DAYS} to the "
    "platform maximum "
    "(`max_booking_window_days` on your mentor profile); outside that range is a 422 "
    "at `/booking_window_days`, except that resending the value already stored for "
    "this offering is accepted. `null` follows your default on your mentor profile."
)


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
    application_stages: list[ApplicationStage] = Field(
        default_factory=list,
        description=(
            "Every stage of an application this offering is aimed at, in the "
            "mentor's order. **Empty means any stage.** When it holds `other`, "
            "`custom_stage_label` carries the mentor's wording, and it never "
            "does otherwise."
        ),
    )
    application_stage: ApplicationStage | None = Field(
        default=None,
        description=(
            "**Deprecated: read `application_stages`.** The first of "
            "`application_stages`, kept for one release; null when that list is "
            "empty."
        ),
    )
    custom_stage_label: str | None = Field(
        default=None,
        description=(
            "The mentor's own wording, and **only** when `application_stages` "
            "holds `other`. Render it in place of that stage's name."
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
            f"default, else `{PLATFORM_DEFAULT_PROVIDER.value}`. Required, and never null — the "
            "platform fallback is the last step precisely so this field cannot "
            "be absent. The meeting **link** is generated per session and never "
            "appears here: a static room means back-to-back sessions share it "
            "and an early joiner walks into the previous one."
        ),
    )
    is_featured: bool = Field(
        default=False,
        description=("The one offering this mentor puts first (at most one); it is listed first."),
    )
    requires_booking_confirmation: bool = Field(
        description=(
            "Whether booking this offering is a **request** the mentor must accept "
            "(`true`), or confirms at once (`false`). **Resolved**: the offering's own "
            "setting, else its mentor's, exactly as `POST /api/v1/sessions` applies it. "
            "Show it before the mentee sends the request."
        ),
    )
    booking_window_days: int = Field(
        description=(
            "How many days ahead this offering can be booked, **resolved**: its own "
            "window, else its mentor's, else the platform default — and never more "
            "than the platform maximum. Show dates up to this in the booking modal."
        ),
    )
    questions: list[QuestionRead] = Field(
        default_factory=list,
        description=(
            "The intake form a mentee fills in when booking this offering: its live "
            "questions in order, choice options included. Empty means no form. "
            "Answer them as `answers` on `POST /api/v1/sessions` (#207)."
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
            application_stages=_stages(row),
            application_stage=_stage(row),
            custom_stage_label=(
                str(row["custom_stage_label"]) if row.get("custom_stage_label") else None
            ),
            icon=SessionTypeIcon(str(row["icon"])) if row.get("icon") else None,
            is_featured=bool(row.get("is_featured")),
            requires_booking_confirmation=bool(row["requires_booking_confirmation"]),
            booking_window_days=int(str(row["booking_window_days"])),
            questions=[
                QuestionRead.from_row(q)
                for q in cast("list[dict[str, object]]", row.get("questions") or [])
            ],
        )


class PendingDeletionRead(BaseModel):
    """When a scheduled offering goes, and what holds it (#218). Derived from its
    sessions each read, so a cancellation moves both."""

    deletes_after: dt.datetime | None = Field(
        description=(
            "When the last booked session on it ends (UTC). `null` once none is "
            "left: it is deleted at the next hourly run."
        )
    )
    booked_count: int = Field(
        description="Sessions still booked on it: awaiting your decision, or agreed."
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
        description=(
            "How long a session of this type runs, and the step between slots — "
            "**resolved**: this offering's own, else your default, else "
            f"{DEFAULT_DURATION_MINUTES}. `duration_inherited` says which."
        )
    )
    min_notice_minutes: int = Field(
        description=(
            "How far ahead a booking must be made against this offering, "
            "**resolved** like `duration_minutes`; `min_notice_inherited` says which."
        )
    )
    duration_inherited: bool = Field(
        description=(
            "`true` when this offering sets no length of its own and "
            "`duration_minutes` is your default (or the platform's)."
        )
    )
    min_notice_inherited: bool = Field(
        description=(
            "`true` when this offering sets no notice of its own and "
            "`min_notice_minutes` is your default (or the platform's)."
        )
    )
    meeting_venue: ConferencingProvider = Field(
        description=(
            "Where this offering is held, **resolved** the same way the public "
            "endpoint resolves it: this offering's own conferencing option, "
            f"else your default, else `{PLATFORM_DEFAULT_PROVIDER.value}`. Never null. The meeting "
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
    application_stages: list[ApplicationStage] = Field(
        default_factory=list,
        description=(
            "Every stage of an application this offering is aimed at, in your "
            "order; empty means any stage."
        ),
    )
    application_stage: ApplicationStage | None = Field(
        default=None,
        description=("**Deprecated: read `application_stages`.** Its first, kept for one release."),
    )
    custom_stage_label: str | None = Field(
        default=None,
        description=(
            "Your own wording, and only when `application_stages` holds `other`. "
            "Sending one without `other` is refused, and so is `other` without one."
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
    booking_window_days: int | None = Field(
        default=None, description="This offering's own window in days; `null` inherits."
    )
    effective_booking_window_days: int = Field(
        description=(
            "The window this offering actually uses: its own, else yours, else the "
            "platform default, capped at the platform maximum."
        ),
    )
    break_after_minutes: int | None = Field(
        default=None, description="This offering's own break in minutes; `null` inherits."
    )
    is_featured: bool = Field(
        default=False,
        description=(
            "Whether this is the offering you put first. At most one; featuring "
            "another un-features this, and hiding it un-features it."
        ),
    )
    uses_own_windows: bool = Field(
        default=False,
        description=(
            "True when this offering books into its own scheduling windows and "
            "nowhere else; false when it follows your Calendar weekly hours."
        ),
    )
    question_count: int = Field(
        default=0,
        ge=0,
        description=(
            "How many questions this offering's booking form asks: the length of "
            "`GET /me/session-types/{id}/questions` for it."
        ),
    )
    #: The live sessions holding it, on every row — so the delete confirm can say
    #: whether deleting waits for them before the mentor chooses (#218). The same
    #: two figures `pending_deletion` carries once it does.
    booked_count: int = Field(
        description="Sessions booked on it now: awaiting your decision, or agreed."
    )
    last_booked_ends_at: dt.datetime | None = Field(
        description=(
            "When the last of those ends (UTC), or `null` if none. Deleting it now "
            "would be scheduled for this time; with none, it is deleted at once."
        )
    )
    pending_deletion: PendingDeletionRead | None = Field(
        default=None,
        description=(
            "Set when you deleted this offering while sessions were booked on it: it "
            "is hidden, and goes once the last of them is over. `null` otherwise. "
            "`POST .../restore` cancels it."
        ),
    )

    @classmethod
    def from_row(cls, row: dict[str, object]) -> OwnSessionTypeRead:
        # Read once, used twice: the row's figures and the schedule's are the
        # same values, so they cannot disagree.
        booked_count = int(str(row.get("booked_count") or 0))
        last_booked_ends_at = cast("dt.datetime | None", row.get("deletes_after"))
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            description=str(row["description"]) if row["description"] else None,
            duration_minutes=int(str(row["duration_minutes"])),
            min_notice_minutes=int(str(row["min_notice_minutes"])),
            duration_inherited=bool(row["duration_inherited"]),
            min_notice_inherited=bool(row["min_notice_inherited"]),
            meeting_venue=ConferencingProvider(str(row["meeting_venue"])),
            is_active=bool(row["is_active"]),
            requires_booking_confirmation=(
                None
                if row["requires_booking_confirmation"] is None
                else bool(row["requires_booking_confirmation"])
            ),
            booking_window_days=_int_or_none(row.get("booking_window_days")),
            effective_booking_window_days=int(str(row["effective_booking_window_days"])),
            break_after_minutes=_int_or_none(row.get("break_after_minutes")),
            service_offering=_taxonomy(row),
            service_offerings=_offerings(row),
            application_stages=_stages(row),
            application_stage=_stage(row),
            custom_stage_label=(
                str(row["custom_stage_label"]) if row.get("custom_stage_label") else None
            ),
            icon=SessionTypeIcon(str(row["icon"])) if row.get("icon") else None,
            is_featured=bool(row.get("is_featured")),
            uses_own_windows=bool(row.get("uses_own_windows")),
            question_count=int(str(row.get("question_count") or 0)),
            booked_count=booked_count,
            last_booked_ends_at=last_booked_ends_at,
            pending_deletion=(
                PendingDeletionRead(deletes_after=last_booked_ends_at, booked_count=booked_count)
                if row.get("deletion_scheduled_at") is not None
                else None
            ),
        )


def _refuse_mismatched_label[Write: MentorSessionTypeWrite | MentorSessionTypePatch](
    model: Write,
) -> Write:
    """`stage_label_problem`, asked at the boundary so the answer is a 422 early.

    **One function, called from both write models**, and the rule itself is the
    domain's — the store asks it again against the row's final state, because
    the database's `CHECK` can see only the first stage (#215).

    **On a `PATCH`, only when both halves are sent.** An absent field means
    *leave it alone*, so a request naming only `custom_stage_label` cannot be
    judged here; the store judges it against what the row holds. Judging it here
    anyway would refuse a legal edit: setting the label on an offering whose set
    already holds `other`.
    """
    fields = model.model_fields_set
    if isinstance(model, MentorSessionTypePatch) and not (
        "custom_stage_label" in fields and ({"application_stages", "application_stage"} & fields)
    ):
        return model
    sent = named_stages(model.model_dump(exclude_unset=True)) or []
    problem = stage_label_problem(sent, model.custom_stage_label)
    if problem is not None:
        raise ValueError(problem[1])
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
    #:
    #: **Null, or absent, follows the mentor's default** (#216), then the
    #: platform's.
    duration_minutes: int | None = Field(
        default=None,
        ge=SESSION_DURATION_MINUTES[0],
        le=SESSION_DURATION_MINUTES[1],
        description=(
            f"Minutes ({SESSION_DURATION_MINUTES[0]}-{SESSION_DURATION_MINUTES[1]}); "
            "`null` follows your default on your mentor profile, then "
            f"{DEFAULT_DURATION_MINUTES}."
        ),
    )
    #: **The product rule, and the boundary is where it lives** (settled decision
    #: #104). 24 hours is the platform floor and 72 the current ceiling; the
    #: column's `CHECK` is sanity only — `BETWEEN 0 AND 43200` — because a
    #: database refuses what is *impossible* and an application refuses what is
    #: *disallowed*. When `booking_policies` lands this range moves there and
    #: becomes a config change rather than a migration.
    #:
    #: **Null, or absent, follows the mentor's default, then the floor** (#216).
    min_notice_minutes: int | None = Field(
        default=None,
        ge=MIN_NOTICE_MINUTES[0],
        le=MIN_NOTICE_MINUTES[1],
        description=(
            f"Minutes of notice ({MIN_NOTICE_MINUTES[0]}-{MIN_NOTICE_MINUTES[1]}, 24 "
            "to 72 hours); `null` follows your default, then 24 hours."
        ),
    )
    #: The taxonomy row, by id. Optional: an unclassified offering is bookable
    #: and simply matches no filter, and forcing a mentor to classify before they
    #: can sell would put a required field in front of the thing they came to do.
    service_offering_id: UUID | None = None
    #: The set, in order, at most `MAX_SESSION_TYPE_OFFERINGS` (#205). Its first is
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
    #: The stage set, in order, each once (#215). `[]` means any stage; absent
    #: leaves it. `application_stage` below is a set of one, for one release.
    application_stages: list[ApplicationStage] | None = Field(
        default=None,
        max_length=MAX_STAGES,
        description=(
            "Every stage this offering is aimed at, in the order to show them; each "
            "once. `[]` means any stage. Send this or `application_stage`, not both."
        ),
    )
    application_stage: ApplicationStage | None = Field(
        default=None,
        description="**Deprecated: send `application_stages`.** A set of one; `null` clears it.",
    )
    #: Only when the set holds `OTHER`, and required by it (`stage_label_problem`).
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
    #: How far ahead, and the break after each session (#204). `null` inherits
    #: the mentor's default, then the platform's (the full horizon, no break).
    booking_window_days: int | None = Field(
        default=None,
        ge=1,
        le=BOOKING_WINDOW_CEILING,
        json_schema_extra=publish_window_minimum,
        description=WINDOW_WRITE_DESCRIPTION,
    )
    break_after_minutes: int | None = Field(
        default=None,
        ge=BREAK_AFTER_MINUTES[0],
        le=BREAK_AFTER_MINUTES[1],
        description=(
            "Minutes of break after each session of this offering "
            f"({BREAK_AFTER_MINUTES[0]}-{BREAK_AFTER_MINUTES[1]}); no session of yours "
            "can start inside it. `null` follows your default."
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
        _refuse_two_set_fields(self)
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
    #: `null` switches the offering to inheriting (#216); absent leaves it.
    duration_minutes: int | None = Field(
        default=None,
        ge=SESSION_DURATION_MINUTES[0],
        le=SESSION_DURATION_MINUTES[1],
        description="Minutes; `null` follows your default, absent leaves it as it is.",
    )
    min_notice_minutes: int | None = Field(
        default=None,
        ge=MIN_NOTICE_MINUTES[0],
        le=MIN_NOTICE_MINUTES[1],
        description=(
            "Minutes of notice, 24 to 72 hours; `null` follows your default, absent "
            "leaves it as it is."
        ),
    )
    #: The taxonomy row, by id. Optional: an unclassified offering is bookable
    #: and simply matches no filter, and forcing a mentor to classify before they
    #: can sell would put a required field in front of the thing they came to do.
    service_offering_id: UUID | None = None
    #: The set, in order, at most `MAX_SESSION_TYPE_OFFERINGS` (#205). Its first is
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
    #: The stage set, in order, each once (#215). `[]` means any stage; absent
    #: leaves it. `application_stage` below is a set of one, for one release.
    application_stages: list[ApplicationStage] | None = Field(
        default=None,
        max_length=MAX_STAGES,
        description=(
            "Every stage this offering is aimed at, in the order to show them; each "
            "once. `[]` means any stage. Send this or `application_stage`, not both."
        ),
    )
    application_stage: ApplicationStage | None = Field(
        default=None,
        description="**Deprecated: send `application_stages`.** A set of one; `null` clears it.",
    )
    #: Only when the set holds `OTHER`, and required by it (`stage_label_problem`).
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
    #: How far ahead, and the break after each session (#204). `null` inherits
    #: the mentor's default, then the platform's (the full horizon, no break).
    booking_window_days: int | None = Field(
        default=None,
        ge=1,
        le=BOOKING_WINDOW_CEILING,
        json_schema_extra=publish_window_minimum,
        description=WINDOW_WRITE_DESCRIPTION,
    )
    break_after_minutes: int | None = Field(
        default=None,
        ge=BREAK_AFTER_MINUTES[0],
        le=BREAK_AFTER_MINUTES[1],
        description=(
            "Minutes of break after each session of this offering "
            f"({BREAK_AFTER_MINUTES[0]}-{BREAK_AFTER_MINUTES[1]}); no session of yours "
            "can start inside it. `null` follows your default."
        ),
    )
    #: One of the design's icons, or `null` for the automatic pick (#198).
    icon: SessionTypeIcon | None = None
    is_active: bool | None = None
    #: **`bool`, not `bool | None`** — an explicit `null` is a `422`, and the
    #: `None` default is never written (`exclude_unset`); it keeps the spec from
    #: publishing a default a generated client would type as always sent (#217).
    is_featured: bool = Field(  # type: ignore[assignment]
        default=None,
        description=(
            "`true` puts this offering first and un-features your current one; "
            "`false` un-features it. Only an offering on offer can be featured."
        ),
    )

    @model_validator(mode="after")
    def _label_matches_stage(self) -> Self:
        _refuse_two_set_fields(self)
        return _refuse_mismatched_label(self)


class DeletionScheduledRead(BaseModel):
    """What `DELETE` answers when booked sessions hold an offering: `202` (#218)."""

    scheduled: bool = Field(default=True, description="Always `true` on a `202`.")
    deletes_after: dt.datetime = Field(description="When the last booked session on it ends (UTC).")
    booked_count: int = Field(description="Sessions still booked on it.")


class SessionTypeCreated(BaseModel):
    """What creating a session type answers (#196)."""

    id: UUID
    question_ids: list[UUID] = Field(
        description="The ids of the `questions` sent, in the order they were sent."
    )
