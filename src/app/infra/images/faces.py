"""Finding faces in an image: OpenCV's YuNet detector, run locally.

**Local, CPU, and no third-party API**: a mentor's photo never leaves the
server to be analysed. YuNet is a small neural detector (the ~230 KB model
beside this file, MIT, `LICENSE-yunet.md`) shipped for OpenCV's `FaceDetectorYN`.
Chosen over MediaPipe, which depends on the full OpenCV build plus audio and
plotting libraries a server never uses (settled decision #180).

Returns boxes only — `domain/avatar_focus.py` decides which one matters and
turns it into a focal point. This module is the vendor boundary: nothing else
imports `cv2`.

**CPU-bound, so callers run it on a worker thread**, as they already do for the
image re-encode it follows.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.domain.avatar_focus import Face, focal_point

__all__ = ["FaceDetectionError", "avatar_focus", "detect_faces"]

MODEL = Path(__file__).with_name("face_detection_yunet_2023mar.onnx")

#: How sure the detector must be. OpenCV's own sample default; lower finds
#: shadows and patterns, higher misses faces in side light.
SCORE_THRESHOLD = 0.8
NMS_THRESHOLD = 0.3
TOP_K = 50

# OpenCV 5's new graph engine logs a warning for every detector it builds
# ("Targets are not supported..."). It is informational and would otherwise
# land in the server log once per upload.
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)


class FaceDetectionError(Exception):
    """The image could not be analysed — distinct from "analysed, no face".

    The difference is stored: "no face" is recorded so nothing looks again,
    while a failure is left unrecorded so a later run does.
    """


_local = threading.local()


def _detector(width: int, height: int) -> Any:
    """This thread's detector, sized for the image.

    Loading the model and building its graph is the expensive part, so each
    worker thread builds one and only re-sizes it per image. One per thread,
    not one per process: a detector object is not safe to share across threads.
    """
    detector = getattr(_local, "detector", None)
    if detector is None:
        detector = cv2.FaceDetectorYN.create(
            str(MODEL), "", (width, height), SCORE_THRESHOLD, NMS_THRESHOLD, TOP_K
        )
        _local.detector = detector
    else:
        detector.setInputSize((width, height))
    return detector


def detect_faces(payload: bytes) -> tuple[list[Face], int, int]:
    """Every face in an encoded image, and the image's width and height.

    Raises `FaceDetectionError` for an image OpenCV cannot decode: that is not an
    image with no face in it, and must not be recorded as one.
    """
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise FaceDetectionError("the image could not be decoded")
    height, width = image.shape[:2]
    _, found = _detector(width, height).detect(image)
    faces = [] if found is None else [Face(*map(float, row[:4])) for row in found]
    return faces, width, height


def avatar_focus(payload: bytes) -> tuple[float, float] | None:
    """The focal point of an encoded avatar, or `None` when it has no face.

    Raises `FaceDetectionError` — and only that — when the image could not be
    analysed at all, whatever the underlying cause, so every caller handles one
    exception and a detector bug never escapes as a 500.
    """
    try:
        faces, width, height = detect_faces(payload)
    except FaceDetectionError:
        raise
    except Exception as exc:  # best-effort by contract; see the class docstring
        raise FaceDetectionError(f"face detection failed: {type(exc).__name__}") from exc
    return focal_point(faces, width=width, height=height)
