"""Save frames from the overhead camera for YOLO labeling (Section 14 step 1, F13).

    python -m training.capture [--interval 0.5] [--out training/raw]

Opens the camera exactly like the vision worker (config.json camera settings, exposure locked), so the
training images look like what YOLO will see live. Frames are resized to camera.width x camera.height
if the camera delivers another size, same as the worker.

Keys:
    space   toggle auto save (one frame every --interval seconds, default 0.5)
    s       save one frame now
    e       toggle "empty shelf" marking: frames saved while it is on count as empty shelf frames
    q/Esc   quit

Images go to training/raw/<YYYYmmdd-HHMMSS>/ (one folder per run, created on the first save) as JPEG.
The overlay (bay boxes, counters) is drawn on the preview only, never on the saved images.

The counter also tracks how many saved frames had NO ArUco tag visible. At least half the dataset must
have the tags covered or removed, otherwise YOLO learns the tags instead of the products (TRAINING.md).
With config.json vision.mode "yolo" (tag free store) every frame must be tag free: the preview shows the
reminder "No tags: remove all tags before capturing" and wants 100 % of frames without tags. About 10 %
of the frames should show an empty shelf (press e while you clear the bays); the counter shows the share.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow  python training/capture.py  as well as  python -m training.capture
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from vision.aruco_detect import TagDetector  # noqa: E402
from vision.camera import CameraError, open_camera  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
CATALOG_PATH = ROOT / "catalog.json"
DEFAULT_OUT = ROOT / "training" / "raw"
DEFAULT_INTERVAL_S = 0.5
JPEG_QUALITY = 95
TARGET_MIN, TARGET_MAX = 250, 400
EMPTY_SHARE_TARGET = 0.10  # about one frame in ten shows an empty shelf
NO_TAGS_REMINDER = "No tags: remove all tags before capturing"
WINDOW = "SpeedMart capture"

log = logging.getLogger("training.capture")


# --- pure logic (tested without a camera) -----------------------------------------------------


def session_name(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


@dataclass
class CaptureSession:
    """Where and when frames are saved. The folder is created lazily on the first save, so starting and
    quitting without saving leaves nothing behind."""

    out_root: Path
    name: str
    interval_s: float = DEFAULT_INTERVAL_S
    auto: bool = False
    saved: int = 0
    saved_without_tags: int = 0
    # vision.mode from config.json: "yolo" is the tag free store, where every frame must be free of tags
    mode: str = "fusion"
    empty: bool = False  # "empty shelf" marking on (key e): frames saved now are empty shelf frames
    saved_empty: int = 0
    _next_auto: float = field(default=0.0, repr=False)

    @property
    def folder(self) -> Path:
        return self.out_root / self.name

    def toggle_auto(self, now: float) -> bool:
        self.auto = not self.auto
        self._next_auto = now  # first auto frame right away
        return self.auto

    def toggle_empty(self) -> bool:
        self.empty = not self.empty
        return self.empty

    @property
    def tag_free(self) -> bool:
        """True in the tag free store (vision.mode "yolo"): no saved frame may show a tag."""
        return self.mode == "yolo"

    @property
    def no_tag_target(self) -> float:
        return 1.0 if self.tag_free else 0.5

    def auto_due(self, now: float) -> bool:
        """True when auto save is on and the next frame is due; schedules the one after."""
        if not self.auto or now < self._next_auto:
            return False
        # keep a steady cadence, but never try to catch up after a stall
        self._next_auto += self.interval_s
        if self._next_auto <= now:
            self._next_auto = now + self.interval_s
        return True

    def next_path(self) -> Path:
        # the run name in the file name keeps files unique when several runs are uploaded together
        return self.folder / f"{self.name}_{self.saved + 1:04d}.jpg"

    def save(self, frame: np.ndarray, tags_visible: int) -> Path:
        path = self.next_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
            raise OSError(f"could not write {path}")
        self.saved += 1
        if tags_visible == 0:
            self.saved_without_tags += 1
        if self.empty:
            self.saved_empty += 1
        return path

    @property
    def no_tag_share(self) -> float:
        return self.saved_without_tags / self.saved if self.saved else 0.0

    @property
    def empty_share(self) -> float:
        return self.saved_empty / self.saved if self.saved else 0.0


def draw_capture_overlay(frame: np.ndarray, bays: list[dict], session: CaptureSession, tags_visible: int,
                         fps: float, message: str = "") -> np.ndarray:
    """Preview only: bay boxes, save counters, auto state. Returns a new image."""
    view = frame.copy()
    h = view.shape[0]

    def text(s: str, org: tuple[int, int], color: tuple[int, int, int], scale: float = 0.7) -> None:
        cv2.putText(view, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(view, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)

    for bay in bays:
        x1, y1, x2, y2 = (int(v) for v in bay["roi"])
        cv2.rectangle(view, (x1, y1), (x2, y2), (0, 200, 0), 2)
        text(f"bay {bay['id']} {bay.get('sku', '')}", (x1 + 4, y1 + 22), (0, 200, 0), 0.6)

    count_color = (0, 200, 0) if session.saved >= TARGET_MIN else (255, 255, 255)
    text(f"saved {session.saved}  (target {TARGET_MIN}-{TARGET_MAX})", (10, 30), count_color, 0.8)
    want = session.no_tag_target
    share_ok = session.saved == 0 or session.no_tag_share >= want
    text(f"no tags visible: {session.saved_without_tags}/{session.saved} ({session.no_tag_share:.0%}, want >= {want:.0%})",
         (10, 62), (0, 200, 0) if share_ok else (0, 140, 255), 0.65)
    empty_ok = session.saved < 10 or session.empty_share >= EMPTY_SHARE_TARGET
    text(f"empty shelf frames: {session.saved_empty}/{session.saved} ({session.empty_share:.0%}, want ~{EMPTY_SHARE_TARGET:.0%})"
         + ("   [EMPTY SHELF marking ON]" if session.empty else ""),
         (10, 92), (0, 200, 0) if empty_ok else (0, 140, 255), 0.65)
    text(f"tags visible now: {tags_visible}   {fps:4.1f} fps", (10, 122), (255, 255, 255), 0.6)
    y = 152
    if session.tag_free:
        # tag free store (vision.mode "yolo"): the reminder stays on screen, red while a tag is in view
        text(NO_TAGS_REMINDER, (10, y), (0, 0, 255) if tags_visible else (0, 220, 255), 0.7)
        y += 30
    if session.auto:
        cv2.circle(view, (view.shape[1] - 30, 30), 12, (0, 0, 255), -1)
        text(f"AUTO every {session.interval_s:g} s", (view.shape[1] - 250, 38), (0, 0, 255), 0.7)
    if message:
        text(message, (10, y), (0, 220, 255), 0.6)
    text(f"folder {session.folder}", (10, h - 40), (200, 200, 200), 0.5)
    text("space auto save on/off | s save one | e empty shelf on/off | q quit", (10, h - 12), (255, 255, 255), 0.6)
    return view


# --- camera loop ------------------------------------------------------------------------------


def run(out_root: Path, interval_s: float) -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    width, height = int(config["camera"]["width"]), int(config["camera"]["height"])
    bays = config["bays"]
    detector = TagDetector(config["vision"]["aruco_dict"], (u["tag_id"] for u in catalog["units"]),
                           clahe=bool(config["vision"].get("clahe", True)))
    mode = str(config["vision"].get("mode", "fusion"))
    session = CaptureSession(out_root, session_name(datetime.now()), interval_s, mode=mode)
    try:
        camera = open_camera(config)
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1

    print(f"Capture ready. Images go to {session.folder}  (vision.mode {mode})")
    if session.tag_free:
        print(NO_TAGS_REMINDER)
    print("Keys: space = auto save on/off, s = save one frame, e = empty shelf marking on/off, q = quit")
    message, message_until = "", 0.0
    stamps: list[float] = []
    try:
        with camera:
            cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
            while True:
                frame, _, _ = camera.read()
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))
                now = time.monotonic()
                stamps = [t for t in stamps[-29:] if now - t < 2.0] + [now]
                fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 and stamps[-1] > stamps[0] else 0.0
                tags_visible = len(detector.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))

                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                save_now = False
                if key == ord(" "):
                    on = session.toggle_auto(now)
                    message, message_until = f"auto save {'ON' if on else 'OFF'}", now + 3.0
                    print(message)
                elif key == ord("s"):
                    save_now = True
                elif key == ord("e"):
                    on = session.toggle_empty()
                    message, message_until = f"empty shelf marking {'ON' if on else 'OFF'}", now + 3.0
                    print(message)
                if save_now or session.auto_due(now):
                    path = session.save(frame, tags_visible)
                    if save_now:
                        message, message_until = f"saved {path.name}", now + 2.0
                    if session.saved % 25 == 0 or save_now:
                        print(f"{session.saved} frames saved ({session.saved_without_tags} without tags, "
                              f"{session.saved_empty} empty shelf)")

                cv2.imshow(WINDOW, draw_capture_overlay(frame, bays, session, tags_visible, fps,
                                                        message if now < message_until else ""))
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
    print(f"Done: {session.saved} frames in {session.folder if session.saved else '(nothing saved)'}; "
          f"{session.saved_without_tags} without visible tags ({session.no_tag_share:.0%}); "
          f"{session.saved_empty} empty shelf ({session.empty_share:.0%}).")
    if session.tag_free and session.saved_without_tags < session.saved:
        print(f"WARNING: {session.saved - session.saved_without_tags} frames show a tag. vision.mode is yolo: "
              f"delete them or recapture without tags (TRAINING.md, tag free capture).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m training.capture", description="Save shelf frames for labeling.")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="auto save period in seconds")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="root folder for the run folders")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    return run(args.out, args.interval)


if __name__ == "__main__":
    sys.exit(main())
