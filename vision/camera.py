"""Open camera, lock exposure/focus, read frames.

    python -m vision.camera --list       try indexes 0 to 5, print which open and at what resolution
    python -m vision.camera --preview    live feed from config.json camera settings, with actual
                                         resolution and measured FPS drawn on the frame (q / Esc quits)

open_camera(cfg) returns a Camera whose background thread always holds the newest frame, so callers
never process stale buffered frames. Camera.read() -> (frame, frame_id, timestamp).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

log = logging.getLogger("vision.camera")

FIRST_FRAME_TIMEOUT_S = 5.0
MAX_FAILED_GRABS = 50  # consecutive failed reads before the camera is considered gone
FPS_WINDOW = 30  # frames used for the measured FPS


class CameraError(Exception):
    """The camera could not be opened or stopped delivering frames."""


@dataclass(frozen=True)
class Platform:
    name: str  # "windows" | "linux" | "macos" | "other"
    backend: int
    backend_name: str


def detect_platform() -> Platform:
    if sys.platform.startswith("win"):
        return Platform("windows", cv2.CAP_DSHOW, "DSHOW")
    if sys.platform.startswith("linux"):
        return Platform("linux", cv2.CAP_V4L2, "V4L2")
    if sys.platform == "darwin":
        return Platform("macos", cv2.CAP_AVFOUNDATION, "AVFoundation")
    return Platform("other", cv2.CAP_ANY, "ANY")


def _quiet_opencv_logs() -> None:
    """OpenCV prints a warning for every index that fails to open; our own messages are clearer."""
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass


def _fourcc_str(value: float) -> str:
    code = int(value)
    chars = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))
    return chars if chars.isascii() and chars.isprintable() and chars.strip() else "?"


def _linux_exposure_units(exposure: float) -> float:
    """config.json uses the Windows convention (log2 seconds, e.g. -6 = 1/64 s).
    V4L2 exposure_time_absolute is in 100 us units, so -6 -> 156. Positive values pass through."""
    if exposure <= 0:
        return float(round((2.0 ** exposure) * 10000))
    return float(exposure)


# --- property locking ------------------------------------------------------------------------


@dataclass
class PropResult:
    name: str
    requested: float | None
    set_ok: bool | None
    readback: float
    applied: bool | None  # None = not requested


def _set(cap: cv2.VideoCapture, prop: int, value: float) -> bool:
    try:
        return bool(cap.set(prop, value))
    except cv2.error:
        return False


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _apply_exposure_focus(cap: cv2.VideoCapture, cam: dict, plat: Platform) -> dict[str, PropResult]:
    results: dict[str, PropResult] = {}

    if not cam.get("auto_exposure", True):
        target = float(cam.get("exposure", -6))
        if plat.name == "linux":
            # V4L2: 1 = manual, 3 = aperture priority (auto)
            auto_ok = _set(cap, cv2.CAP_PROP_AUTO_EXPOSURE, 1)
            target = _linux_exposure_units(target)
        elif plat.name == "windows":
            # DSHOW: 0.25 selects manual on most builds; setting EXPOSURE also sets the manual flag
            auto_ok = _set(cap, cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        else:
            auto_ok = _set(cap, cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        exp_ok = _set(cap, cv2.CAP_PROP_EXPOSURE, target)
        readback = cap.get(cv2.CAP_PROP_EXPOSURE)
        tol = max(0.5, abs(target) * 0.05)
        results["auto_exposure"] = PropResult(
            "auto_exposure", 0, auto_ok, cap.get(cv2.CAP_PROP_AUTO_EXPOSURE), None
        )
        results["exposure"] = PropResult(
            "exposure", target, exp_ok, readback, exp_ok and _close(readback, target, tol)
        )
    else:
        _set(cap, cv2.CAP_PROP_AUTO_EXPOSURE, 3 if plat.name == "linux" else 0.75)

    if not cam.get("autofocus", True):
        target = float(cam.get("focus", 0))
        af_ok = _set(cap, cv2.CAP_PROP_AUTOFOCUS, 0)
        focus_ok = _set(cap, cv2.CAP_PROP_FOCUS, target)
        readback = cap.get(cv2.CAP_PROP_FOCUS)
        # Logitech focus moves in steps of 5
        results["autofocus"] = PropResult("autofocus", 0, af_ok, cap.get(cv2.CAP_PROP_AUTOFOCUS), None)
        results["focus"] = PropResult(
            "focus", target, focus_ok, readback, focus_ok and _close(readback, target, 5)
        )
    else:
        _set(cap, cv2.CAP_PROP_AUTOFOCUS, 1)

    return results


def lock_help(plat: Platform, cam: dict, what: list[str]) -> str:
    """How to lock exposure / focus outside OpenCV on this OS."""
    items = " and ".join(what)
    if plat.name == "windows":
        return (
            f"Lock {items} with Logitech's own app: Logi Tune (or G HUB for C922 / StreamCam, "
            "Logitech Capture for older models). Turn off auto exposure / autofocus there, set the "
            "values, close the app, then rerun this command. Settings stay while the camera is plugged in."
        )
    if plat.name == "linux":
        exp = int(_linux_exposure_units(float(cam.get("exposure", -6))))
        focus = int(cam.get("focus", 0))
        return (
            f"Lock {items} with v4l2-ctl (package v4l-utils). List controls: v4l2-ctl -d /dev/video"
            f"{cam.get('index', 0)} -l. Then: v4l2-ctl -d /dev/video{cam.get('index', 0)} "
            f"-c auto_exposure=1 -c exposure_time_absolute={exp} "
            f"-c focus_automatic_continuous=0 -c focus_absolute={focus} "
            "(older kernels name them exposure_auto, exposure_absolute, focus_auto)."
        )
    if plat.name == "macos":
        return (
            f"AVFoundation usually ignores {items} from OpenCV. Lock them with a UVC control tool: "
            "CameraController (github.com/Itaybre/CameraController) or uvc-util. Turn off auto "
            "exposure / autofocus there, keep it running, then rerun this command."
        )
    return f"Lock {items} with your camera vendor's control tool."


# --- camera ---------------------------------------------------------------------------------


class Camera:
    """Owns the VideoCapture. A daemon thread grabs continuously and keeps only the newest frame."""

    def __init__(self, cap: cv2.VideoCapture, cam_cfg: dict, plat: Platform):
        self._cap = cap
        self.cfg = cam_cfg
        self.platform = plat
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._frame_id = -1
        self._ts = 0.0
        self._last_returned_id = -1
        self._stamps: deque[float] = deque(maxlen=FPS_WINDOW)
        self._running = True
        self.error: str | None = None
        self.props: dict[str, PropResult] = {}
        self._thread = threading.Thread(target=self._reader, name="camera-reader", daemon=True)

    # reader thread
    def _reader(self) -> None:
        failed = 0
        while self._running:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                failed += 1
                if failed >= MAX_FAILED_GRABS:
                    with self._cond:
                        self.error = "camera stopped delivering frames (unplugged or in use by another app?)"
                        self._running = False
                        self._cond.notify_all()
                    return
                time.sleep(0.01)
                continue
            failed = 0
            now = time.monotonic()
            with self._cond:
                self._frame = frame
                self._frame_id += 1
                self._ts = now
                self._stamps.append(now)
                self._cond.notify_all()

    def read(self, timeout: float = 1.0) -> tuple[np.ndarray, int, float]:
        """Newest frame as (frame, frame_id, timestamp). Waits up to `timeout` for a frame newer than the
        last one returned; on timeout returns the newest available. timestamp is time.monotonic()."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._frame_id > self._last_returned_id or not self._running, timeout
            )
            if self.error:
                raise CameraError(self.error)
            if self._frame is None:
                raise CameraError("no frame from camera yet")
            self._last_returned_id = self._frame_id
            return self._frame, self._frame_id, self._ts

    @property
    def measured_fps(self) -> float:
        with self._cond:
            if len(self._stamps) < 2:
                return 0.0
            span = self._stamps[-1] - self._stamps[0]
            return (len(self._stamps) - 1) / span if span > 0 else 0.0

    @property
    def frame_size(self) -> tuple[int, int]:
        with self._cond:
            if self._frame is None:
                return (0, 0)
            h, w = self._frame.shape[:2]
            return (w, h)

    def close(self) -> None:
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._cap.release()

    def __enter__(self) -> Camera:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _open_capture(index: int, plat: Platform) -> tuple[cv2.VideoCapture, str]:
    cap = cv2.VideoCapture(index, plat.backend)
    if cap.isOpened():
        return cap, plat.backend_name
    cap.release()
    if plat.name == "windows":  # some drivers only work through Media Foundation
        cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        if cap.isOpened():
            return cap, "MSMF"
        cap.release()
    raise CameraError(
        f"Could not open camera index {index} with {plat.backend_name}. Is it plugged in and not used by "
        "another app (Zoom, Teams, browser)? Run: python -m vision.camera --list"
    )


