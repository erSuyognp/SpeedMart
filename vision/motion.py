"""Per-bay motion detection (9.2, F3).

For each bay keep the previous blurred grayscale crop and compare it with the current one:

    diff = absdiff(roi_now, roi_prev)
    changed_fraction = count(diff > 25) / roi_area
    motion = changed_fraction > vision.motion_threshold
    last_motion_ts[bay] = now if motion

A bay is motion free when now - last_motion_ts[bay] >= vision.motion_settle_ms.

The crop is the bay ROI grown by vision.motion_margin_px on every side (clamped to the frame), so a hand
or arm reaching in from the side freezes the bay a moment before it covers a tag. The margin is used for
motion only; tag assignment still uses the plain ROI.

Times are seconds from any monotonic clock (the worker passes time.monotonic(); tests pass a fake one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

PIXEL_DIFF = 25  # gray levels; a pixel counts as changed when it moved by more than this
BLUR_KSIZE = (5, 5)
DEFAULT_THRESHOLD = 0.02
DEFAULT_SETTLE_MS = 300
DEFAULT_MARGIN_PX = 40
TIME_EPS_MS = 1e-3  # float slack so exactly settle_ms counts as settled


@dataclass(frozen=True)
class MotionState:
    changed_fraction: float
    motion: bool  # motion in this frame
    motion_free: bool  # no motion for at least settle_ms
    settle_left_ms: int  # ms until motion free (0 when motion free)


def expand_roi(roi: Iterable[int], margin: int, width: int, height: int) -> tuple[int, int, int, int]:
    """[x1, y1, x2, y2] grown by margin on every side and clamped to a width x height frame."""
    x1, y1, x2, y2 = (int(v) for v in roi)
    return (max(0, x1 - margin), max(0, y1 - margin), min(width, x2 + margin), min(height, y2 + margin))


class MotionDetector:
    """Keeps one previous crop per bay. threshold and settle_ms are plain attributes so the worker can
    tune them live."""

    def __init__(self, bays: list[dict], threshold: float = DEFAULT_THRESHOLD,
                 settle_ms: float = DEFAULT_SETTLE_MS, margin_px: int = DEFAULT_MARGIN_PX):
        self.rois = {int(b["id"]): [int(v) for v in b["roi"]] for b in bays}
        self.threshold = float(threshold)
        self.settle_ms = float(settle_ms)
        self.margin_px = int(margin_px)
        self._prev: dict[int, np.ndarray] = {}
        self._last_motion: dict[int, float] = {}
        self._frame_shape: tuple[int, ...] | None = None

    @classmethod
    def from_config(cls, config: dict) -> MotionDetector:
        vision = config.get("vision", {})
        return cls(
            config["bays"],
            threshold=vision.get("motion_threshold", DEFAULT_THRESHOLD),
            settle_ms=vision.get("motion_settle_ms", DEFAULT_SETTLE_MS),
            margin_px=vision.get("motion_margin_px", DEFAULT_MARGIN_PX),
        )

    def motion_rect(self, bay_id: int, width: int, height: int) -> tuple[int, int, int, int]:
        return expand_roi(self.rois[bay_id], self.margin_px, width, height)

    def update(self, gray: np.ndarray, now: float) -> dict[int, MotionState]:
        """Motion state of every bay for this grayscale frame taken at `now` (seconds)."""
        if gray.ndim != 2:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if gray.shape != self._frame_shape:
            self._prev.clear()  # a resized frame is not comparable with the old crops
            self._frame_shape = gray.shape
        height, width = gray.shape
        blurred = cv2.GaussianBlur(gray, BLUR_KSIZE, 0)

        out: dict[int, MotionState] = {}
        for bay_id in self.rois:
            x1, y1, x2, y2 = self.motion_rect(bay_id, width, height)
            crop = blurred[y1:y2, x1:x2]
            prev = self._prev.get(bay_id)
            self._prev[bay_id] = crop.copy()
            if prev is None or prev.shape != crop.shape or crop.size == 0:
                fraction = 0.0
            else:
                diff = cv2.absdiff(crop, prev)
                fraction = float(np.count_nonzero(diff > PIXEL_DIFF)) / float(crop.size)
            motion = fraction > self.threshold
            if motion:
                self._last_motion[bay_id] = now
            last = self._last_motion.get(bay_id)
            since_ms = float("inf") if last is None else (now - last) * 1000.0 + TIME_EPS_MS
            free = since_ms >= self.settle_ms
            out[bay_id] = MotionState(
                changed_fraction=fraction,
                motion=motion,
                motion_free=free,
                settle_left_ms=0 if free else max(1, int(round(self.settle_ms - since_ms))),
            )
        return out
