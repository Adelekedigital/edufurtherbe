"""Where a card should centre an avatar: the middle of the main face.

Cards crop avatars into fixed frames, and one CSS position cannot serve both a
tight selfie and a half-body shot — tested against real mentor photos by the
frontend and the owner. So the backend says where the face is, as fractions of
the stored image, and the client centres on it.

**The largest face is the main one.** A mentor's photo is of the mentor;
anyone else in it — a group shot, a poster behind them — is smaller.

**Fractions, not pixels.** The client scales the image to its frame, so a
pixel position would mean nothing there; a fraction survives any resize.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["CHOSEN", "DETECTED", "Face", "focal_point"]

#: Where a focus came from. A mentor's own choice outranks a detection, and a
#: backfill never overwrites it.
DETECTED = "detected"
CHOSEN = "chosen"

#: Stored to this many places: a thousandth of the image is finer than any crop.
PLACES = 3


@dataclass(frozen=True, slots=True)
class Face:
    """One detected face, as a box in pixels of the image it was found in."""

    x: float
    y: float
    width: float
    height: float


def focal_point(faces: Iterable[Face], *, width: int, height: int) -> tuple[float, float] | None:
    """The centre of the largest face as `(x, y)` fractions, or `None` without one.

    Clamped to the image: a detector may report a box that runs past an edge
    when a face is partly out of frame, and a fraction outside 0..1 would push
    a client's crop off the picture.
    """
    largest = max(faces, key=lambda face: face.width * face.height, default=None)
    if largest is None or width <= 0 or height <= 0:
        return None

    def fraction(centre: float, extent: int) -> float:
        return round(min(1.0, max(0.0, centre / extent)), PLACES)

    return (
        fraction(largest.x + largest.width / 2, width),
        fraction(largest.y + largest.height / 2, height),
    )
