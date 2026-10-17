"""Public catalogue reads: institution search, and the lookup lists.

The lookup lists sit under `/catalog/{name}` rather than at `/{name}`. A bare
`/{name}` under this prefix is a **catch-all**: it matches `/me`, and every
resource added later, with nothing but router registration order deciding who
wins. That is a routing bug waiting for the next endpoint, and the segment costs
one word. `institutions` keeps its own path because it is a real resource that
education entries reference by id, not a reference list.

`/catalog/fields-of-study` is a constant, not a lookup table (decision 262), so
it is its own route, declared **before** `/catalog/{catalogue}`: Starlette tries
routes in order, and the generic one answers any name it does not hold with 404.

**Unauthenticated, deliberately.** These serve public reference data — a list of
the world's universities, ISO countries, ISO languages, and the platform's own
closed vocabularies. There is nothing here to protect, and requiring a token
would break the case that matters most: somebody choosing their school during
signup, before an account exists.

Reading is public; **adding is not**. A school the catalogue does not hold is
created on the authenticated education write, in the same transaction, with
`created_by` set to the caller — so nothing anonymous ever reaches
`institutions`.

The accepted risk, stated rather than discovered: this is the first
unauthenticated endpoint in the service, and it takes a user-supplied string.
The query is bounded by `LIMIT` and every tier is index-backed, but there is no
rate limiting anywhere in this codebase yet.

**`/institutions` takes no `cursor`, deliberately.** ADR 0016 asks for cursor
pagination on every list endpoint; autocomplete narrows by typing rather than by
paging, and nothing would ever call a second page. The `Page` envelope is
returned anyway, so adding one later is not a breaking change — which is the
part of that rule that actually matters.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import InstitutionResultsDep, LookupPageDep
from app.api.schemas.catalogue import FieldOfStudySuggestionRead, InstitutionRead, LookupRead
from app.api.schemas.common import Page, encode_cursor
from app.domain.goals import FIELD_OF_STUDY_MAX_LENGTH, FIELD_OF_STUDY_SUGGESTIONS

router = APIRouter(prefix="/api/v1", tags=["catalog"])

#: One list for every caller, changed only by a deploy, so shared caches may hold
#: it. An hour: a stale suggestion after a deploy restricts nothing.
SUGGESTIONS_CACHE = "public, max-age=3600"

#: Built once: the suggestions are a constant (decision 262).
SUGGESTIONS_PAGE = Page(
    data=[FieldOfStudySuggestionRead(name=name) for name in FIELD_OF_STUDY_SUGGESTIONS]
)

CURSOR_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "The `cursor` was not one this endpoint issued."
    }
}


@router.get(
    "/institutions",
    response_model=Page[InstitutionRead],
    summary="Search institutions",
    description=(
        "Autocomplete over the mirrored university catalogue.\n\n"
        "Matching runs in three tiers, best first: names **starting** with the "
        "term, then names **containing** it, then a fuzzy pass for "
        "misspellings. Each tier only fills what the previous one left, so a "
        "query with plenty of matches never pays for fuzzy matching.\n\n"
        "**Fuzzy matching helps when the term resembles the whole name** — "
        "`Univerity of Lagos` finds `University of Lagos`. It will not rescue a "
        "short misspelled word against a long name: `Oxfrod` returns nothing, "
        "because six letters share too few trigrams with "
        "`Oxford Brookes University`. Correcting the spelling is the fix.\n\n"
        "**Results exclude institutions awaiting review**, and ones merged into "
        "another. A school somebody typed in yesterday is not offered to "
        "everybody else until an admin has looked at it — so its absence here "
        "is the intended behaviour, not a gap in the catalogue.\n\n"
        "An empty `q` returns an empty list rather than an error: an empty "
        "search box is a normal state, not a mistake."
    ),
)
async def search(rows: InstitutionResultsDep) -> Page[InstitutionRead]:
    # No cursor: autocomplete narrows by typing, not by paging. The envelope is
    # still `Page`, so adding one later breaks nothing.
    return Page(data=[InstitutionRead.from_row(row) for row in rows])


@router.get(
    "/catalog/fields-of-study",
    response_model=Page[FieldOfStudySuggestionRead],
    summary="List field-of-study suggestions",
    description=(
        'The suggestions onboarding offers under "What do you want to study?", '
        "returned whole in the order the product intends — not alphabetically. "
        "`next_cursor` is always `null`.\n\n"
        "**Suggestions only: this list never restricts what is saved.** The "
        "mentee's answer is free text on the goal's `field_of_study` (`PUT "
        "/users/{id}/goal`), so a typed answer is stored as typed, up to "
        f"{FIELD_OF_STUDY_MAX_LENGTH} characters. Every suggestion is one that "
        "write accepts unchanged.\n\n"
        "A name and no id, because nothing references a suggestion: save the "
        "`name` itself."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "The suggestions, in order.",
            "headers": {
                "Cache-Control": {
                    "description": f"`{SUGGESTIONS_CACHE}`: the same for everyone.",
                    "schema": {"type": "string"},
                }
            },
        }
    },
)
async def fields_of_study(response: Response) -> Page[FieldOfStudySuggestionRead]:
    response.headers["Cache-Control"] = SUGGESTIONS_CACHE
    return SUGGESTIONS_PAGE


@router.get(
    "/catalog/{catalogue}",
    response_model=Page[LookupRead],
    summary="List a lookup catalogue",
    description=(
        "One of the reference lists a profile form is built from: "
        "`degree-levels`, `service-offerings`, `scholarship-programs`, "
        "`countries`, `languages`.\n\n"
        "`degree-levels` and `service-offerings` are closed vocabularies of a "
        "handful of rows, returned whole in the order the product intends — "
        "not alphabetically.\n\n"
        "`languages` holds **7,078** rows (ISO 639-3, so that languages such as "
        "Nigerian Pidgin are present at all), so it is the one list that really "
        "pages. Use `q` to narrow it rather than fetching every page.\n\n"
        "`scholarship-programs` excludes entries awaiting review, for the same "
        "reason institution search does.\n\n"
        "Field-of-study suggestions are not a lookup: they are "
        "`/catalog/fields-of-study`, with their own shape."
    ),
    responses=CURSOR_RESPONSES,
)
async def lookup(page: LookupPageDep) -> Page[LookupRead]:
    rows, has_more = page
    next_cursor = (
        encode_cursor(str(rows[-1]["display_name"]), rows[-1]["id"]) if has_more and rows else None
    )
    return Page(data=[LookupRead.from_row(row) for row in rows], next_cursor=next_cursor)
