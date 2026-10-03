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
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from app.core.config import MIN_BOOKING_WINDOW_DAYS
from app.core.errors import ValidationError
from app.domain.social_links import MAX_LENGTH, SocialNetwork, canonical_social

#: Anything above this is clamped rather than refused. A client asking for 5,000
#: rows has made a mistake, and a 422 in the middle of an autocomplete is a worse
#: answer than the 50 it should have asked for.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 10

#: Lookup lists serve select boxes rather than autocomplete, and `countries` is
#: 249 rows a client wants in one call — so their page is larger than a search's.
LOOKUP_PAGE_SIZE = 300


class AvatarFocusRead(BaseModel):
    """Where to centre an avatar: the main face, as 0..1 fractions of the image.

    For CSS, `object-position: {x * 100}% {y * 100}%`. `null` on the parent
    when no face was found or the photo has not been processed yet — fall back
    to the client's default crop (settled decision #180).

    **Sent wherever `avatar_url` is**: the discovery card, the featured mentor,
    the public profile, a session's party cards and `/me`. A photo framed well
    in one place and badly in another is the same problem, moved.
    """

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)

    @classmethod
    def of(cls, x: object, y: object) -> AvatarFocusRead | None:
        """From the two stored columns; `None` unless both are set."""
        if x is None or y is None:
            return None
        return cls(x=float(str(x)), y=float(str(y)))


class AvatarFocusWrite(AvatarFocusRead):
    """A mentor's own choice of where to centre their photo — the read's shape.

    The same bounds as the read, because it *is* the read: a point the client
    shows is a point it may send back. Stored to the column's precision
    (`domain.avatar_focus.PLACES`), which is what the read returns.
    """

    model_config = ConfigDict(extra="forbid")


class SessionTypeRefRead(BaseModel):
    """Which of a mentor's offerings something was about: `{id, name}`.

    One shape for every reader that names an offering beside something else —
    a review's topic (#185) and a session's heading. The name is the offering's
    **current** name, read whatever its state: retiring an offering does not
    change what an old session was about.
    """

    id: UUID
    name: str = Field(description="The offering's name as it is now.")

    @classmethod
    def of(cls, type_id: object, name: object) -> Self | None:
        """From the joined pair; `None` when the row names no offering."""
        if type_id is None:
            return None
        return cls(id=UUID(str(type_id)), name=str(name))


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


def encode_cursor(sort_key: str, row_id: UUID) -> str:
    """The keyset position, as one opaque token.

    Base64 of the two ordering columns. Opaque is not security — anyone can
    decode it — it is a contract: a client that cannot read the cursor cannot
    build one, so the encoding stays ours to change.

    **``sort_key`` rather than ``display_name``.** ADR 0016's amendment makes
    this general: the id alone is the cursor when display order *is* id order,
    and otherwise the cursor is the sort column plus the id. The catalogues sort
    by name and sessions sort by ``starts_at``, so the parameter is whatever
    column the list is ordered on — rendered as a string, because the token is
    text either way. Naming it for the first caller would have made the second
    one pass a timestamp to something called a display name.
    """
    raw = f"{sort_key}\x00{row_id}".encode()
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
        sort_key, _, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("\x00")
        return sort_key, UUID(row_id)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc


def encode_browse_cursor(taking_bookings: bool, row_id: UUID) -> str:
    """The Explore browse position: the group, then the id (#220).

    Browse orders bookable mentors first, so the group is the leading sort
    column and ADR 0016's two-part form applies — a cursor on the id alone
    would compare across the boundary and skip or repeat mentors.
    """
    return encode_cursor("1" if taking_bookings else "0", row_id)


def decode_browse_cursor(cursor: str | None) -> tuple[bool, UUID] | None:
    """A browse cursor back into `(taking_bookings, id)`, or a `422`.

    A cursor minted before the group was in it (the id alone) is refused, not
    guessed at: the frontend restarts from page 1 on a `422`, which is agreed.
    """
    decoded = decode_cursor(cursor)
    if decoded is None:
        return None
    group, row_id = decoded
    if group not in {"0", "1"}:
        raise ValidationError("cursor is not a cursor this endpoint issued")
    return group == "1", row_id


#: Marks a soonest-first session cursor. **Only `asc` is tagged**: newest first
#: is the default and predates `order`, so its cursors stay exactly as clients
#: already hold them. A timestamp never starts with this letter.
ASCENDING_CURSOR_TAG = "a"


def encode_session_cursor(starts_at: str, row_id: UUID, *, ascending: bool) -> str:
    """A session-list position that remembers which way it was going."""
    return encode_cursor(f"{ASCENDING_CURSOR_TAG if ascending else ''}{starts_at}", row_id)