def open_camera(cfg: dict) -> Camera:
    """Open config camera.index at camera.width x camera.height and camera.fps, apply manual exposure and
    focus when requested, read every property back and log what actually applied. Accepts the whole
    config.json dict or just its "camera" section. Raises CameraError if the camera cannot be opened."""
    cam_cfg = cfg.get("camera", cfg)
    index = int(cam_cfg.get("index", 0))
    width, height, fps = int(cam_cfg["width"]), int(cam_cfg["height"]), float(cam_cfg["fps"])
    plat = detect_platform()
    _quiet_opencv_logs()

    cap, backend_name = _open_capture(index, plat)
    _set(cap, cv2.CAP_PROP_FRAME_WIDTH, width)
    _set(cap, cv2.CAP_PROP_FRAME_HEIGHT, height)
    _set(cap, cv2.CAP_PROP_FPS, fps)
    if plat.name != "macos":
        # MJPG lets USB 2.0 webcams deliver 1280x720 at 30 fps (raw YUY2 tops out near 10 fps).
        # DSHOW only honours it when set after the resolution and fps.
        _set(cap, cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    _set(cap, cv2.CAP_PROP_BUFFERSIZE, 1)

    # grab one frame so the driver has negotiated the format before we lock exposure/focus
    ok, _ = cap.read()
    if not ok:
        cap.release()
        raise CameraError(f"Camera index {index} opened but returned no frame. Try another index (--list).")
    props = _apply_exposure_focus(cap, cam_cfg, plat)

    camera = Camera(cap, cam_cfg, plat)
    camera.props = props
    camera._thread.start()
    deadline = time.monotonic() + FIRST_FRAME_TIMEOUT_S
    while camera.frame_size == (0, 0):
        if camera.error or time.monotonic() > deadline:
            camera.close()
            raise CameraError(camera.error or f"Camera index {index} delivered no frames in {FIRST_FRAME_TIMEOUT_S:.0f} s.")
        time.sleep(0.02)

    _report(camera, cap, backend_name, width, height, fps)
    return camera


def _report(camera: Camera, cap: cv2.VideoCapture, backend_name: str, width: int, height: int, fps: float) -> None:
    """Log every property as read back from the driver, and warn about anything that did not take."""
    plat, cam_cfg = camera.platform, camera.cfg
    actual_w, actual_h = camera.frame_size
    log.info("camera %s opened via %s on %s", cam_cfg.get("index", 0), backend_name, plat.name)
    log.info("  resolution  requested %dx%d  actual %dx%d", width, height, actual_w, actual_h)
    log.info("  fps         requested %g  driver reports %g", fps, cap.get(cv2.CAP_PROP_FPS))
    log.info("  fourcc      %s", _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)))
    log.info("  auto_exp    %g   exposure %g", cap.get(cv2.CAP_PROP_AUTO_EXPOSURE), cap.get(cv2.CAP_PROP_EXPOSURE))
    log.info("  autofocus   %g   focus    %g", cap.get(cv2.CAP_PROP_AUTOFOCUS), cap.get(cv2.CAP_PROP_FOCUS))
    for r in camera.props.values():
        if r.applied is None:
            continue
        log.info(
            "  %-11s requested %g  set() %s  read back %g  -> %s",
            r.name, r.requested, "ok" if r.set_ok else "refused", r.readback,
            "LOCKED" if r.applied else "NOT APPLIED",
        )

    if (actual_w, actual_h) != (width, height):
        log.warning(
            "WARNING: camera gives %dx%d, config.json asks for %dx%d. ROIs are in config pixels; "
            "set camera.width/height to a mode this camera supports.", actual_w, actual_h, width, height,
        )
    failed = [name for name in ("exposure", "focus") if name in camera.props and not camera.props[name].applied]
    if failed:
        log.warning("WARNING: %s did NOT lock. Lighting changes or refocusing will disturb detection.", " and ".join(failed))
        log.warning("  %s", lock_help(plat, cam_cfg, failed))
    elif camera.props:
        log.info("  exposure/focus locked as requested")


