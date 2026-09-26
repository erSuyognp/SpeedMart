"""Event clips for dispute review (8.14, F20 `disputes`): what the shelf looked like around every change.

A ring buffer keeps the last RING_SECONDS of frames at CLIP_FPS, cropped to the union of all bay ROIs plus a
small margin, never the full frame. When a bay's stable contents change during a shopping session (the same
"change" the EvidenceRecorder saves a crop for), the frames from PRE_MS before to POST_MS after the change become
one clip: an MP4 written with cv2.VideoWriter (mp4v, which works on Windows without extra codecs) plus KEYFRAMES
evenly spaced JPEG keyframes. Files go to data/evidence/<session_id>/clips/ and a notice goes to
POST /internal/clips (X-Internal-Token) with the bay, its SKU, the unit tags before and after, the timestamps and
the motion periods, so the review agent and the admin queue can find them. The backend deletes them with the
crops: at session close unless a dispute exists. Writing and posting run on a background thread; the camera loop
only copies the crop into the ring.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import cv2
import httpx
import numpy as np

log = logging.getLogger("vision.clips")

CLIP_FPS = 5.0
RING_SECONDS = 10.0
PRE_MS = 4000  # clip starts this long before the change
POST_MS = 2000  # and ends this long after it
KEYFRAMES = 6
MARGIN_PX = 24  # around the union of the bay ROIs
JPEG_QUALITY = 80
QUEUE_SIZE = 8  # pending clips; beyond this new ones are dropped (the loop never waits)
HTTP_TIMEOUT_S = 2.0
TIME_EPS_MS = 1.0  # a frame exactly one period after the last one is sampled


def clips_url(shelf_url: str) -> str:
    """http://host:8000/internal/shelf -> http://host:8000/internal/clips"""
    parts = urlsplit(shelf_url)
    return urlunsplit((parts.scheme, parts.netloc, "/internal/clips", "", ""))


def stamp(ts_ms: int) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(ts_ms / 1000)) + f"{int(ts_ms) % 1000:03d}Z"


def clip_name(bay: int, change_ts: int) -> str:
    return f"clip_bay{int(bay)}_{stamp(change_ts)}.mp4"


def keyframe_name(bay: int, change_ts: int, index: int) -> str:
    return f"clip_bay{int(bay)}_{stamp(change_ts)}_k{int(index)}.jpg"


def union_box(rois: Iterable[Iterable[int]], frame_shape: tuple[int, ...], margin: int = MARGIN_PX
              ) -> tuple[int, int, int, int]:
    """The smallest box holding every bay ROI plus `margin`, clamped to the frame: [x1, y1, x2, y2].
    Width and height are even: the mp4v encoder rounds odd sizes down, which would break the keyframe/video
    match and the outline percentages."""
    h, w = frame_shape[:2]
    rois = [list(map(int, r)) for r in rois]
    if not rois:
        x1, y1, x2, y2 = 0, 0, w, h
    else:
        x1 = max(0, min(w, min(r[0] for r in rois) - margin))
        y1 = max(0, min(h, min(r[1] for r in rois) - margin))
        x2 = max(0, min(w, max(r[2] for r in rois) + margin))
        y2 = max(0, min(h, max(r[3] for r in rois) + margin))
    if (x2 - x1) % 2:
        x2 -= 1
    if (y2 - y1) % 2:
        y2 -= 1
    return x1, y1, x2, y2


def keyframe_indexes(n_frames: int, count: int = KEYFRAMES) -> list[int]:
    """`count` evenly spaced frame indexes, first and last included; every frame when there are fewer."""
    if n_frames <= 0:
        return []
    if n_frames <= count:
        return list(range(n_frames))
    return sorted({round(i * (n_frames - 1) / (count - 1)) for i in range(count)})


def motion_periods(samples: Iterable["Sample"], bay: int, period_ms: float) -> list[tuple[int, int]]:
    """[(start_ms, end_ms)] runs of motion in `bay` over the samples (each run ends one period after its
    last moving sample, so a single moving frame still has a length)."""
    periods: list[list[int]] = []
    for s in samples:
        if not s.motion.get(bay, False):
            continue
        if periods and s.ts_ms - periods[-1][1] <= period_ms + TIME_EPS_MS:
            periods[-1][1] = s.ts_ms + int(period_ms)
        else:
            periods.append([s.ts_ms, s.ts_ms + int(period_ms)])
    return [(a, b) for a, b in periods]


