"""The legacy snapshot, as the rest of the system is allowed to see it.

**The transform must never know which source a record came from.** Bubble's Data
API and its Data-tab export disagree on six things, and every one of them would
otherwise leak into the mapping code as a branch:

| | Export | Data API | Canonical |
|---|---|---|---|
| identifier | ``unique id`` | ``_id`` | ``bubble_id`` |
| email | flat field | nested in ``authentication`` | ``email`` |
| list of X | ``"a , b"`` | ``["a", "b"]`` | ``list[str]`` |
| empty value | ``""`` | key absent entirely | absent |
| timestamps | ``Sep 3, 2025 5:31 am`` | ``2025-09-03T05:31:00.000Z`` | aware UTC |
| created key | ``Creation Date`` | ``Created Date`` | ``created_at`` |

Each adapter absorbs its own quirks and emits the shape in the right-hand
column. Nothing downstream branches on provenance.

**Field names are otherwise identical between the two**, including the hostile
ones — ``'Admin \N{TOP HAT}'``, ``'\N{BUSTS IN SILHOUETTE}Role'``,
``'registration completed '`` with its trailing space. That was verified against
real responses rather than assumed, and it is why there is no key-translation
table here: only the six rows above differ.

The sixth arrived late and is worth the note. The first five were derived by
reading both formats; ``Creation Date`` versus ``Created Date`` was found only
by dry-running the loader against the real export, which refused **all 43
records** for a missing timestamp. Reading two documents side by side is how the
first five were found and is exactly what missed the sixth.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any, Protocol

# Dropped before a record is written anywhere.
#
# These are **expired Cal.com managed-user tokens**, not live Google credentials
# — decoded from the dev extract, both access tokens are over 400 days past
# expiry and both refresh tokens over 50. Three repository records claimed
# otherwise, inferring from the field names; ADR 0007 carries the correction.
#
# They are dropped anyway. It costs this tuple, the snapshot files stay boring,
# and "expired" is a statement about a third party honouring its own expiry
# rather than something we can verify.
#
# ``composioAuthId`` is deliberately absent: it is a reference to a credential
# Composio holds, not a credential, and the field mapping migrates it.
REDACTED_FIELDS: tuple[str, ...] = (
    "calAccessToken",
    "calAccessTokenExpiresAt",
    "calRefreshToken",
    "calRefreshTokenExpiresAt",
)


class BubbleTimestampError(ValueError):
    """A timestamp could not be read without guessing."""


def parse_timestamp(value: str, *, assume: tzinfo | None = None) -> datetime:
    """Parse a Bubble timestamp into an aware UTC ``datetime``.

    One function for both sources, because how a time is *handled* should not
    depend on where it was read from. The formats differ; the meaning does not.

    The API emits ISO-8601 with a ``Z``, which is unambiguous and needs nothing.
    The export emits ``Sep 3, 2025 5:31 am`` — **no offset at all**, rendered in
    the Bubble application's timezone. That is missing data rather than a parsing
    problem, and no amount of cleverness recovers it.

    So ``assume`` must be supplied for that format, and its absence raises rather
    than defaulting to UTC. Defaulting is the one genuinely dangerous option: the
    dev app runs in ``America/New_York``, so a silent UTC assumption would shift
    every migrated ``created_at`` by four or five hours — plausible-looking,
    wrong, and invisible to every row-count and null-rate check.
    """
    text = value.strip()
    if not text:
        raise BubbleTimestampError("empty timestamp")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = _parse_export_format(text)

    if parsed.tzinfo is None:
        if assume is None:
            raise BubbleTimestampError(
                f"{value!r} carries no UTC offset and no timezone was supplied. "
                "The Bubble export renders in the application's timezone; pass "
                "`assume` explicitly rather than defaulting to UTC."
            )
        parsed = parsed.replace(tzinfo=assume)

    return parsed.astimezone(UTC)


# `%I` is hour 1-12 and `%p` is AM/PM; Bubble renders lowercase ("5:31 am"),
# which strptime does not match, so the text is upper-cased first. The day is
# not zero-padded either, and `%d` accepts both.
_EXPORT_FORMAT = "%b %d, %Y %I:%M %p"


def _parse_export_format(text: str) -> datetime:
    try:
        # Upper-cased because Bubble renders "5:31 am" and %p expects "AM".
        return datetime.strptime(text.upper(), _EXPORT_FORMAT)
    except ValueError as exc:
        raise BubbleTimestampError(f"unrecognised Bubble timestamp: {text!r}") from exc


def redact(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the record without any field in ``REDACTED_FIELDS``.

    A new mapping rather than a mutation, so a caller holding the original cannot
    accidentally write the unredacted version — the mistake this exists to make
    impossible is "redacted, then wrote the wrong variable".
    """
    return {key: value for key, value in record.items() if key not in REDACTED_FIELDS}


def normalise_list(value: Any) -> list[str]:
    """Coerce a Bubble "list of X" to a list of ids, from either source.

    The API returns a real JSON array. The export flattens the same field to
    ``"id1 , id2"`` — comma-joined, with irregular spacing around the comma that
    a naive ``split(",")`` leaves attached to the ids.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def blank_to_none(value: Any) -> Any:
    """``""`` becomes ``None``.

    Only the export needs this — the API omits empty fields entirely — but it is
    applied on both paths so the canonical record has exactly one representation
    of "absent". Otherwise ``about_me`` arrives as an empty string from one source
    and missing from the other, and every ``IS NULL`` check and null-rate
    reconciliation quietly means something different depending on provenance.
    """
    return None if isinstance(value, str) and not value.strip() else value


class BubbleSource(Protocol):
    """Where legacy records come from.

    The port ADR 0002 requires, and it names no transport: an implementation may
    read a file, call the Data API, or replay a fixture. ``domain`` states the
    need; ``infra`` satisfies it.
    """

    def read(self, thing: str) -> Iterable[dict[str, Any]]:
        """Yield every record of one Bubble Thing, already canonical and redacted."""
        ...