# --- CLI --------------------------------------------------------------------------------------


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_cameras(max_index: int = 5) -> list[tuple[int, int, int]]:
    """Try indexes 0..max_index with this OS's backend; return (index, width, height) for each that opens."""
    plat = detect_platform()
    _quiet_opencv_logs()
    found = []
    for index in range(max_index + 1):
        cap = cv2.VideoCapture(index, plat.backend)
        try:
            if not cap.isOpened():
                print(f"  index {index}: not available")
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                print(f"  index {index}: opens but returns no frame")
                continue
            h, w = frame.shape[:2]
            print(f"  index {index}: OK  default {w}x{h}")
            found.append((index, w, h))
        finally:
            cap.release()
    return found


def preview(cfg: dict) -> None:
    window = "SpeedMart camera preview"
    with open_camera(cfg) as camera:
        cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
        locks = ", ".join(
            f"{r.name} {'locked' if r.applied else 'NOT locked'}" for r in camera.props.values() if r.applied is not None
        ) or "auto exposure/focus (per config)"
        print("Preview running. q or Esc quits.")
        while True:
            frame, frame_id, _ = camera.read()
            view = frame.copy()
            h, w = view.shape[:2]
            lines = [f"{w}x{h}  {camera.measured_fps:5.1f} fps  frame {frame_id}", locks]
            for i, text in enumerate(lines):
                y = 30 + i * 30
                cv2.putText(view, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(view, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow(window, view)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                break
        print(f"Last measured FPS: {camera.measured_fps:.1f}")
    cv2.destroyAllWindows()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vision.camera", description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="try camera indexes 0 to 5")
    group.add_argument("--preview", action="store_true", help="live feed using config.json camera settings")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.list:
        print(f"Probing cameras with {detect_platform().backend_name}:")
        found = list_cameras()
        if not found:
            print("No camera opened. Check the USB cable and close apps that use the camera.")
            return 1
        print("Set camera.index in config.json to the overhead camera's index.")
        return 0

    try:
        preview(load_config())
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