def decode_session_cursor(cursor: str | None, *, ascending: bool) -> tuple[str, UUID] | None:
    """A session cursor back into `(starts_at, id)`, or a `422` when it was minted
    for the other direction — a page from the wrong place looks like working
    software and loses rows."""
    decoded = decode_cursor(cursor)
    if decoded is None:
        return None
    sort_key, row_id = decoded
    if sort_key.startswith(ASCENDING_CURSOR_TAG) != ascending:
        raise ValidationError("cursor was issued for the other order")
    return sort_key.removeprefix(ASCENDING_CURSOR_TAG), row_id


#: How deep a search may be paged. Elasticsearch refuses past 10,000 results by
#: default and Google stops near 1,000: the systems built for search cap depth
#: rather than solve it, because relevance is unstable and nobody reads page 40.
#: Here it also stops a public endpoint being asked to count past a million rows.
MAX_SEARCH_OFFSET = 500


def encode_offset_cursor(offset: int) -> str:
    """The position in a ranked result set.

    **A browse cursor replayed here is already refused**, and it needed no kind
    tag to do it: this decodes to an integer and an id cursor holds a UUID, so
    `int()` rejects it. A tag was written first on the belief that it was what
    produced the 422 — a mutation removing it survived, which is how the claim was
    found to be false. Removed rather than kept untestable, following the
    redundant guard deleted from `list_session_events` for the same reason. The
    two tests asserting cross-kind refusal stay: they assert the behaviour, which
    is what matters, and would hold under either mechanism.

    Offset rather than a keyset because a rank is not in the row and is not
    stable — "best match" grows to include quality signals that do not exist yet,
    and a token encoding today's ranking is invalidated the day the formula
    changes. An offset is indifferent to what the ordering is.
    """
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _within_cap(offset: int) -> bool:
    """Whether a position is one this endpoint will serve.

    **One predicate, because both directions ask the same question.** The decoder
    refused past the cap from the first commit and the minting side did not ask at
    all, so the last page of a deep search handed out a token the very next
    request rejected — a client following `next_cursor` exactly as the envelope
    documents ended on a 422. Two comparisons against one constant is how that
    happens; the constant being shared is not enough (#8).

    Inclusive at the top: `MAX_SEARCH_OFFSET` is a position that *is* served, not
    the first one refused. `<` here silently drops the last page and looks like
    the fix.
    """
    return 0 <= offset <= MAX_SEARCH_OFFSET


#: Marks a goal-ranked `/mentors` cursor. **A kind tag, where search and browse
#: cursors need none**, because this one must be told apart from a *search*
#: cursor — both are offsets. A search cursor sent without its `q` stays a 422
#: (a client that dropped its query), while a goal cursor must keep paging
#: after the viewer changes: a token that lapses between pages, or a last goal
#: removed in another tab.
GOAL_CURSOR_TAG = "g"


