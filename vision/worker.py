"""Vision main loop (9.1): camera -> tags -> bays -> snapshot -> POST /internal/shelf.

    python -m vision.worker [--backend URL] [--no-window]

Every frame: detect tags, assign them to bays, update per bay status, draw the overlay. At
vision.snapshot_hz the newest snapshot (8.2) is handed to a background poster; the camera loop never
waits on HTTP. Keys (window mode): q quit, space pause/resume posting, c recalibration reminder.

Per bay motion and stability (9.2, 9.3) come in S2.3: they plug in as another BayTracker. Until then
AlwaysStable reports every bay stable with no motion.
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
from vision.camera import CameraError, open_camera
from vision.overlay import draw_overlay

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
CATALOG_PATH = ROOT / "catalog.json"
ENV_PATH = ROOT / ".env"

WINDOW = "SpeedMart shelf cam"
DEFAULT_BACKEND = "http://127.0.0.1:8000/internal/shelf"
HTTP_TIMEOUT_S = 0.5
LOG_EVERY_S = 5.0  # post failures are logged at most this often
STATUS_EVERY_S = 10.0  # periodic one line status in the console

log = logging.getLogger("vision.worker")


# --- per bay status (S2.3 plugs in here) -----------------------------------------------------


@dataclass(frozen=True)
class BayStatus:
    stable: bool
    motion: bool


class BayTracker(Protocol):
    def update(self, gray: np.ndarray, per_bay: Mapping[int, list[int]], now: float) -> dict[int, BayStatus]:
        """Status for every bay given this frame (grayscale, config pixels), its per bay tag ids and
        time.monotonic()."""
        ...


class AlwaysStable:
    """S2.2 stand in: every bay is stable and motion free."""

    def update(self, gray: np.ndarray, per_bay: Mapping[int, list[int]], now: float) -> dict[int, BayStatus]:
        return {bay_id: BayStatus(stable=True, motion=False) for bay_id in per_bay}


# --- snapshot (8.2) -----------------------------------------------------------------------------


def build_snapshot(
    ts_ms: int,
    frame_id: int,
    per_bay: Mapping[int, list[int]],
    status: Mapping[int, BayStatus],
    loose: list[int],
) -> dict:
    """The /internal/shelf body, field for field as in Section 8.2."""
    return {
        "ts": int(ts_ms),
        "frame_id": int(frame_id),
        "bays": [
            {
                "bay": int(bay_id),
                "stable": bool(status[bay_id].stable),
                "motion": bool(status[bay_id].motion),
                "units": [int(u) for u in units],
                "yolo_counts": {},
            }
            for bay_id, units in per_bay.items()
        ],
        "loose_units": [int(u) for u in loose],
    }


# --- background poster ------------------------------------------------------------------------


class SnapshotPoster:
    """Posts snapshots from a daemon thread. submit() never blocks: the queue holds one snapshot and a
    newer one replaces whatever has not been sent yet."""

    def __init__(self, url: str, token: str, timeout: float = HTTP_TIMEOUT_S):
        self.url = url
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
    tracker = tracker or AlwaysStable()
    interval = 1.0 / float(vision.get("snapshot_hz", 5))
    url = shelf_url(backend)

    try:
        camera = open_camera(config)
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1

    poster = SnapshotPoster(url, token)
    poster.start()
    log.info("posting snapshots to %s at %g Hz%s", url, 1.0 / interval, "" if show_window else " (no window)")
    fps_meter = FpsMeter()
    paused = False
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
                status = tracker.update(gray, per_bay, now)
                fps = fps_meter.tick(now)

                if not paused and now >= next_post:
                    poster.submit(build_snapshot(int(time.time() * 1000), frame_id, per_bay, status, loose))
                    next_post = max(next_post + interval, now)
                backend_ok, age_ms = poster.status()

                if now >= next_status:
                    next_status = now + STATUS_EVERY_S
                    state = "PAUSED" if paused else {True: "backend OK", False: "backend unreachable",
                                                     None: "no post yet"}[backend_ok]
                    log.info("%.1f fps (camera %.1f)  bays %s  loose %s  %s", fps, camera.measured_fps, dict(per_bay),
                             loose, state)

                if not show_window:
                    continue
                cv2.imshow(WINDOW, draw_overlay(
                    frame, bays=bays, sku_names=sku_names, detections=detections, per_bay=per_bay,
                    loose=loose, status=status, fps=fps, camera_fps=camera.measured_fps, backend_ok=backend_ok,
                    last_post_age_ms=age_ms, paused=paused,
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
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        poster.stop()
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
