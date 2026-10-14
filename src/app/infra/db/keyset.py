"""A timestamp keyset position: parsed once, compared once (non-negotiable #8).

Every list paged on ``(timestamp, id)`` receives its cursor's sort key as text
(`schemas.common.encode_cursor`), so it parses it back. A token that survives
base64 but holds no timestamp is a **client** error: raising `ValidationError`
here turns a `fromisoformat` 500 into the 422 the envelope documents. Six
readers wrote this parse and comparison out by hand before it moved here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import literal, tuple_

from app.core.errors import ValidationError

__all__ = ["after", "position"]


def position(cursor: tuple[str, UUID]) -> tuple[dt.datetime, UUID]:
    """The cursor's ``(timestamp, id)``, or a 422 when the key is no timestamp."""
    raw, after_id = cursor
    try:
        return dt.datetime.fromisoformat(raw), after_id
    except ValueError as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc


def after(sort_key: Any, row_id: Any, cursor: tuple[str, UUID], *, ascending: bool = False) -> Any:
    """Rows past the cursor in list order: later for ``ascending``, earlier for
    the newest-first default, ties broken by the id the same way."""
    at, after_id = position(cursor)
    here = tuple_(sort_key, row_id)
    bound = tuple_(literal(at), literal(after_id))
    return here > bound if ascending else here < bound
