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

import logging
from pathlib import Path

import cv2
import numpy as np

from app.domain.avatar_focus import Face, focal_point

__all__ = ["avatar_focus", "detect_faces"]

logger = logging.getLogger(__name__)

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


def detect_faces(payload: bytes) -> tuple[list[Face], int, int]:
    """Every face in an encoded image, and the image's width and height.

    An image OpenCV cannot decode yields no faces rather than an error: the
    upload has already been validated and re-encoded by the time this runs, and
    a focal point is an enhancement, never a reason to refuse a photo.
    """
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return [], 0, 0
    height, width = image.shape[:2]
    detector = cv2.FaceDetectorYN.create(
        str(MODEL), "", (width, height), SCORE_THRESHOLD, NMS_THRESHOLD, TOP_K
    )
    _, found = detector.detect(image)
    faces = [] if found is None else [Face(*map(float, row[:4])) for row in found]
    return faces, width, height


def avatar_focus(payload: bytes) -> tuple[float, float] | None:
    """The focal point of an encoded avatar, or `None` when there is no face.

    **Never raises.** A focal point is an enhancement: a detector failure must
    not refuse a photo that has already been validated and stored. It is logged
    and the card falls back to its default crop.
    """
    try:
        faces, width, height = detect_faces(payload)
    except cv2.error:
        logger.warning("face detection failed; the avatar keeps no focal point")
        return None
    return focal_point(faces, width=width, height=height)
