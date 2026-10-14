"""The one timestamp keyset parse (`infra.db.keyset`): a 422, never a 500."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from app.core.errors import ValidationError
from app.infra.db.keyset import position


def test_a_timestamp_key_parses_back_to_the_instant_and_id() -> None:
    row = uuid4()
    at = dt.datetime(2026, 10, 14, 9, 30, 0, 123456, tzinfo=dt.UTC)

    assert position((at.isoformat(), row)) == (at, row)


def test_a_key_that_is_no_timestamp_is_a_client_error() -> None:
    with pytest.raises(ValidationError, match="cursor is not a cursor this endpoint issued"):
        position(("yesterday", uuid4()))