@dataclass(frozen=True)
class Sample:
    ts_ms: int
    image: np.ndarray  # the union crop
    motion: dict[int, bool]  # bay -> motion in this frame


class FrameRing:
    """The last `seconds` of frames sampled at `fps` (frames arriving faster are skipped, never resized)."""

    def __init__(self, fps: float = CLIP_FPS, seconds: float = RING_SECONDS):
        self.fps = float(fps)
        self.period_ms = 1000.0 / self.fps
        self._frames: deque[Sample] = deque(maxlen=max(1, int(round(seconds * self.fps))))
        self._last_ts: int | None = None

    def push(self, ts_ms: int, image: np.ndarray, motion: Mapping[int, bool] | None = None) -> bool:
        """Keep this frame if a period has passed since the last kept one. Returns True when kept."""
        ts_ms = int(ts_ms)
        if self._last_ts is not None and ts_ms - self._last_ts < self.period_ms - TIME_EPS_MS:
            return False
        self._last_ts = ts_ms
        self._frames.append(Sample(ts_ms, image, dict(motion or {})))
        return True

    def window(self, start_ms: int, end_ms: int) -> list[Sample]:
        return [s for s in self._frames if start_ms <= s.ts_ms <= end_ms]

    @property
    def newest_ts(self) -> int | None:
        return self._frames[-1].ts_ms if self._frames else None

    def __len__(self) -> int:
        return len(self._frames)


@dataclass
class Pending:
    session_id: str
    bay: int
    sku: str
    change_ts: int
    end_ts: int
    units_before: list[int]
    units_after: list[int]


@dataclass
class Clip:
    session_id: str
    bay: int
    sku: str
    change_ts: int
    start_ts: int
    end_ts: int
    units_before: list[int]
    units_after: list[int]
    fps: float
    samples: list[Sample]
    motion: list[tuple[int, int]] = field(default_factory=list)

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.samples[0].image.shape[:2]
        return int(w), int(h)


