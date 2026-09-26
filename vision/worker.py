"""Vision main loop (9.1): camera -> tags -> bays -> snapshot -> POST /internal/shelf.

    python -m vision.worker [--backend URL] [--no-window]

Every frame: detect tags, assign them to bays, update per bay status, draw the overlay. At
vision.snapshot_hz the newest snapshot (8.2) is handed to a background poster; the camera loop never
waits on HTTP.

Keys (window mode): q quit, space pause/resume posting, c recalibration reminder,
[ / ] motion_threshold -/+ 0.005, - / = motion_settle_ms -/+ 50, s save both into config.json.

Per bay motion (9.2, vision/motion.py) and stability (9.3, StabilityTracker below): a bay is stable when
it has been motion free for vision.motion_settle_ms and its contents held for vision.stable_ms
(additions) or vision.stable_remove_ms (removals). Unstable bays report their last stable contents.
features.motion_freeze false turns the motion part off; stability timing still applies.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np
from dotenv import load_dotenv

from vision.aruco_detect import TagDetector, assign_to_bays
from vision.camera import CameraError, open_camera, save_section_settings
from vision.clips import ClipRecorder, clips_url
from vision.evidence import EvidenceRecorder, evidence_url
from vision.motion import MotionDetector
from vision.overlay import draw_overlay
from vision.yolo_detect import YoloDetector, load_yolo

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
CATALOG_PATH = ROOT / "catalog.json"
ENV_PATH = ROOT / ".env"

WINDOW = "SpeedMart shelf cam"
DEFAULT_BACKEND = "http://127.0.0.1:8000/internal/shelf"
HTTP_TIMEOUT_S = 0.5
LOG_EVERY_S = 5.0  # post failures are logged at most this often
STATUS_EVERY_S = 10.0  # periodic one line status in the console
DEFAULT_STABLE_REMOVE_MS = 700
TIME_EPS_MS = 1e-3  # float slack so a hold of exactly stable_ms counts

log = logging.getLogger("vision.worker")


# --- per bay status (S2.3 plugs in here) -----------------------------------------------------


@dataclass(frozen=True)
class BayStatus:
    stable: bool
    motion: bool
    # What the snapshot reports for this bay: the last stable contents (7.3). None = the current tags.
    units: tuple[int, ...] | None = None
    yolo_counts: Mapping[str, int] | None = None
    # Overlay details. changed_fraction is None when motion freeze is off.
    changed_fraction: float | None = None
    settle_left_ms: int = 0
    pending_units: tuple[int, ...] | None = None  # candidate contents not confirmed yet
    pending_yolo: Mapping[str, int] | None = None
    held_ms: int = 0  # how long the candidate has held
    need_ms: int = 0  # how long it must hold (stable_ms, or stable_remove_ms for a removal)


class BayTracker(Protocol):
    def update(self, gray: np.ndarray, per_bay: Mapping[int, list[int]], now: float | None = None,
               yolo_counts: Mapping[int, Mapping[str, int]] | None = None) -> dict[int, BayStatus]:
        """Status for every bay given this frame (grayscale, config pixels), its per bay tag ids, the
        time in seconds (time.monotonic() when None) and, with YOLO on, per bay SKU counts."""
        ...


class AlwaysStable:
    """Every bay stable and motion free (the S2.2 behavior)."""

    def update(self, gray: np.ndarray, per_bay: Mapping[int, list[int]], now: float | None = None,
               yolo_counts: Mapping[int, Mapping[str, int]] | None = None) -> dict[int, BayStatus]:
        return {bay_id: BayStatus(stable=True, motion=False,
                                  yolo_counts=None if yolo_counts is None else dict(yolo_counts.get(bay_id, {})))
                for bay_id in per_bay}


@dataclass(frozen=True)
class BayContent:
    """What stability compares (9.3): sorted unit ids, plus sorted (sku, count) pairs when YOLO is on."""
    units: tuple[int, ...]
    yolo: tuple[tuple[str, int], ...] = ()

    def yolo_dict(self) -> dict[str, int]:
        return dict(self.yolo)


def is_removal(old: BayContent | None, new: BayContent) -> bool:
    """True when new has lost anything old had: a unit tag, or part of a YOLO count. A swap counts as a
    removal, so it waits for the longer hold."""
    if old is None:
        return False
    if set(old.units) - set(new.units):
        return True
    new_yolo = new.yolo_dict()
    return any(new_yolo.get(sku, 0) < n for sku, n in old.yolo)


class StabilityTracker:
    """Per bay stability (9.3) with motion freeze (9.2) and asymmetric confirmation:

        content = (sorted unit ids, yolo counts if on)
        if content != candidate: candidate = content; candidate_since = now
        need = stable_remove_ms if content lost anything vs the last stable content, else stable_ms
        stable = motion_free AND now - candidate_since >= need

    A removal (the pick, which puts an item in the cart) must hold longer than an addition, so a tag
    briefly hidden by glare or a finger never reaches the cart. Unstable bays report the last stable
    contents. motion=None (features.motion_freeze false) makes every bay motion free.
    """

    def __init__(self, bay_ids, stable_ms: float, stable_remove_ms: float,
                 motion: MotionDetector | None = None, clock=time.monotonic):
        self.bay_ids = [int(b) for b in bay_ids]
        self.stable_ms = float(stable_ms)
        self.stable_remove_ms = float(stable_remove_ms)
        self.motion = motion
        self.clock = clock
        self._candidate: dict[int, BayContent] = {}
        self._since: dict[int, float] = {}
        self._last_stable: dict[int, BayContent] = {}

    @classmethod
    def from_config(cls, config: dict, clock=time.monotonic) -> StabilityTracker:
        vision = config["vision"]
        motion_on = bool(config.get("features", {}).get("motion_freeze", True))
        return cls(
            [b["id"] for b in config["bays"]],
            stable_ms=vision.get("stable_ms", 400),
            stable_remove_ms=vision.get("stable_remove_ms", DEFAULT_STABLE_REMOVE_MS),
            motion=MotionDetector.from_config(config) if motion_on else None,
            clock=clock,
        )

    def update(self, gray: np.ndarray, per_bay: Mapping[int, list[int]], now: float | None = None,
               yolo_counts: Mapping[int, Mapping[str, int]] | None = None) -> dict[int, BayStatus]:
        now = self.clock() if now is None else now
        motion = self.motion.update(gray, now) if self.motion is not None else {}
        out: dict[int, BayStatus] = {}
        for bay_id in self.bay_ids:
            yolo = () if yolo_counts is None else tuple(sorted(
                (sku, int(n)) for sku, n in yolo_counts.get(bay_id, {}).items() if int(n) > 0))
            content = BayContent(tuple(sorted(int(u) for u in per_bay.get(bay_id, []))), yolo)
            if content != self._candidate.get(bay_id):
                self._candidate[bay_id] = content
                self._since[bay_id] = now
            last = self._last_stable.get(bay_id)
            need = self.stable_remove_ms if is_removal(last, content) else self.stable_ms
            held = (now - self._since[bay_id]) * 1000.0 + TIME_EPS_MS
            m = motion.get(bay_id)
            motion_free = m is None or m.motion_free
            stable = motion_free and held >= need
            if stable:
                self._last_stable[bay_id] = last = content
            pending = content != last
            reported = last or BayContent(())
            out[bay_id] = BayStatus(
                stable=stable,
                motion=bool(m and m.motion),
                units=reported.units,
                yolo_counts=None if yolo_counts is None else reported.yolo_dict(),
                changed_fraction=None if m is None else m.changed_fraction,
                settle_left_ms=0 if m is None else m.settle_left_ms,
                pending_units=content.units if pending else None,
                pending_yolo=(content.yolo_dict() if pending and yolo_counts is not None else None),
                held_ms=int(held),
                need_ms=int(need),
            )
        return out


# --- live tuning (worker window keys) -----------------------------------------------------------

THRESHOLD_STEP = 0.005
SETTLE_STEP_MS = 50
TUNING_KEYS = "[ ] motion thr  - = settle ms  s save"


def apply_tuning_key(key: int, motion: MotionDetector | None) -> str | None:
    """[ ] change motion_threshold by 0.005, - = change motion_settle_ms by 50. Returns a message, or
    None when the key is not a tuning key."""
    if key not in (ord("["), ord("]"), ord("-"), ord("=")):
        return None
    if motion is None:
        return "motion freeze is off (features.motion_freeze false): nothing to tune"
    if key in (ord("["), ord("]")):
        step = THRESHOLD_STEP if key == ord("]") else -THRESHOLD_STEP
        motion.threshold = round(min(1.0, max(THRESHOLD_STEP, motion.threshold + step)), 4)
        return f"motion_threshold {motion.threshold:g}"
    step = SETTLE_STEP_MS if key == ord("=") else -SETTLE_STEP_MS
    motion.settle_ms = float(max(0, int(motion.settle_ms) + step))
    return f"motion_settle_ms {int(motion.settle_ms)}"


def save_motion_tuning(motion: MotionDetector | None, path: Path | None = None) -> str:
    """Write vision.motion_threshold and vision.motion_settle_ms into config.json, keeping every other key."""
    if motion is None:
        return "motion freeze is off: nothing to save"
    path = path or CONFIG_PATH
    values = {"motion_threshold": round(motion.threshold, 4), "motion_settle_ms": int(motion.settle_ms)}
    save_section_settings("vision", values, path)
    return f"saved motion_threshold {values['motion_threshold']:g} motion_settle_ms {values['motion_settle_ms']} to {path.name}"


# --- YOLO (F13) ---------------------------------------------------------------------------------


def start_yolo(config: dict, catalog: dict, loader=load_yolo) -> tuple[YoloDetector | None, str]:
    """(detector or None, overlay status text). None when features.yolo is false, or when ultralytics or
    the model file is missing (loader logs one warning); the worker then runs exactly as with YOLO off."""
    if not config.get("features", {}).get("yolo", False):
        return None, ""
    detector = loader(config, catalog)
    if detector is None:
        return None, "YOLO unavailable (see log), tags only"
    return detector, f"YOLO on ({detector.device}, every {detector.every_n})"


# --- snapshot (8.2) -----------------------------------------------------------------------------


def build_snapshot(
    ts_ms: int,
    frame_id: int,
    per_bay: Mapping[int, list[int]],
    status: Mapping[int, BayStatus],
    loose: list[int],
) -> dict:
    """The /internal/shelf body, field for field as in Section 8.2. A bay reports status.units (its last
    stable contents) when the tracker provides them, else the tags seen in this frame. yolo_counts is
    filled only when YOLO is on (status.yolo_counts not None), else {}."""
    bays = []
    for bay_id, units in per_bay.items():
        st = status[bay_id]
        reported = units if st.units is None else st.units
        bays.append({
            "bay": int(bay_id),
            "stable": bool(st.stable),
            "motion": bool(st.motion),
            "units": [int(u) for u in reported],
            "yolo_counts": {str(k): int(v) for k, v in (st.yolo_counts or {}).items()},
        })
    return {
        "ts": int(ts_ms),
        "frame_id": int(frame_id),
        "bays": bays,
        "loose_units": [int(u) for u in loose],
    }


# --- background poster ------------------------------------------------------------------------


class SnapshotPoster:
    """Posts snapshots from a daemon thread. submit() never blocks: the queue holds one snapshot and a
    newer one replaces whatever has not been sent yet."""

    def __init__(self, url: str, token: str, timeout: float = HTTP_TIMEOUT_S, on_session=None):
        self.url = url
        self._on_session = on_session  # gets the reply's "session_id" (8.2) after every successful post
        self._headers = {"X-Internal-Token": token}
        self._timeout = timeout
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._ok: bool | None = None  # None until the first post finishes
        self._last_ok: float | None = None  # time.monotonic() of the last successful post
        self._last_log = -LOG_EVERY_S
        self._thread = threading.Thread(target=self._run, name="snapshot-poster", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, snapshot: dict | None) -> None:
        while True:
            try:
                self._queue.put_nowait(snapshot)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()  # drop the stale one
                except queue.Empty:
                    pass

    def stop(self) -> None:
        self.submit(None)
        if self._thread.is_alive():
            self._thread.join(timeout=2 * self._timeout + 1)

    def status(self) -> tuple[bool | None, int | None]:
        """(last post succeeded, ms since the last successful post)."""
        with self._lock:
            age = None if self._last_ok is None else int((time.monotonic() - self._last_ok) * 1000)
            return self._ok, age

    def _record(self, ok: bool, error: str = "") -> None:
        now = time.monotonic()
        with self._lock:
            was_ok = self._ok
            self._ok = ok
            if ok:
                self._last_ok = now
        if ok and was_ok is False:
            log.info("backend reachable again at %s", self.url)
        elif not ok and now - self._last_log >= LOG_EVERY_S:
            self._last_log = now
            log.warning("snapshot post failed: %s", error)

    def _run(self) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            while True:
                snapshot = self._queue.get()
                if snapshot is None:
                    return
                try:
                    resp = client.post(self.url, json=snapshot, headers=self._headers)
                except httpx.HTTPError as exc:
                    self._record(False, f"{type(exc).__name__} posting to {self.url}")
                    continue
                if resp.status_code == 401:
                    self._record(False, "401: INTERNAL_TOKEN in .env does not match the backend's")
                elif resp.is_success:
                    self._record(True)
                    if self._on_session is not None:
                        try:
                            self._on_session(resp.json().get("session_id"))
                        except ValueError:
                            self._on_session(None)
                else:
                    self._record(False, f"HTTP {resp.status_code}: {resp.text[:200]}")


# --- config ----------------------------------------------------------------------------------


def shelf_url(backend: str) -> str:
    """Accept a full URL or just the backend origin (http://host:8000)."""
    backend = backend.rstrip("/")
    return backend if urlsplit(backend).path else backend + "/internal/shelf"


def load_token() -> str:
    load_dotenv(ENV_PATH)
    return os.getenv("INTERNAL_TOKEN", "").strip()


class FpsMeter:
    def __init__(self, window: int = 30):
        self._stamps: deque[float] = deque(maxlen=window)

    def tick(self, now: float) -> float:
        self._stamps.append(now)
        if len(self._stamps) < 2:
            return 0.0
        span = self._stamps[-1] - self._stamps[0]
        return (len(self._stamps) - 1) / span if span > 0 else 0.0


# --- main loop -------------------------------------------------------------------------------


def run(backend: str, show_window: bool, tracker: BayTracker | None = None) -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    vision = config["vision"]
    bays = config["bays"]
    width, height = int(config["camera"]["width"]), int(config["camera"]["height"])
    sku_names = {s["sku"]: s["name"] for s in catalog["skus"]}
    token = load_token()
    if not token:
        print("INTERNAL_TOKEN is not set in .env; the backend would reject every snapshot.", file=sys.stderr)
        return 1

    detector = TagDetector(vision["aruco_dict"], (u["tag_id"] for u in catalog["units"]),
                           clahe=bool(vision.get("clahe", True)))
    tracker = tracker or StabilityTracker.from_config(config)
    yolo, yolo_status = start_yolo(config, catalog)
    motion = getattr(tracker, "motion", None)
    interval = 1.0 / float(vision.get("snapshot_hz", 5))
    url = shelf_url(backend)

    try:
        camera = open_camera(config)
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1

    # F20 cart disputes: bay crops for the shopping session the backend names in each reply (vision/evidence.py),
    # and event clips around every stable change for the dispute review (vision/clips.py).
    disputes_on = bool(config.get("features", {}).get("disputes", False))
    evidence = EvidenceRecorder(bays, ROOT / "data" / "evidence", evidence_url(url), token) if disputes_on else None
    clips = ClipRecorder(bays, ROOT / "data" / "evidence", clips_url(url), token) if disputes_on else None

    def on_session(session_id):
        if evidence is not None:
            evidence.set_session(session_id)
        if clips is not None:
            clips.set_session(session_id)

    poster = SnapshotPoster(url, token, on_session=on_session if disputes_on else None)
    poster.start()
    log.info("posting snapshots to %s at %g Hz%s", url, 1.0 / interval, "" if show_window else " (no window)")
    fps_meter = FpsMeter()
    paused = False
    message, message_until = "", 0.0
    next_post = next_status = 0.0
    size_warned = False
    try:
        with camera:
            if show_window:
                cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
            while True:
                frame, frame_id, _ = camera.read()
                if frame.shape[1] != width or frame.shape[0] != height:
                    if not size_warned:
                        log.warning("camera frame is %dx%d, resizing to config %dx%d so ROIs line up",
                                    frame.shape[1], frame.shape[0], width, height)
                        size_warned = True
                    frame = cv2.resize(frame, (width, height))
                now = time.monotonic()
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                detections = detector.detect(gray)
                per_bay, loose = assign_to_bays(detections, bays)
                yolo_result = yolo.update(frame) if yolo is not None else None
                status = tracker.update(gray, per_bay, now,
                                        yolo_counts=None if yolo_result is None else yolo_result.per_bay)
                fps = fps_meter.tick(now)
                ts_ms = int(time.time() * 1000)
                if evidence is not None:
                    crops = evidence.update(frame, status, detections, ts_ms=ts_ms, per_bay=per_bay)
                    clips.update(frame, status, crops, ts_ms)

                if not paused and now >= next_post:
                    poster.submit(build_snapshot(ts_ms, frame_id, per_bay, status, loose))
                    next_post = max(next_post + interval, now)
                backend_ok, age_ms = poster.status()

                if now >= next_status:
                    next_status = now + STATUS_EVERY_S
                    state = "PAUSED" if paused else {True: "backend OK", False: "backend unreachable",
                                                     None: "no post yet"}[backend_ok]
                    unstable = [b for b, st in status.items() if not st.stable]
                    log.info("%.1f fps (camera %.1f)  bays %s  unstable %s  loose %s  %s", fps, camera.measured_fps,
                             dict(per_bay), unstable or "-", loose, state)

                if not show_window:
                    continue
                cv2.imshow(WINDOW, draw_overlay(
                    frame, bays=bays, sku_names=sku_names, detections=detections, per_bay=per_bay,
                    loose=loose, status=status, fps=fps, camera_fps=camera.measured_fps, backend_ok=backend_ok,
                    last_post_age_ms=age_ms, paused=paused, motion=motion,
                    yolo_boxes=None if yolo_result is None else yolo_result.boxes, yolo_status=yolo_status,
                    message=message if now < message_until else "",
                ))
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord(" "):
                    paused = not paused
                    log.info("posting %s", "PAUSED" if paused else "resumed")
                elif key == ord("c"):
                    print("To recalibrate bays: quit this worker (q), run  python -m vision.calibrate  "
                          "then start the worker again.")
                elif key == ord("s"):
                    message, message_until = save_motion_tuning(motion), now + 4.0
                    log.info(message)
                else:
                    tuned = apply_tuning_key(key, motion)
                    if tuned:
                        message, message_until = tuned, now + 4.0
                        log.info(tuned)
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        poster.stop()
        if evidence is not None:
            evidence.stop()
        if clips is not None:
            clips.stop()
        if show_window:
            cv2.destroyAllWindows()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vision.worker", description="SpeedMart shelf vision worker.")
    parser.add_argument("--backend", default=DEFAULT_BACKEND,
                        help=f"shelf snapshot URL or backend origin (default {DEFAULT_BACKEND})")
    parser.add_argument("--no-window", action="store_true", help="headless: no overlay window, Ctrl+C to stop")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per post at 5 Hz drowns everything else
    return run(args.backend, show_window=not args.no_window)


if __name__ == "__main__":
    sys.exit(main())
