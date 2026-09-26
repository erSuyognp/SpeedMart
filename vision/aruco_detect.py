"""Tag detection -> per-bay unit sets (9.1).

TagDetector is built once per process: ArUco dictionary from config vision.aruco_dict, sub-pixel corner
refinement, optional CLAHE (config vision.clahe, default true). detect(gray) returns only tags whose id
is a unit in catalog.json; assign_to_bays() then places each tag by its center.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple

import cv2
import numpy as np


class Detection(NamedTuple):
    tag_id: int
    center: tuple[float, float]  # (x, y) in frame pixels, mean of the 4 corners
    corners: np.ndarray  # 4x2 float32, clockwise from the marker's top left


def _dictionary(name: str):
    value = getattr(cv2.aruco, name, None)
    if not isinstance(value, int) or not name.startswith("DICT_"):
        raise ValueError(f"vision.aruco_dict {name!r} is not an OpenCV ArUco dictionary (e.g. DICT_4X4_50)")
    return cv2.aruco.getPredefinedDictionary(value)


class TagDetector:
    def __init__(self, dict_name: str, known_ids: Iterable[int], clahe: bool = True):
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self._detector = cv2.aruco.ArucoDetector(_dictionary(dict_name), params)
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) if clahe else None
        self.known_ids = frozenset(int(i) for i in known_ids)

    def detect(self, gray: np.ndarray) -> list[Detection]:
        """Known unit tags in a grayscale frame, as (tag_id, center_xy, corners)."""
        if self._clahe is not None:
            gray = self._clahe.apply(gray)
        corners, ids, _ = self._detector.detectMarkers(gray)
        if ids is None:
            return []
        found = []
        for quad, tag_id in zip(corners, ids.flatten()):
            tag_id = int(tag_id)
            if tag_id not in self.known_ids:
                continue
            pts = quad.reshape(4, 2)
            cx, cy = pts.mean(axis=0)
            found.append(Detection(tag_id, (float(cx), float(cy)), pts))
        return found


def in_roi(point: tuple[float, float], roi: list[int]) -> bool:
    """Half open [x1, x2) x [y1, y2), so a center on an edge shared by two adjacent bays lands in exactly one."""
    x, y = point
    x1, y1, x2, y2 = roi
    return x1 <= x < x2 and y1 <= y < y2


def assign_to_bays(detections: Iterable[Detection], bays: list[dict]) -> tuple[dict[int, list[int]], list[int]]:
    """({bay_id: sorted tag ids whose center is in that bay's ROI}, sorted loose tag ids outside every ROI).
    Every configured bay appears in the dict, empty if it holds no tags. First matching bay wins."""
    per_bay: dict[int, set[int]] = {int(b["id"]): set() for b in bays}
    loose: set[int] = set()
    for det in detections:
        for bay in bays:
            if in_roi(det.center, bay["roi"]):
                per_bay[int(bay["id"])].add(det.tag_id)
                break
        else:
            loose.add(det.tag_id)
    return {bay_id: sorted(ids) for bay_id, ids in per_bay.items()}, sorted(loose)
