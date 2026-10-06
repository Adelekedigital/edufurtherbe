"""Registering interest in something that has not shipped, as a client reads and writes it."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.interest import FEATURE_PATTERN, MAX_FEATURES_PER_ACCOUNT, is_feature_key

__all__ = ["InterestRead", "InterestWrite"]

#: Said once, in the two places a client meets it — the write's refusal and the
#: read's description — rather than twice in prose that can drift apart.
_FEATURE_DESCRIPTION = (
    "What is being waited for, as a lowercase slug: a letter, then letters, "
    f"digits or underscores, 2 to 40 characters (`{FEATURE_PATTERN}`).\n\n"
    "**An open vocabulary, not an enum.** A new coming-soon control anywhere in "
    "the product works with no backend release, which is the point — the cost "
    "is that a typo records a key nobody will notify against, so send the same "
    "spelling the rest of the product uses. `payments` and `new_mentors` are "
    "the keys in use today."
)


class InterestWrite(BaseModel):
    """A feature to be told about when it ships.

    **Pressing twice is pressing once**, so there is no `Idempotency-Key`: the
    second request answers `204` and changes nothing.
    """

    model_config = ConfigDict(extra="forbid")

    feature: str = Field(description=_FEATURE_DESCRIPTION)

    @field_validator("feature", mode="after")
    @classmethod
    def _a_well_formed_key(cls, value: str) -> str:
        """Case is folded; nothing else is.

        **`mode="after"`, and that is load-bearing.** As a `before` validator
        this ran ahead of the `str` type check and did its own `str(value)`, so
        JSON `null`, `true` and `false` arrived as `"none"`, `"true"` and
        `"false"` — each of which satisfies the slug pattern and the column's
        `CHECK`. A client whose `feature` prop was uninitialised would have sent
        `{"feature": null}`, received `204`, switched its button to *we'll let
        you know*, and created a `none` row nobody could ever be notified
        against. That is exactly the failure this shape check is the only
        defence against, and running before the type check is what defeated it.
        Found by review, not by a test.

        **Case and whitespace are not the same kind of difference**, which is
        the whole of why one is normalised and the other refused. `Payments` and
        `payments` are one key written two ways — folding them stops one
        intention becoming two rows, and case carries no meaning in a slug.
        `" payments "` is not a case variant; it is a malformed key, and
        accepting it would hide a client bug while making two spellings collide.
        So the refusal is what gets that one fixed.

        An earlier version of this comment argued against trimming on grounds
        that applied just as well to lowercasing, which would have made the
        reasoning self-contradicting rather than merely arguable.

        `is_feature_key` is the one home for the shape (`domain/interest.py`),
        pinned to the column's `CHECK` by a test — so a key this accepts cannot
        be one the insert refuses.
        """
        lowered = value.lower()
        if not is_feature_key(lowered):
            raise ValueError(
                "a feature key is a lowercase slug: a letter, then letters, "
                "digits or underscores, 2 to 40 characters"
            )
        return lowered


class InterestRead(BaseModel):
    """One thing this account is waiting for.

    **An object rather than a bare string**, so this list is shaped like every
    other list behind ADR 0016's envelope. A client that only needs membership
    reads `feature` and ignores the rest; one that wants to say *when* they
    asked has it without a second endpoint.
    """

    feature: str = Field(description=_FEATURE_DESCRIPTION)
    registered_at: dt.datetime = Field(
        description=(
            "When this was first registered. **Unchanged by pressing again** — "
            "the repeat is a no-op, so this is when they first asked, not when "
            "they last did."
        )
    )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> InterestRead:
        # `created_at` is the column; `registered_at` is what it means here, and
        # a second column holding the same instant would be a derived value
        # persisted.
        return cls(feature=str(row["feature"]), registered_at=row["created_at"])


#: What a client can rely on about the list's size: the cap is per account, so
#: the whole answer always fits in one page. Named for the route's description,
#: which would otherwise restate the number.
MAX_INTERESTS = MAX_FEATURES_PER_ACCOUNT
