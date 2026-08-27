"""What an admin sends to grant credits, and what comes back.

**Bounds live here, at the edge**, before the domain sees anything. The writer
refuses an over-cap quantity too — that is the rule's home — but a request
carrying five hundred should be answered by a validation error naming the field
rather than by a domain exception naming a constant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AdminCreditGrantRead", "AdminCreditGrantWrite", "AdminGrantHistoryRead"]

#: How many people one request may credit.
#:
#: **A bound on the blast radius, not a product rule.** An outage costs a cohort
#: and the real batches are tens, not thousands; a request naming ten thousand
#: users is far more likely to be a mistake than an intention, and it would hold
#: a transaction open long enough to matter.
MAX_RECIPIENTS = 500


class AdminCreditGrantWrite(BaseModel):
    """One admin action: this many credits, to these people, optionally why."""

    model_config = ConfigDict(extra="forbid")

    #: Deduplicated by the writer rather than here, so a caller who sends the
    #: same id twice is not refused over something harmless — they are credited
    #: once and the response says so.
    user_ids: list[UUID] = Field(min_length=1, max_length=MAX_RECIPIENTS)

    #: The upper bound is the configured monthly grant, which this cannot state
    #: statically — the writer holds it, and the OpenAPI description renders it.
    quantity: int = Field(ge=1)

    #: Why, in the admin's own words, or nothing. **Optional deliberately**: an
    #: admin may credit people who never complained — goodwill after an outage
    #: reaches a cohort, not a queue of requests — and requiring prose for that
    #: is how a field fills with "goodwill".
    note: str | None = Field(default=None, max_length=1000)


class AdminCreditGrantRead(BaseModel):
    """What landed, and who could not be found.

    **`unresolved` is named rather than counted.** An admin who asked for six and
    credited five needs the sixth id to go and look at it; a number tells them
    only that something went wrong, which is the note `report_unresolved` already
    makes for the ETL.
    """

    granted: list[UUID]
    unresolved: list[UUID]
    quantity: int


class AdminGrantHistoryRead(BaseModel):
    """One past grant, as an audit reads it.

    **Granted *and* remaining**, because they answer different questions. "We
    gave them three" and "they still have three" are not the same fact, and a
    history showing only the first cannot say whether a goodwill gesture ever
    reached anybody — or whether it expired unspent, which is the outcome worth
    knowing about.

    Both parties are named. `granted_by` is the whole point of the table, and
    the recipient's name is what makes a page of ids readable at all.
    """

    id: UUID
    created_at: datetime
    note: str | None

    user_id: UUID
    recipient_name: str

    granted_by: UUID
    granted_by_name: str

    quantity_granted: int
    quantity_remaining: int
    expires_at: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AdminGrantHistoryRead:
        """**Full names, not the initial the public pages use.**

        `list_mentor_reviews` gives a first name and an initial because a
        stranger is reading it. This is an admin auditing who was credited, and
        an audit that cannot tell two people apart is not one.
        """
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            note=row["note"],
            user_id=row["user_id"],
            recipient_name=_full_name(row["recipient_first_name"], row["recipient_last_name"]),
            granted_by=row["granted_by"],
            granted_by_name=_full_name(row["granted_by_first_name"], row["granted_by_last_name"]),
            quantity_granted=row["quantity_granted"],
            quantity_remaining=row["quantity_remaining"],
            expires_at=row["expires_at"],
        )


def _full_name(first: str | None, last: str | None) -> str:
    """Whatever the row actually holds, joined.

    `users.last_name` is nullable and the ETL leaves it empty for some migrated
    accounts, so a naive f-string renders a trailing space on every one of them.
    """
    return " ".join(part for part in (first, last) if part)
