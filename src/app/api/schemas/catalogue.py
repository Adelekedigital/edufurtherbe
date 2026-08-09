"""What the catalogues look like over the wire.

Every model here is deliberately narrower than its table. `institutions` carries
`source`, `status`, `merged_into_id`, `created_by` and `last_synced_at`; none of
them appear below. They are curation state — how a row got here and whether an
admin has looked at it — and a client has no use for any of it. Returning them
would also leak the shape of the review queue to anyone who can call a public
endpoint.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CountryRef(BaseModel):
    """A country as it appears inside another resource."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    display_name: str


class InstitutionRead(BaseModel):
    """A university, as search returns it and as an education entry embeds it.

    **One model for both**, which is the point. Search excludes unreviewed rows;
    reading an education entry does not — but what a client receives is the same
    shape either way, so nothing downstream has to know which path produced it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    web_page: str | None = None
    #: Null for a user-created institution an admin has not completed yet.
    country: CountryRef | None = None

    @classmethod
    def from_row(cls, row: dict[str, object]) -> InstitutionRead:
        """Build from a flat query row.

        The store returns `country_code`/`country_name` flat rather than nested,
        because a join produces columns and not objects. Nesting happens here,
        once, so three tiers of search and the education read cannot each invent
        their own shape.
        """
        code = row.get("country_code")
        name = row.get("country_name")
        return cls(
            id=row["id"],  # type: ignore[arg-type]
            name=row["name"],  # type: ignore[arg-type]
            web_page=row.get("web_page"),  # type: ignore[arg-type]
            country=(
                CountryRef(code=str(code), display_name=str(name))
                if code is not None and name is not None
                else None
            ),
        )


class LookupRead(BaseModel):
    """One row of a lookup catalogue.

    Deliberately loose about which identifier column it carries: `degree_levels`
    and `service_offerings` have a `slug`, `countries` a two-letter `code`,
    `languages` a three-letter `code_639_3`, and `scholarship_programs` a slug
    that may be null. All are "the stable thing code refers to", so they share
    one field rather than five models differing in one key name.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    #: The stable identifier: a slug, or an ISO code. Null only where the
    #: underlying catalogue permits it (`scholarship_programs.slug`).
    code: str | None = None
    #: Present for `service-offerings` only; null elsewhere.
    category: str | None = None
    #: Present for `scholarship-programs` only.
    official_url: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, object]) -> LookupRead:
        """Map whichever identifier column this catalogue uses onto `code`."""
        code = row.get("slug") or row.get("code") or row.get("code_639_3")
        return cls(
            id=row["id"],  # type: ignore[arg-type]
            display_name=row["display_name"],  # type: ignore[arg-type]
            code=str(code) if code is not None else None,
            category=row.get("category"),  # type: ignore[arg-type]
            official_url=row.get("official_url"),  # type: ignore[arg-type]
        )
