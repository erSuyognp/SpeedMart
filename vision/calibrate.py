"""Click to draw bay ROIs, saves into config.json.

    python -m vision.calibrate

Drag one rectangle per bay, in order bay 0, 1, 2 ... (one per entry in config.json "bays").
Keys: r = redo all, u = undo last, s = save, q / Esc = quit without saving.

Saving rewrites only bays[].roi as [x1, y1, x2, y2] integers in config pixels; every other key and the
file's formatting stay as they are.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from vision.camera import CameraError, dump_config, open_camera, write_text_atomic

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
CATALOG_PATH = ROOT / "catalog.json"

WINDOW = "SpeedMart calibrate"
MIN_SIZE = 10  # px; smaller drags are treated as accidental clicks
MESSAGE_S = 4.0

Roi = list[int]  # [x1, y1, x2, y2]


class SaveError(Exception):
    """ROIs were rejected; the message is shown to the user."""


# --- config save logic (tested without a camera) ----------------------------------------------


def normalize(x1: float, y1: float, x2: float, y2: float) -> Roi:
    """Integer [x1, y1, x2, y2] with x1 <= x2 and y1 <= y2, whichever corner the drag started from."""
    return [int(round(min(x1, x2))), int(round(min(y1, y2))), int(round(max(x1, x2))), int(round(max(y1, y2)))]


def overlaps(a: Roi, b: Roi) -> bool:
    """True when the rectangles share any area. Touching edges do not count."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def validate_rois(rois: list[Roi], n_bays: int, width: int, height: int) -> str | None:
    """Reason the ROIs cannot be saved, or None if they are fine."""
    if len(rois) != n_bays:
        return f"Need {n_bays} rectangles (one per bay), have {len(rois)}"
    for i, roi in enumerate(rois):
        if len(roi) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) for v in roi):
            return f"Bay {i}: ROI must be 4 integers"
        x1, y1, x2, y2 = roi
        if x2 - x1 < MIN_SIZE or y2 - y1 < MIN_SIZE:
            return f"Bay {i}: rectangle is too small"
        if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
            return f"Bay {i}: rectangle is outside the {width}x{height} frame"
    for i in range(len(rois)):
        for j in range(i + 1, len(rois)):
            if overlaps(rois[i], rois[j]):
                return f"Bays {i} and {j} overlap"
    return None


_ROI_RE = re.compile(r'("roi"\s*:\s*)\[[^\[\]]*\]')


def _replace_rois_in_text(text: str, rois: list[Roi]) -> str | None:
    """Swap the literal "roi": [...] arrays in place, keeping all other text byte for byte.
    Returns None if the file does not have exactly one "roi" per bay."""
    matches = list(_ROI_RE.finditer(text))
    if len(matches) != len(rois):
        return None
    parts, pos = [], 0
    for match, roi in zip(matches, rois):
        parts.append(text[pos:match.start()])
        parts.append(f"{match.group(1)}[{roi[0]}, {roi[1]}, {roi[2]}, {roi[3]}]")
        pos = match.end()
    parts.append(text[pos:])
    return "".join(parts)


def save_rois(rois: list[Roi], path: Path = CONFIG_PATH) -> None:
    """Validate and write rois into bays[i].roi. Raises SaveError with a readable reason."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    config = json.loads(text)
    bays = config.get("bays", [])
    cam = config.get("camera", {})
    reason = validate_rois(rois, len(bays), int(cam.get("width", 0)), int(cam.get("height", 0)))
    if reason:
        raise SaveError(reason)

    expected = json.loads(text)
    for bay, roi in zip(expected["bays"], rois):
        bay["roi"] = list(roi)

    new_text = _replace_rois_in_text(text, rois)
    if new_text is None or json.loads(new_text) != expected:
        # unexpected layout: fall back to a full rewrite, same data, 2 space indent
        new_text = dump_config(expected, text)
    write_text_atomic(path, new_text)


# --- display helpers ----------------------------------------------------------------------------


def _make_dpi_aware() -> None:
    """On Windows, stop the OS from bitmap-scaling the window, so window pixels are frame pixels."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def window_to_frame(x: int, y: int, view_w: int, view_h: int, frame_w: int, frame_h: int) -> tuple[int, int]:
    """Map a mouse position in the displayed image (view_w x view_h window pixels) to frame pixels,
    clamped to the frame."""
    sx = frame_w / view_w if view_w > 0 else 1.0
    sy = frame_h / view_h if view_h > 0 else 1.0
    fx = min(max(int(round(x * sx)), 0), frame_w)
    fy = min(max(int(round(y * sy)), 0), frame_h)
    return fx, fy


def _view_size(frame_w: int, frame_h: int) -> tuple[int, int]:
    """Size of the image as the window system shows it. Differs from the frame when the OS scales the
    window on a high DPI screen; mouse events arrive in that space."""
    try:
        _, _, w, h = cv2.getWindowImageRect(WINDOW)
    except cv2.error:
        return frame_w, frame_h
    if w <= 0 or h <= 0:
        return frame_w, frame_h
    if abs(w - frame_w) <= 2 and abs(h - frame_h) <= 2:
        return frame_w, frame_h
    return w, h


def _label(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int], scale: float = 0.6) -> None:
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


# --- interactive tool ---------------------------------------------------------------------------


