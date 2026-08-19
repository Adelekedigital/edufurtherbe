"""Every tag on a route is described, and every description is used.

**This is the check that was missing, and its absence is why three tags shipped
undescribed.** `public`, `availability` and `sessions` were each added with a
router and never added to `OPENAPI_TAGS`, because nothing anywhere compared the
two lists — the spec still rendered, the endpoints still worked, and the only
symptom was an empty heading in the documentation nobody generates in CI.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.main import create_app


def spec() -> dict[str, Any]:
    return dict(create_app(Settings(_env_file=None)).openapi())


def used_tags(document: dict[str, Any]) -> set[str]:
    return {
        tag
        for path in document["paths"].values()
        for operation in path.values()
        for tag in operation.get("tags", ())
    }


def described_tags(document: dict[str, Any]) -> set[str]:
    return {tag["name"] for tag in document.get("tags", ())}


def test_every_tag_on_a_route_is_described() -> None:
    """A tag with no description renders as a bare heading with the endpoints
    beneath it and nothing saying what they have in common — which is precisely
    where a reader looks first when the grouping is the only structure the page
    has."""
    document = spec()

    assert used_tags(document) - described_tags(document) == set()


def test_every_description_belongs_to_a_tag_in_use() -> None:
    """The other direction, and the one that rots quietly. A description left
    behind by a rename or a removed router documents a section of the API that
    no longer exists, which is worse than an undescribed one: it is confidently
    wrong rather than merely thin."""
    document = spec()

    assert described_tags(document) - used_tags(document) == set()


def test_a_mentors_own_offerings_are_their_own_tag() -> None:
    """**Settled decision #64: everything outside the catalogue takes its domain
    name.** These lived under `users` while the surface was one list, and the
    module said at the time that a tag "may still earn its place once `DELETE`
    joins them". It has, along with `POST`, `PATCH` and the four intake
    endpoints — eight, against `users`' own handful.
    """
    document = spec()

    assert "session-types" in used_tags(document)
    offerings = [
        path
        for path, operations in document["paths"].items()
        if any("session-types" in op.get("tags", ()) for op in operations.values())
    ]
    assert all(path.startswith("/api/v1/me/session-types") for path in offerings), offerings
