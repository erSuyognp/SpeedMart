"""Open camera, lock exposure/focus, read frames.

    python -m vision.camera --list       try indexes 0 to 5, print which open and at what resolution
    python -m vision.camera --preview    live feed from config.json camera settings, with actual
                                         resolution, measured FPS, exposure, gain and mean brightness
                                         drawn on the frame. Keys:
                                           [ / ]  exposure down / up one unit
                                           - / =  gain down / up one unit
                                           a      auto then lock again
                                           s      save exposure and gain into config.json
                                           q/Esc  quit

open_camera(cfg) returns a Camera whose background thread always holds the newest frame, so callers
never process stale buffered frames. Camera.read() -> (frame, frame_id, timestamp).

camera.exposure and camera.gain in config.json are in the capture backend's own units (DSHOW exposure:
log2 seconds, e.g. -5 = 1/32 s; V4L2: 100 us steps). When camera.exposure is null and auto_exposure is
false, the camera runs auto exposure for ~2 s, then locks the exposure and gain it chose.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
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
AUTO_SETTLE_S = 2.0  # time auto exposure gets before its values are locked
SETTLE_FRAMES = 5  # frames to wait after a property change before measuring brightness
DARK, BRIGHT = 40, 220  # mean brightness outside this range after locking -> warning


class CameraError(Exception):
    """The camera could not be opened or stopped delivering frames."""


@dataclass(frozen=True)
class Platform:
    name: str  # "windows" | "linux" | "macos" | "other"
    backend: int
    backend_name: str
    auto_exposure_on: float
    auto_exposure_off: float


def detect_platform() -> Platform:
    # V4L2: 3 = aperture priority (auto), 1 = manual. DSHOW and others: 0.75 = auto, 0.25 = manual.
    if sys.platform.startswith("win"):
        return Platform("windows", cv2.CAP_DSHOW, "DSHOW", 0.75, 0.25)
    if sys.platform.startswith("linux"):
        return Platform("linux", cv2.CAP_V4L2, "V4L2", 3, 1)
    if sys.platform == "darwin":
        return Platform("macos", cv2.CAP_AVFOUNDATION, "AVFoundation", 0.75, 0.25)
    return Platform("other", cv2.CAP_ANY, "ANY", 0.75, 0.25)


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


def mean_brightness(frame: np.ndarray) -> float:
    """Mean gray level, 0 to 255."""
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def _num(value: float) -> int | float:
    """Integral floats as int, so config.json gets -5 rather than -5.0."""
    return int(value) if float(value).is_integer() else float(value)


# --- config.json writes ---------------------------------------------------------------------------


def write_text_atomic(path: Path, text: str) -> None:
    """Replace path with text in one step, so a crash never leaves a half written config."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump_config(config: dict, original_text: str) -> str:
    """Full rewrite with 2 space indent, keeping the original file's line endings."""
    newline = "\r\n" if "\r\n" in original_text else "\n"
    return json.dumps(config, indent=2, ensure_ascii=False).replace("\n", newline) + newline


_SCALAR = r"(?:null|true|false|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"


def _set_section_keys_in_text(text: str, section: str, values: dict) -> str | None:
    """Rewrite <section>.<key> literals in place, adding missing keys at the end of that block. The block
    must be a flat object (no nested braces). Returns None if the block cannot be found."""
    block = re.search(rf'"{re.escape(section)}"\s*:\s*\{{[^{{}}]*\}}', text)
    if not block:
        return None
    body = block.group(0)
    for key, value in values.items():
        literal = json.dumps(value)
        key_re = re.compile(rf'("{re.escape(key)}"\s*:\s*){_SCALAR}')
        if key_re.search(body):
            body = key_re.sub(lambda m: m.group(1) + literal, body, count=1)
            continue
        last = re.search(rf'\n([ \t]*)"[^"]+"\s*:\s*(?:{_SCALAR}|"[^"]*")(?=\s*\}}$)', body)
        if not last:
            return None
        newline = "\r\n" if "\r\n" in text else "\n"
        body = body[: last.end()] + f",{newline}{last.group(1)}\"{key}\": {literal}" + body[last.end():]
    return text[: block.start()] + body + text[block.end():]


def _set_camera_keys_in_text(text: str, values: dict) -> str | None:
    return _set_section_keys_in_text(text, "camera", values)