class Calibrator:
    def __init__(self, config: dict, catalog: dict, config_path: Path):
        self.config_path = config_path
        self.bays = config["bays"]
        self.width = int(config["camera"]["width"])
        self.height = int(config["camera"]["height"])
        names = {s["sku"]: s["name"] for s in catalog.get("skus", [])}
        self.labels = [f"bay {b['id']} {names.get(b.get('sku'), b.get('sku', '?'))}" for b in self.bays]
        self.saved: list[Roi] = [list(map(int, b["roi"])) for b in self.bays if b.get("roi")]
        self.rois: list[Roi] = []
        self.drag_start: tuple[int, int] | None = None
        self.drag_now: tuple[int, int] | None = None
        self.message = ""
        self.message_color = (255, 255, 255)
        self.message_until = 0.0

    def say(self, text: str, color: tuple[int, int, int] = (255, 255, 255)) -> None:
        print(text)
        self.message, self.message_color, self.message_until = text, color, time.monotonic() + MESSAGE_S

    def on_mouse(self, event: int, x: int, y: int, flags: int, param) -> None:
        view_w, view_h = _view_size(self.width, self.height)
        pt = window_to_frame(x, y, view_w, view_h, self.width, self.height)
        if event == cv2.EVENT_LBUTTONDOWN:
            if len(self.rois) >= len(self.bays):
                self.say("All bays drawn. s = save, u = undo last, r = redo all", (0, 200, 255))
                return
            self.drag_start = self.drag_now = pt
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_start is not None:
            self.drag_now = pt
        elif event == cv2.EVENT_LBUTTONUP and self.drag_start is not None:
            roi = normalize(*self.drag_start, *pt)
            self.drag_start = self.drag_now = None
            if roi[2] - roi[0] < MIN_SIZE or roi[3] - roi[1] < MIN_SIZE:
                self.say("Too small, drag a larger rectangle", (0, 200, 255))
                return
            self.rois.append(roi)
            self.say(f"{self.labels[len(self.rois) - 1]}: {roi}", (0, 255, 0))

    def key(self, key: int) -> bool:
        """Handle a key press. Returns False when the tool should exit."""
        if key in (ord("q"), 27):
            print("Quit without saving.")
            return False
        if key == ord("r"):
            self.rois.clear()
            self.say("Cleared. Drag bay 0 again", (0, 200, 255))
        elif key == ord("u"):
            if self.rois:
                self.rois.pop()
                self.say(f"Undid {self.labels[len(self.rois)]}", (0, 200, 255))
        elif key == ord("s"):
            try:
                save_rois(self.rois, self.config_path)
            except SaveError as exc:
                self.say(f"NOT SAVED: {exc}", (0, 0, 255))
            else:
                self.saved = [list(r) for r in self.rois]
                self.say(f"Saved {len(self.rois)} ROIs to {self.config_path.name}. q to quit", (0, 255, 0))
        return True

    def draw(self, frame: np.ndarray) -> np.ndarray:
        view = frame.copy()
        drawing = len(self.rois) < len(self.bays)
        if drawing:  # saved boxes as a faint reference until all new ones are drawn
            for i, roi in enumerate(self.saved):
                cv2.rectangle(view, tuple(roi[:2]), tuple(roi[2:]), (200, 200, 200), 1)
                _label(view, f"{self.labels[i]} (saved)" if i < len(self.labels) else "saved", (roi[0] + 4, roi[1] + 20), (200, 200, 200), 0.5)
        for i, roi in enumerate(self.rois):
            cv2.rectangle(view, tuple(roi[:2]), tuple(roi[2:]), (0, 255, 0), 2)
            _label(view, self.labels[i], (roi[0] + 4, roi[1] + 22), (0, 255, 0))
        if self.drag_start and self.drag_now:
            cv2.rectangle(view, self.drag_start, self.drag_now, (0, 255, 255), 2)

        if drawing:
            prompt = f"Drag {self.labels[len(self.rois)]}   ({len(self.rois)}/{len(self.bays)})"
        else:
            prompt = "All bays drawn: s save, u undo, r redo"
        _label(view, prompt, (10, 30), (255, 255, 255), 0.8)
        _label(view, "r redo all | u undo | s save | q quit", (10, self.height - 15), (255, 255, 255), 0.6)
        if self.message and time.monotonic() < self.message_until:
            _label(view, self.message, (10, 65), self.message_color, 0.8)
        return view


def run(config_path: Path = CONFIG_PATH, catalog_path: Path = CATALOG_PATH) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not config.get("bays"):
        print("config.json has no bays to calibrate.", file=sys.stderr)
        return 1
    tool = Calibrator(config, catalog, config_path)

    _make_dpi_aware()
    try:
        camera = open_camera(config)
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1

    with camera:
        cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(WINDOW, tool.on_mouse)
        size_warned = False
        print(f"Drag one rectangle per bay, bay 0 to {len(tool.bays) - 1}. r redo all, u undo, s save, q quit.")
        try:
            while True:
                frame, _, _ = camera.read()
                if frame.shape[1] != tool.width or frame.shape[0] != tool.height:
                    if not size_warned:
                        print(
                            f"WARNING: camera frame is {frame.shape[1]}x{frame.shape[0]}, showing it stretched to "
                            f"config {tool.width}x{tool.height}. Fix camera.width/height before trusting ROIs."
                        )
                        size_warned = True
                    frame = cv2.resize(frame, (tool.width, tool.height))
                cv2.imshow(WINDOW, tool.draw(frame))
                key = cv2.waitKey(1) & 0xFF
                if key != 255 and not tool.key(key):
                    break
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    print("Window closed.")
                    break
        except CameraError as exc:
            print(f"Camera error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            pass
        finally:
            cv2.destroyAllWindows()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return run()


if __name__ == "__main__":
    sys.exit(main())
