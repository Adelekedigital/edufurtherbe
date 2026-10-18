"""A mentee's top mentor matches and why (`/me/mentor-matches`, decision 265)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.api.schemas.mentors import MentorSummaryRead
from app.domain.matching import Reason, Ref


class MatchRefRead(BaseModel):
    """A catalogue row: its id and the label a client shows."""

    id: str
    label: str

    @classmethod
    def of(cls, ref: Ref | None) -> MatchRefRead | None:
        return None if ref is None else cls(id=str(ref.id), label=ref.label)


class HelpAreasReason(BaseModel):
    """The mentor offers some of the help the mentee asked for."""

    code: Literal["help_areas"]
    offering_ids: list[str] = Field(
        description=(
            "The shared service offerings (`/catalog/service-offerings` ids), "
            "in the platform's order."
        )
    )


class FieldReason(BaseModel):
    """The mentor studied the mentee's field, or close to it."""

    code: Literal["field"]
    field: str = Field(
        description="The mentor's own course or programme that matched, as they wrote it."
    )


class OriginReason(BaseModel):
    """The mentor is from the mentee's country."""

    code: Literal["origin"]
    country: MatchRefRead


class StudyCountryReason(BaseModel):
    """The mentor studied in one of the mentee's destination countries."""

    code: Literal["study_country"]
    country: MatchRefRead


class DegreeReason(BaseModel):
    """The mentor holds the mentee's goal degree level, or a higher one."""

    code: Literal["degree"]
    level: MatchRefRead


MatchReason = Annotated[
    HelpAreasReason | FieldReason | OriginReason | StudyCountryReason | DegreeReason,
    Field(discriminator="code"),
]


def _reason(reason: Reason) -> Any:
    ref = MatchRefRead.of(reason.ref)
    match reason.code:
        case "help_areas":
            return HelpAreasReason(
                code="help_areas", offering_ids=[str(o) for o in reason.offering_ids]
            )
        case "field":
            return FieldReason(code="field", field=reason.field or "")
        case "origin":
            return OriginReason(code="origin", country=ref)  # type: ignore[arg-type]
        case "study_country":
            return StudyCountryReason(code="study_country", country=ref)  # type: ignore[arg-type]
        case "degree":
            return DegreeReason(code="degree", level=ref)  # type: ignore[arg-type]


class MentorMatchRead(BaseModel):
    """One match: the `/mentors` card unchanged, and why it matched."""

    mentor: MentorSummaryRead = Field(description="Exactly one `GET /mentors` item.")
    reasons: list[MatchReason] = Field(
        description=(
            "The signals that matched, **in ranking order**: `help_areas`, `field`, "
            "`origin`, `study_country`, `degree`. Lead with the first. Empty when "
            "nothing matched; a free slot is never a reason."
        ),
    )
    level: MatchRefRead | None = Field(
        default=None,
        description="The mentor's highest degree level, for the line `{level} · {study_country}`.",
    )
    study_country: MatchRefRead | None = Field(
        default=None, description="Where the mentor mainly studied."
    )

    @classmethod
    def of(
        cls, row: dict[str, Any], found: list[Reason], level: Ref | None, country: Ref | None
    ) -> MentorMatchRead:
        return cls(
            mentor=MentorSummaryRead.from_row(row),
            reasons=[_reason(r) for r in found],
            level=MatchRefRead.of(level),
            study_country=MatchRefRead.of(country),
        )


class DegreeBasisRead(BaseModel):
    """The mentee's degree goal: the level, and what they typed when it is not one."""

    id: str | None = None
    label: str | None = None
    raw: str | None = None


class MatchBasisRead(BaseModel):
    """The caller's own answers the matches were ranked on."""

    degree: DegreeBasisRead | None = None
    field: str | None = None
    country: MatchRefRead | None = Field(
        default=None, description='The first destination; `null` for "Not sure yet".'
    )
    origin: MatchRefRead | None = None


class MentorMatchesRead(BaseModel):
    """The caller's top matches, whether the top one is exact, and what they rest on."""

    matches: list[MentorMatchRead] = Field(
        description="Best first. Empty only when no mentor can be booked at all."
    )
    exact: bool = Field(
        description=(
            "`true` when the top match meets every primary signal you answered: "
            "at least one shared help area, your field, your origin. `false` "
            "otherwise, with no matches, or when you answered none of the three."
        )
    )
    based_on: MatchBasisRead
    for_mentor_bookable: bool | None = Field(
        default=None,
        description=(
            "Only when `for_mentor` was sent: `true` and `matches` holds just that "
            "mentor, or `false` and `matches` is the usual top list without them."
        ),
    )