def save_section_settings(section: str, values: dict, path: Path = CONFIG_PATH) -> None:
    """Write <section>.<key> = value for each item into config.json (section is a flat object such as
    "camera" or "vision"). Every other key and the file's formatting stay unchanged."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    expected = json.loads(text)
    expected.setdefault(section, {}).update(values)
    new_text = _set_section_keys_in_text(text, section, values)
    if new_text is None or json.loads(new_text) != expected:
        new_text = dump_config(expected, text)
    write_text_atomic(path, new_text)


def save_camera_settings(values: dict, path: Path = CONFIG_PATH) -> None:
    """Write camera.<key> = value for each item (e.g. exposure, gain) into config.json. Every other key and
    the file's formatting stay unchanged."""
    save_section_settings("camera", values, path)


# --- property locking ------------------------------------------------------------------------


@dataclass
class PropResult:
    name: str
    requested: float | None
    set_ok: bool | None
    readback: float
    applied: bool | None  # None = not requested


def lock_help(plat: Platform, cam: dict, what: list[str], exposure: float, gain: float) -> str:
    """How to lock exposure / focus outside OpenCV on this OS."""
    items = " and ".join(what)
    if plat.name == "windows":
        return (
            f"Lock {items} with Logitech's own app: Logi Tune (or G HUB for C922 / StreamCam, "
            "Logitech Capture for older models). Turn off auto exposure / autofocus there, set the "
            "values, close the app, then rerun this command. Settings stay while the camera is plugged in."
        )
    if plat.name == "linux":
        dev = f"/dev/video{cam.get('index', 0)}"
        return (
            f"Lock {items} with v4l2-ctl (package v4l-utils). List controls: v4l2-ctl -d {dev} -l. Then: "
            f"v4l2-ctl -d {dev} -c auto_exposure=1 -c exposure_time_absolute={int(exposure)} -c gain={int(gain)} "
            f"-c focus_automatic_continuous=0 -c focus_absolute={int(cam.get('focus', 0))} "
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
    """Owns the VideoCapture. A daemon thread grabs continuously and keeps only the newest frame.
    Property reads and writes share a lock with the grab, so they are safe while frames stream."""

    def __init__(self, cap: cv2.VideoCapture, cam_cfg: dict, plat: Platform, backend_name: str):
        self._cap = cap
        self._cap_lock = threading.Lock()
        self.cfg = cam_cfg
        self.platform = plat
        self.backend_name = backend_name
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
            with self._cap_lock:
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

    def fresh_frame(self, skip: int = SETTLE_FRAMES) -> np.ndarray:
        """A frame grabbed at least `skip` frames from now, i.e. after a property change took effect."""
        with self._cond:
            target = self._frame_id + skip
            if not self._cond.wait_for(lambda: self._frame_id >= target or not self._running, 3.0):
                raise CameraError("camera stopped delivering frames")
            if self.error:
                raise CameraError(self.error)
            return self._frame

    # properties
    def get(self, prop: int) -> float:
        with self._cap_lock:
            return float(self._cap.get(prop))

    def set(self, prop: int, value: float) -> bool:
        with self._cap_lock:
            try:
                return bool(self._cap.set(prop, value))
            except cv2.error:
                return False

    @property
    def exposure(self) -> float:
        return self.get(cv2.CAP_PROP_EXPOSURE)

    @property
    def gain(self) -> float:
        return self.get(cv2.CAP_PROP_GAIN)

    def set_manual_exposure(self, exposure: float, gain: float | None = None) -> dict[str, PropResult]:
        """Auto exposure off, then exactly these values. Returns what the driver read back."""
        auto_ok = self.set(cv2.CAP_PROP_AUTO_EXPOSURE, self.platform.auto_exposure_off)
        exp_ok = self.set(cv2.CAP_PROP_EXPOSURE, exposure)
        results = {
            "auto_exposure": PropResult("auto_exposure", self.platform.auto_exposure_off, auto_ok,
                                        self.get(cv2.CAP_PROP_AUTO_EXPOSURE), None),
        }
        readback = self.exposure
        results["exposure"] = PropResult("exposure", exposure, exp_ok, readback,
                                         exp_ok and abs(readback - exposure) <= max(0.5, abs(exposure) * 0.05))
        if gain is not None:
            gain_ok = self.set(cv2.CAP_PROP_GAIN, gain)
            readback = self.gain
            results["gain"] = PropResult("gain", gain, gain_ok, readback, gain_ok and abs(readback - gain) <= 1)
        return results

    def auto_then_lock(self, settle_s: float = AUTO_SETTLE_S) -> dict[str, PropResult]:
        """Let auto exposure settle on the current scene, then lock the exposure and gain it chose."""
        self.set(cv2.CAP_PROP_AUTO_EXPOSURE, self.platform.auto_exposure_on)
        log.info("auto exposure ON, settling for %.1f s ...", settle_s)
        time.sleep(settle_s)  # the reader thread keeps streaming, which is what drives auto exposure
        exposure, gain = self.exposure, self.gain
        log.info("auto exposure chose exposure %g, gain %g; locking those values", exposure, gain)
        return self.set_manual_exposure(exposure, gain)

    def step(self, prop: int, delta: float) -> float:
        """Nudge exposure or gain by delta (auto exposure stays off). Returns the value read back."""
        if prop == cv2.CAP_PROP_EXPOSURE:
            self.set(cv2.CAP_PROP_AUTO_EXPOSURE, self.platform.auto_exposure_off)
        self.set(prop, self.get(prop) + delta)
        return self.get(prop)

    def check_brightness(self) -> float:
        """Mean brightness of a frame taken after the last change; warns if it is too dark or too bright."""
        level = mean_brightness(self.fresh_frame())
        if level < DARK or level > BRIGHT:
            what = "too dark" if level < DARK else "too bright"
            log.warning(
                "WARNING: image is %s after locking (mean brightness %.0f, want %d to %d). Run "
                "python -m vision.camera --preview and adjust exposure with [ and ] (gain with - and =), "
                "then press s to save.", what, level, DARK, BRIGHT,
            )
        return level

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


def _set(cap: cv2.VideoCapture, prop: int, value: float) -> bool:
    try:
        return bool(cap.set(prop, value))
    except cv2.error:
        return False


def open_camera(cfg: dict) -> Camera:
    """Open config camera.index at camera.width x camera.height and camera.fps, lock exposure (auto then
    lock when camera.exposure is null), gain and focus as configured, read every property back and log
    what actually applied. Accepts the whole config.json dict or just its "camera" section. Raises
    CameraError if the camera cannot be opened."""
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

    ok, _ = cap.read()
    if not ok:
        cap.release()
        raise CameraError(f"Camera index {index} opened but returned no frame. Try another index (--list).")

    # raw values before we touch anything, so the backend's real units are visible
    log.info("OS %s, OpenCV capture backend %s (camera index %d)", plat.name, backend_name, index)
    log.info(
        "  as opened: CAP_PROP_EXPOSURE %g  CAP_PROP_AUTO_EXPOSURE %g  CAP_PROP_GAIN %g  CAP_PROP_BRIGHTNESS %g",
        cap.get(cv2.CAP_PROP_EXPOSURE), cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
        cap.get(cv2.CAP_PROP_GAIN), cap.get(cv2.CAP_PROP_BRIGHTNESS),
    )

    camera = Camera(cap, cam_cfg, plat, backend_name)
    camera._thread.start()
    deadline = time.monotonic() + FIRST_FRAME_TIMEOUT_S
    while camera.frame_size == (0, 0):
        if camera.error or time.monotonic() > deadline:
            camera.close()
            raise CameraError(camera.error or f"Camera index {index} delivered no frames in {FIRST_FRAME_TIMEOUT_S:.0f} s.")
        time.sleep(0.02)

    try:
        camera.props = _apply_exposure_focus(camera, cam_cfg)
        _report(camera, width, height, fps)
    except CameraError:
        camera.close()
        raise
    return camera


def _apply_exposure_focus(camera: Camera, cam: dict) -> dict[str, PropResult]:
    results: dict[str, PropResult] = {}
    if cam.get("auto_exposure", True):
        camera.set(cv2.CAP_PROP_AUTO_EXPOSURE, camera.platform.auto_exposure_on)
    elif cam.get("exposure") is None:
        results.update(camera.auto_then_lock())
    else:
        results.update(camera.set_manual_exposure(float(cam["exposure"]),
                                                  None if cam.get("gain") is None else float(cam["gain"])))

    if not cam.get("autofocus", True):
        target = float(cam.get("focus", 0))
        af_ok = camera.set(cv2.CAP_PROP_AUTOFOCUS, 0)
        focus_ok = camera.set(cv2.CAP_PROP_FOCUS, target)
        readback = camera.get(cv2.CAP_PROP_FOCUS)
        results["autofocus"] = PropResult("autofocus", 0, af_ok, camera.get(cv2.CAP_PROP_AUTOFOCUS), None)
        # Logitech focus moves in steps of 5
        results["focus"] = PropResult("focus", target, focus_ok, readback, focus_ok and abs(readback - target) <= 5)
    else:
        camera.set(cv2.CAP_PROP_AUTOFOCUS, 1)
    return results


def _report(camera: Camera, width: int, height: int, fps: float) -> None:
    """Log every property as read back from the driver, and warn about anything that did not take."""
    plat, cam_cfg = camera.platform, camera.cfg
    actual_w, actual_h = camera.frame_size
    log.info("  resolution  requested %dx%d  actual %dx%d", width, height, actual_w, actual_h)
    log.info("  fps         requested %g  driver reports %g", fps, camera.get(cv2.CAP_PROP_FPS))
    log.info("  fourcc      %s", _fourcc_str(camera.get(cv2.CAP_PROP_FOURCC)))
    log.info("  auto_exp    %g   exposure %g   gain %g   brightness %g",
             camera.get(cv2.CAP_PROP_AUTO_EXPOSURE), camera.exposure, camera.gain,
             camera.get(cv2.CAP_PROP_BRIGHTNESS))
    log.info("  autofocus   %g   focus    %g", camera.get(cv2.CAP_PROP_AUTOFOCUS), camera.get(cv2.CAP_PROP_FOCUS))
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
    failed = [name for name in ("exposure", "gain", "focus") if name in camera.props and not camera.props[name].applied]
    if failed:
        log.warning("WARNING: %s did NOT lock. Lighting changes or refocusing will disturb detection.", " and ".join(failed))
        log.warning("  %s", lock_help(plat, cam_cfg, failed, camera.exposure, camera.gain))
    elif camera.props:
        log.info("  exposure/focus locked as requested")
    if "exposure" in camera.props:
        log.info("  mean brightness %.0f (0 to 255)", camera.check_brightness())


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


PREVIEW_KEYS = "[ ] exposure  - = gain  a auto+lock  s save  q quit"


def _draw_lines(img: np.ndarray, lines: list[str], y0: int = 30, color: tuple[int, int, int] = (0, 255, 0)) -> None:
    for i, text in enumerate(lines):
        y = y0 + i * 30
        cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)


