"""Acknowledgements several areas share: a created row's id, an edit, a removal.

One model per shape (non-negotiable #8), so the spec names one component for
`{"updated": true}` wherever it is returned rather than one per route.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

__all__ = ["CreatedRead", "DeletedRead", "UpdatedRead"]


class CreatedRead(BaseModel):
    """The id of the row a create (or an upsert) wrote."""

    id: UUID = Field(description="The written row's id.")


class UpdatedRead(BaseModel):
    """An edit was applied. A missing row is a 404, never `false`."""

    updated: bool = Field(description="Always true: the edit was applied.")


class DeletedRead(BaseModel):
    """A row was removed (soft delete). A missing row is a 404, never `false`."""

    deleted: bool = Field(description="Always true: the row was removed.")