def _raw(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc


def is_goal_cursor(cursor: str) -> bool:
    """Whether a token is a goal-ranked cursor. Malformed tokens are not — they
    fall through to the decoder that refuses them."""
    try:
        return _raw(cursor).startswith(GOAL_CURSOR_TAG)
    except ValidationError:
        return False


def next_goal_cursor(offset: int) -> str | None:
    """The next goal-ranked page's token, or `None` past the same depth cap."""
    if not _within_cap(offset):
        return None
    return base64.urlsafe_b64encode(f"{GOAL_CURSOR_TAG}{offset}".encode()).decode()


def decode_goal_cursor(cursor: str) -> int:
    """A goal cursor back into a position — the offset decoder's rules exactly,
    by handing it the untagged offset."""
    raw = _raw(cursor)
    if not raw.startswith(GOAL_CURSOR_TAG):
        raise ValidationError("cursor is not a cursor this endpoint issued")
    untagged = base64.urlsafe_b64encode(raw[len(GOAL_CURSOR_TAG) :].encode()).decode()
    return decode_offset_cursor(untagged)


def next_offset_cursor(offset: int) -> str | None:
    """The token for the next page of a search, or `None` because the cap ends it.

    `None` is not an error here — past the cap there genuinely is no next page, so
    the envelope says so in the one field clients already check. Kept apart from
    `encode_offset_cursor`, which stays a pure codec: a test fabricating an
    out-of-range token must not be able to get one from the function the endpoint
    uses, or it proves the decoder against input the encoder can no longer produce.
    """
    return encode_offset_cursor(offset) if _within_cap(offset) else None


def decode_offset_cursor(cursor: str | None) -> int:
    """A search cursor back into a position, or a refusal.

    Refuses an id cursor, a malformed token, a negative offset, and anything past
    `MAX_SEARCH_OFFSET`. All four are client errors and all four are better as a
    422 than as a page that looks right.
    """
    if cursor is None:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("cursor is not a cursor this endpoint issued") from exc
    if not _within_cap(offset):
        raise ValidationError(f"a search may not be paged past {MAX_SEARCH_OFFSET} results")
    return offset


def clamp_limit(limit: int | None) -> int:
    """The requested page size, bounded.

    One function rather than a `min()` at each call site: three list endpoints
    with three copies of the bound is three chances for one of them to be wrong,
    and the wrong one is a query nobody notices until it is slow.
    """
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


def storable(value: str) -> str:
    """The same text, minus what this database cannot be given.

    **Two causes, and they fail in different places** — which is why the first
    version of this function was wrong and shipped saying so. A ``str`` Python
    accepts is not the same set as a ``text`` PostgreSQL accepts, and the gap has
    two halves:

    - **``U+0000``** encodes to UTF-8 perfectly well and PostgreSQL then refuses
      the value: ``CharacterNotInRepertoireError``, raised by the server.
    - **An unpaired surrogate** (``U+D800`` to ``U+DFFF``) never reaches the server
      at all. UTF-8 has no encoding for one, so asyncpg raises
      ``UnicodeEncodeError`` while building the message. JSON can carry one —
      ``"\\ud800"`` is well-formed — so a request body is a live route to it.

    Both are **a 500 with a stack trace, on endpoints that take no token at all**,
    and nothing in the gate sees either: the annotation is ``str``, the value is
    bound rather than interpolated, the SQL is correct, and both are legal Python.
    Found by probe, and the second only because the first version of this
    docstring asserted it did not exist — the claim was tested rather than
    believed, and it was false.

    So the rule is not a list of characters. It is **whatever cannot be stored**,
    which the encoder itself defines: encode with the database's own encoding and
    drop what it cannot represent, then drop the one thing it can represent and
    the server still rejects. Every other control character survives, including
    the C0 range, non-characters and astral-plane emoji — all probed, all stored.

    Removed rather than refused, which is this file's existing answer. `Normalised`
    already rewrites what it is given — trims, and turns ``""`` into ``None`` —
    and a base class that quietly repairs whitespace while hard-failing a control
    byte would be two rules wearing one name. No client that means anything sends
    either of these: they come from a scanner, or from something already broken
    upstream.
    """
    return value.encode("utf-8", "ignore").decode("utf-8").replace("\x00", "")


#: The same rule for a query parameter, which reaches no model to be normalised
#: by. Declared as a type rather than called in each dependency, so a fourth text
#: parameter inherits it instead of remembering it — the three that exist were
#: each reachable, and the fourth is the one that would ship.
StorableText = AfterValidator(storable)


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
                # Before the trim, not after: a value that is *only* a NUL must
                # end up `None` like any other emptied field, rather than a
                # one-character string that passes every length check.
                stripped = storable(value).strip()
                normalised[key] = stripped or None
            else:
                normalised[key] = value
        return normalised


def _published_link(network: SocialNetwork) -> Any:
    def convert(value: object) -> str | None:
        return canonical_social(network, value) if isinstance(value, str) else None

    return convert


_REFUSAL = {
    SocialNetwork.LINKEDIN: "not a LinkedIn profile: give a handle or a linkedin.com/in/ link",
    SocialNetwork.X: "not an X profile: give a handle or an x.com link",
    SocialNetwork.YOUTUBE: "not a YouTube channel: give a @handle or a youtube.com link",
}


def _written_link(network: SocialNetwork) -> Any:
    def convert(value: str | None) -> str | None:
        if value is None:
            return None
        link = canonical_social(network, value)
        if link is None:
            raise ValueError(_REFUSAL[network])
        return link

    return convert


#: A social link as written: a handle or a link on that network, **stored in
#: its canonical `https://` form** and refused (`422`) otherwise (#182).
LinkedInWrite = Annotated[
    str | None, Field(max_length=MAX_LENGTH), AfterValidator(_written_link(SocialNetwork.LINKEDIN))
]
XWrite = Annotated[
    str | None, Field(max_length=MAX_LENGTH), AfterValidator(_written_link(SocialNetwork.X))
]
YouTubeWrite = Annotated[
    str | None, Field(max_length=MAX_LENGTH), AfterValidator(_written_link(SocialNetwork.YOUTUBE))
]

#: A social link as published: **always canonical or `null`**. The same rule
#: the write applies, run again on the way out, so a legacy value stored before
#: the rule existed is never published in a shape the client must parse.
LinkedInRead = Annotated[str | None, BeforeValidator(_published_link(SocialNetwork.LINKEDIN))]
XRead = Annotated[str | None, BeforeValidator(_published_link(SocialNetwork.X))]
YouTubeRead = Annotated[str | None, BeforeValidator(_published_link(SocialNetwork.YOUTUBE))]


def publish_window_minimum(schema: dict[str, Any]) -> None:
    """Publish `MIN_BOOKING_WINDOW_DAYS` as a booking window's minimum (#311).

    Validation keeps `ge=1`, so a window stored before the minimum existed can
    be resent unchanged; the write dependency enforces the minimum on anything
    new. The published contract is the minimum a client should offer. Set on
    the integer branch, where an optional field's bound lives.
    """
    for branch in schema.get("anyOf", [schema]):
        if branch.get("type") == "integer":
            branch["minimum"] = MIN_BOOKING_WINDOW_DAYS
