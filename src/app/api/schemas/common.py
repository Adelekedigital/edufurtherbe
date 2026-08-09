"""Shapes every list endpoint shares.

**The envelope is the part that must be right on day one.** A bare JSON array has
nowhere to put pagination metadata, so adding it later is a breaking change for
every client already parsing the array — which is why the migration package says
*"cursor pagination on every list endpoint. Retrofitting is a breaking API
change."*

So every list returns `Page`, including the ones that will never need a second
page. `degree_levels` has six rows and `next_cursor` will be `null` forever; the
cost is one key, and the benefit is that the day a list does grow, nothing on the
other side has to change.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.core.errors import ValidationError

#: Anything above this is clamped rather than refused. A client asking for 5,000
#: rows has made a mistake, and a 422 in the middle of an autocomplete is a worse
#: answer than the 50 it should have asked for.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 10

#: Lookup lists serve select boxes rather than autocomplete, and `countries` is
#: 249 rows a client wants in one call — so their page is larger than a search's.
LOOKUP_PAGE_SIZE = 300


class Page[T](BaseModel):
    """One page of results, and how to ask for the next.

    ``next_cursor`` is **opaque**. Clients must not construct, parse or reason
    about it — that is what lets the cursor's encoding change without breaking
    them, and it is the whole reason for preferring cursors to offsets. ``null``
    means there is no next page.
    """

    data: list[T]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Pass back as `cursor` to fetch the next page. Opaque — do not parse "
            "or construct it. `null` means this is the last page."
        ),
    )


def encode_cursor(display_name: str, row_id: UUID) -> str:
    """The keyset position, as one opaque token.

    Base64 of the two ordering columns. Opaque is not security — anyone can
    decode it — it is a contract: a client that cannot read the cursor cannot
    build one, so the encoding stays ours to change.
    """
    raw = f"{display_name}\x00{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str | None) -> tuple[str, UUID] | None:
    """A cursor back into a keyset position, or a refusal.

    A malformed cursor is a **client** error, not a server one, and not something
    to silently treat as "start from the beginning" — that would answer a paging
    bug with page one forever, which looks like working software and loses rows.
    """
    if cursor is None:
        return None
    try:
        name, _, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("\x00")
        return name, UUID(row_id)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc


def clamp_limit(limit: int | None) -> int:
    """The requested page size, bounded.

    One function rather than a `min()` at each call site: three list endpoints
    with three copies of the bound is three chances for one of them to be wrong,
    and the wrong one is a query nobody notices until it is slow.
    """
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


class Normalised(BaseModel):
    """Trims every string, and turns an emptied one into ``None``.

    **Declarative, so a new field cannot omit it by nobody thinking about it** —
    the same reasoning `NormalisedEmail` follows for lowercasing, and what ADR
    0016 point 3 means by normalising at the boundary. A form posts `""` for a
    field the user cleared; storing that gives a column holding two different
    spellings of "absent", and every later query has to remember both.

    Runs before validation, so `str | None` fields accept `""` and arrive as
    `None` rather than failing a length check on a value the user did not type.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalised: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                stripped = value.strip()
                normalised[key] = stripped or None
            else:
                normalised[key] = value
        return normalised
