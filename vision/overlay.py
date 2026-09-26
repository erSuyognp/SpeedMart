"""Debug window drawing (9.5). Everything is drawn on a copy; the input frame is never modified."""

from __future__ import annotations

from typing import Iterable, Mapping, Protocol

import cv2
import numpy as np

from vision.aruco_detect import Detection

# BGR
GREEN = (0, 200, 0)
YELLOW = (0, 220, 255)
ORANGE = (0, 140, 255)
RED = (0, 0, 255)
CYAN = (255, 255, 0)
WHITE = (255, 255, 255)

FONT = cv2.FONT_HERSHEY_SIMPLEX


class BayStatusLike(Protocol):
    stable: bool
    motion: bool


def _text(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int], scale: float = 0.6) -> None:
    cv2.putText(img, text, org, FONT, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, scale, color, 2, cv2.LINE_AA)


def draw_overlay(
    frame: np.ndarray,
    *,
    bays: list[dict],
    sku_names: Mapping[str, str],
    detections: Iterable[Detection],
    per_bay: Mapping[int, list[int]],
    loose: Iterable[int],
    status: Mapping[int, BayStatusLike],
    fps: float,
    camera_fps: float | None = None,
    backend_ok: bool | None,
    last_post_age_ms: int | None,
    paused: bool,
) -> np.ndarray:
    """Bay boxes (green stable, yellow unstable) with id, SKU name and unit count; tag outlines with ids
    (loose tags orange); FPS; backend status line with the age of the last successful post."""
    view = frame.copy()
    h = view.shape[0]
    loose = set(loose)

    for bay in bays:
        bay_id = int(bay["id"])
        x1, y1, x2, y2 = (int(v) for v in bay["roi"])
        st = status.get(bay_id)
        color = GREEN if st is None or st.stable else YELLOW
        cv2.rectangle(view, (x1, y1), (x2, y2), color, 2)
        name = sku_names.get(bay.get("sku"), bay.get("sku", "?"))
        _text(view, f"bay {bay_id} {name}", (x1 + 4, y1 + 22), color)
        units = per_bay.get(bay_id, [])
        state = "" if st is None or st.stable else ("  MOTION" if st.motion else "  settling")
        _text(view, f"{len(units)} units {units}{state}", (x1 + 4, y2 - 10), color, 0.55)

    for det in detections:
        color = ORANGE if det.tag_id in loose else CYAN
        pts = det.corners.astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(view, [pts], True, color, 2, cv2.LINE_AA)
        cx, cy = (int(v) for v in det.center)
        _text(view, str(det.tag_id), (cx - 8, cy + 8), color, 0.8)

    fps_text = f"FPS {fps:4.1f}" + ("" if camera_fps is None else f"  (camera {camera_fps:4.1f})")
    _text(view, fps_text, (10, 30), WHITE, 0.8)
    if loose:
        _text(view, f"loose {sorted(loose)}", (10, 62), ORANGE, 0.7)

    age = "never" if last_post_age_ms is None else f"{last_post_age_ms} ms ago"
    if backend_ok:
        line, color = f"backend OK  last post {age}", GREEN
    elif backend_ok is None:
        line, color = "backend: no post yet", WHITE
    else:
        line, color = f"backend unreachable  last OK post {age}", RED
    if paused:
        line, color = f"PAUSED (space to resume)  {line}", YELLOW
    _text(view, line, (10, h - 40), color, 0.7)
    _text(view, "q quit | space pause posting | c recalibrate hint", (10, h - 12), WHITE, 0.55)
    return view