def preview(cfg: dict, config_path: Path = CONFIG_PATH) -> None:
    window = "SpeedMart camera preview"
    with open_camera(cfg) as camera:
        cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
        focus = camera.props.get("focus")
        focus_text = "" if focus is None else f"  focus {'locked' if focus.applied else 'NOT locked'}"
        message, message_until = "", 0.0
        exposure, gain = camera.exposure, camera.gain
        print(f"Preview running. Keys: {PREVIEW_KEYS}")
        while True:
            frame, frame_id, _ = camera.read()
            view = frame.copy()
            h, w = view.shape[:2]
            level = mean_brightness(frame)
            ok_level = DARK <= level <= BRIGHT
            _draw_lines(view, [
                f"{w}x{h}  {camera.measured_fps:5.1f} fps  frame {frame_id}",
                f"exposure {exposure:g}  gain {gain:g}{focus_text}",
            ])
            _draw_lines(view, [f"brightness {level:5.1f}" + ("" if ok_level else "  adjust with [ ]")], 90,
                        (0, 255, 0) if ok_level else (0, 0, 255))
            _draw_lines(view, [PREVIEW_KEYS], h - 15, (255, 255, 255))
            if message and time.monotonic() < message_until:
                _draw_lines(view, [message], 125, (0, 255, 255))
            cv2.imshow(window, view)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key in (ord("["), ord("]")):
                exposure = camera.step(cv2.CAP_PROP_EXPOSURE, -1 if key == ord("[") else 1)
                message = f"exposure {exposure:g}"
            elif key in (ord("-"), ord("=")):
                gain = camera.step(cv2.CAP_PROP_GAIN, -1 if key == ord("-") else 1)
                message = f"gain {gain:g}"
            elif key == ord("a"):
                _draw_lines(view, ["auto exposure settling ..."], 125, (0, 255, 255))
                cv2.imshow(window, view)
                cv2.waitKey(1)
                camera.auto_then_lock()
                exposure, gain = camera.exposure, camera.gain
                level = camera.check_brightness()
                message = f"locked exposure {exposure:g} gain {gain:g} (brightness {level:.0f})"
            elif key == ord("s"):
                values = {"exposure": _num(exposure), "gain": _num(gain)}
                save_camera_settings(values, config_path)
                message = f"saved exposure {values['exposure']} gain {values['gain']} to {config_path.name}"
            else:
                continue
            print(message)
            message_until = time.monotonic() + 4.0
        print(f"Last measured FPS: {camera.measured_fps:.1f}")
    cv2.destroyAllWindows()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vision.camera", description="Camera tools for the shelf cam.")
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