class ClipRecorder:
    """Ring buffer on the camera thread; clip writing and the notice on a background thread."""

    def __init__(self, bays: list[dict], root: Path, url: str, token: str,
                 send: Callable[[Clip, Path, list[Path]], None] | None = None, start: bool = True,
                 fps: float = CLIP_FPS, seconds: float = RING_SECONDS, pre_ms: int = PRE_MS,
                 post_ms: int = POST_MS, margin: int = MARGIN_PX):
        self.bays = {int(b["id"]): list(b["roi"]) for b in bays}
        self.skus = {int(b["id"]): str(b.get("sku", "")) for b in bays}
        self.root = Path(root)
        self.url = url
        self.pre_ms, self.post_ms, self.margin = int(pre_ms), int(post_ms), int(margin)
        self.ring = FrameRing(fps, seconds)
        self._headers = {"X-Internal-Token": token}
        self._lock = threading.Lock()
        self._wanted: str | None = None
        self.session_id: str | None = None
        self._pending: dict[int, Pending] = {}
        self._units: dict[int, list[int]] = {}  # last known stable units per bay (from the evidence crops)
        self._reported: dict[int, list[int]] = {}  # what the tracker reported per bay on the previous frame
        self._box: tuple[int, int, int, int] | None = None
        self._send = send or self._post
        self._queue: queue.Queue[Clip | None] = queue.Queue(maxsize=QUEUE_SIZE)
        self._client: httpx.Client | None = None
        self._thread = threading.Thread(target=self._run, name="clips", daemon=True)
        if start:
            self._thread.start()

    def set_session(self, session_id: str | None) -> None:
        with self._lock:
            self._wanted = session_id or None

    def box(self, frame_shape: tuple[int, ...]) -> tuple[int, int, int, int]:
        if self._box is None:
            self._box = union_box(self.bays.values(), frame_shape, self.margin)
        return self._box

    def update(self, frame: np.ndarray, status: Mapping[int, Any], changes: Iterable[Any], ts_ms: int | None = None
               ) -> list[Clip]:
        """One camera frame. `changes` are the crops the EvidenceRecorder queued this frame (kind "baseline" or
        "change", with .bay, .units, .ts_ms). Returns the clips finished and queued for writing."""
        ts_ms = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
        with self._lock:
            wanted = self._wanted
        if wanted != self.session_id:  # session over or a new one: pending clips would be refused anyway
            self.session_id = wanted
            self._pending.clear()
            self._units.clear()
        x1, y1, x2, y2 = self.box(frame.shape)
        if x2 > x1 and y2 > y1:
            motion = {bay: bool(getattr(st, "motion", False)) for bay, st in status.items()}
            self.ring.push(ts_ms, frame[y1:y2, x1:x2].copy(), motion)
        previous = dict(self._reported)
        self._reported = {int(bay): [int(u) for u in st.units] for bay, st in status.items()
                          if getattr(st, "units", None) is not None}
        if not self.session_id:
            return []
        for crop in changes:
            bay = int(crop.bay)
            before = self._units.get(bay, previous.get(bay, []))
            after = [int(u) for u in crop.units]
            self._units[bay] = after
            if crop.kind != "change" or bay not in self.bays:
                continue
            pending = self._pending.get(bay)
            if pending is not None and int(crop.ts_ms) <= pending.end_ts:  # a second change inside the window
                pending.end_ts = int(crop.ts_ms) + self.post_ms
                pending.units_after = after
            else:
                self._pending[bay] = Pending(self.session_id, bay, self.skus.get(bay, ""), int(crop.ts_ms),
                                             int(crop.ts_ms) + self.post_ms, before, after)
        done = []
        for bay, pending in list(self._pending.items()):
            if ts_ms < pending.end_ts:
                continue
            del self._pending[bay]
            clip = self._finish(pending)
            if clip is None:
                continue
            try:
                self._queue.put_nowait(clip)
                done.append(clip)
            except queue.Full:
                log.warning("clip queue full, dropped a clip of bay %d", bay)
        return done

    def _finish(self, p: Pending) -> Clip | None:
        start_ts = p.change_ts - self.pre_ms
        samples = self.ring.window(start_ts, p.end_ts)
        if not samples:
            return None
        return Clip(p.session_id, p.bay, p.sku, p.change_ts, start_ts, p.end_ts, list(p.units_before),
                    list(p.units_after), self.ring.fps, samples, motion_periods(samples, p.bay, self.ring.period_ms))

    def save(self, clip: Clip) -> tuple[Path, list[Path]]:
        """Write the MP4 and its keyframes. Returns (mp4 path, keyframe paths)."""
        folder = self.root / clip.session_id / "clips"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / clip_name(clip.bay, clip.change_ts)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), clip.fps, clip.size)
        if not writer.isOpened():
            raise OSError(f"could not open {path} for writing (mp4v)")
        try:
            for s in clip.samples:
                writer.write(s.image)
        finally:
            writer.release()
        keys = []
        for i, index in enumerate(keyframe_indexes(len(clip.samples))):
            key = folder / keyframe_name(clip.bay, clip.change_ts, i)
            if not cv2.imwrite(str(key), clip.samples[index].image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
                raise OSError(f"could not write {key}")
            keys.append(key)
        return path, keys

    def notice(self, clip: Clip, path: Path, keys: list[Path]) -> dict:
        w, h = clip.size
        indexes = keyframe_indexes(len(clip.samples))
        return {
            "session_id": clip.session_id, "bay": clip.bay, "sku": clip.sku, "file": path.name,
            "keyframes": [{"id": f"k{i}", "file": k.name, "ts": clip.samples[index].ts_ms}
                          for i, (k, index) in enumerate(zip(keys, indexes))],
            "change_ts": clip.change_ts, "starts_ts": clip.samples[0].ts_ms, "ends_ts": clip.samples[-1].ts_ms,
            "units_before": clip.units_before, "units_after": clip.units_after,
            "motion": [[a, b] for a, b in clip.motion],
            "width": w, "height": h, "fps": clip.fps, "frames": len(clip.samples),
        }

    def _post(self, clip: Clip, path: Path, keys: list[Path]) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=HTTP_TIMEOUT_S)
        resp = self._client.post(self.url, json=self.notice(clip, path, keys), headers=self._headers)
        if resp.status_code in (404, 409):  # session over, or disputes off: nobody will ask for this clip
            for p in (path, *keys):
                p.unlink(missing_ok=True)
        elif not resp.is_success:
            log.warning("clip notice failed: HTTP %d %s", resp.status_code, resp.text[:200])

    def _run(self) -> None:
        while True:
            clip = self._queue.get()
            if clip is None:
                break
            try:
                path, keys = self.save(clip)
                self._send(clip, path, keys)
            except (OSError, httpx.HTTPError) as exc:
                log.warning("clip of bay %d not saved/announced: %s", clip.bay, exc)
        if self._client is not None:
            self._client.close()

    def stop(self) -> None:
        try:
            self._queue.put(None, timeout=2)
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)
