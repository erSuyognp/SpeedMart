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


GRAY = (160, 160, 160)
MAGENTA = (255, 0, 255)


class BayStatusLike(Protocol):
    stable: bool
    motion: bool


def _fmt_content(units, yolo) -> str:
    text = str(list(units))
    if yolo:
        text += " yolo " + ",".join(f"{k}:{v}" for k, v in sorted(yolo.items()))
    return text


def bay_state_text(st) -> str:
    """MOTION / settling N ms / hold N/M ms / stable, plus the changed fraction when motion freeze is on."""
    if st.stable:
        state = "stable"
    elif st.motion:
        state = "MOTION"
    elif getattr(st, "settle_left_ms", 0) > 0:
        state = f"settling {st.settle_left_ms} ms"
    else:
        state = f"hold {getattr(st, 'held_ms', 0)}/{getattr(st, 'need_ms', 0)} ms"
    frac = getattr(st, "changed_fraction", None)
    return state if frac is None else f"chg {frac:.3f}  {state}"


def _text(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int], scale: float = 0.6) -> None:
    cv2.putText(img, text, org, FONT, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, scale, color, 2, cv2.LINE_AA)


def draw_yolo_boxes(img: np.ndarray, boxes) -> None:
    """YOLO boxes with class and confidence, drawn in place. Boxes of classes missing from the catalog
    are gray."""
    for box in boxes or ():
        x1, y1, x2, y2 = (int(round(v)) for v in box.xyxy)
        color = MAGENTA if box.sku is not None else GRAY
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        _text(img, f"{box.cls_name} {box.conf:.2f}", (x1 + 2, max(16, y1 - 6)), color, 0.5)


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
    motion=None,
    message: str = "",
    yolo_boxes=None,
    yolo_status: str = "",
) -> np.ndarray:
    """Bay boxes (green stable, yellow unstable) with id, SKU name, reported units, changed fraction,
    MOTION / settling / hold / stable and the pending candidate; the motion area (ROI + margin) in gray;
    tag outlines with ids (loose tags orange); YOLO boxes with class and confidence (magenta) when YOLO
    is on; FPS; motion tuning values and YOLO status; backend status line with the age of the last
    successful post."""
    view = frame.copy()
    h = view.shape[0]
    loose = set(loose)

    w = view.shape[1]
    for bay in bays:
        bay_id = int(bay["id"])
        x1, y1, x2, y2 = (int(v) for v in bay["roi"])
        st = status.get(bay_id)
        color = GREEN if st is None or st.stable else YELLOW
        if motion is not None and bay_id in motion.rois:
            mx1, my1, mx2, my2 = motion.motion_rect(bay_id, w, h)
            cv2.rectangle(view, (mx1, my1), (mx2 - 1, my2 - 1), GRAY, 1)
        cv2.rectangle(view, (x1, y1), (x2, y2), color, 2)
        name = sku_names.get(bay.get("sku"), bay.get("sku", "?"))
        _text(view, f"bay {bay_id} {name}", (x1 + 4, y1 + 22), color)
        reported = per_bay.get(bay_id, []) if st is None or getattr(st, "units", None) is None else list(st.units)
        yolo = None if st is None else getattr(st, "yolo_counts", None)
        _text(view, f"{len(reported)} units {_fmt_content(reported, yolo)}", (x1 + 4, y2 - 58), color, 0.55)
        if st is not None:
            _text(view, bay_state_text(st), (x1 + 4, y2 - 34), color, 0.55)
            pending = getattr(st, "pending_units", None)
            if pending is not None:
                _text(view, f"pending {_fmt_content(pending, getattr(st, 'pending_yolo', None))}",
                      (x1 + 4, y2 - 10), YELLOW, 0.55)

    draw_yolo_boxes(view, yolo_boxes)

    for det in detections:
        color = ORANGE if det.tag_id in loose else CYAN
        pts = det.corners.astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(view, [pts], True, color, 2, cv2.LINE_AA)
        cx, cy = (int(v) for v in det.center)
        _text(view, str(det.tag_id), (cx - 8, cy + 8), color, 0.8)

    fps_text = f"FPS {fps:4.1f}" + ("" if camera_fps is None else f"  (camera {camera_fps:4.1f})")
    _text(view, fps_text, (10, 30), WHITE, 0.8)
    if motion is not None:
        tune = f"motion thr {motion.threshold:g}  settle {int(motion.settle_ms)} ms  margin {motion.margin_px}px"
    else:
        tune = "motion freeze OFF"
    if yolo_status:
        tune += f"  |  {yolo_status}"
    _text(view, tune, (10, 62), WHITE, 0.6)
    if loose:
        _text(view, f"loose {sorted(loose)}", (10, 92), ORANGE, 0.7)
    if message:
        _text(view, message, (10, 122), YELLOW, 0.7)

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
    _text(view, "q quit | space pause | c recalibrate | [ ] motion thr | - = settle | s save", (10, h - 12),
          WHITE, 0.55)
    return view
