"""Shelf photos for cart disputes (8.13, F20 `disputes`): bay crops, never the full frame.

The backend names the shopping session in every /internal/shelf reply ("session_id", null when nobody is
shopping). When a session starts, EvidenceRecorder saves a JPEG crop of each bay ROI (the baseline) as soon as
that bay is stable; afterwards, whenever a bay's stable contents change, it saves a new crop of that bay. Files go
to data/evidence/<session_id>/bay<id>_<UTC time>_<kind>.jpg and a small notice is posted to /internal/evidence
(X-Internal-Token) with the unit tags in the crop and their boxes, so the dispute screen can outline the
missing unit. Encoding, writing and posting run on a background thread; the camera loop only copies the crop.
The backend deletes the photos when the visit is over.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import cv2
import httpx
import numpy as np

log = logging.getLogger("vision.evidence")
JPEG_QUALITY = 80
QUEUE_SIZE = 64  # pending crops; beyond this new ones are dropped (the loop never waits)
HTTP_TIMEOUT_S = 1.0


def evidence_url(shelf_url: str) -> str:
    """http://host:8000/internal/shelf -> http://host:8000/internal/evidence"""
    parts = urlsplit(shelf_url)
    return urlunsplit((parts.scheme, parts.netloc, "/internal/evidence", "", ""))


def file_name(bay: int, ts_ms: int, kind: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(ts_ms / 1000)) + f"{int(ts_ms) % 1000:03d}Z"
    return f"bay{int(bay)}_{stamp}_{kind}.jpg"


@dataclass
class Crop:
    session_id: str
    bay: int
    kind: str  # "baseline" | "change"
    image: np.ndarray
    ts_ms: int
    units: list[int]
    tags: dict[str, list[float]]  # tag id -> [x, y, w, h] in crop pixels


def roi_box(roi: list[int], frame_shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    """[x1, y1, x2, y2] clamped to the frame."""
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = (int(v) for v in roi)
    return max(0, min(w, x1)), max(0, min(h, y1)), max(0, min(w, x2)), max(0, min(h, y2))


def tag_boxes(detections: Iterable[Any], box: tuple[int, int, int, int]) -> dict[str, list[float]]:
    """Bounding box of every detected tag whose center is in the crop, in crop pixels."""
    x1, y1, x2, y2 = box
    out = {}
    for det in detections:
        cx, cy = det.center
        if not (x1 <= cx < x2 and y1 <= cy < y2):
            continue
        pts = np.asarray(det.corners, dtype=float).reshape(-1, 2)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        out[str(int(det.tag_id))] = [round(float(lo[0]) - x1, 1), round(float(lo[1]) - y1, 1),
                                     round(float(hi[0] - lo[0]), 1), round(float(hi[1] - lo[1]), 1)]
    return out


class EvidenceRecorder:
    """Decides which bay crops to keep (camera thread) and saves + announces them (background thread)."""

    def __init__(self, bays: list[dict], root: Path, url: str, token: str,
                 send: Callable[[Crop, Path], None] | None = None, start: bool = True):
        self.bays = {int(b["id"]): list(b["roi"]) for b in bays}
        self.root = Path(root)
        self.url = url
        self._headers = {"X-Internal-Token": token}
        self._lock = threading.Lock()
        self._wanted: str | None = None  # session id from the latest /internal/shelf reply
        self.session_id: str | None = None  # the session crops are being saved for
        self._baseline_pending: set[int] = set()
        self._saved: dict[int, tuple[int, ...]] = {}
        self._send = send or self._post
        self._queue: queue.Queue[Crop | None] = queue.Queue(maxsize=QUEUE_SIZE)
        self._client: httpx.Client | None = None
        self._thread = threading.Thread(target=self._run, name="evidence", daemon=True)
        if start:
            self._thread.start()

    def set_session(self, session_id: str | None) -> None:
        """Called from the snapshot poster with the backend's answer."""
        with self._lock:
            self._wanted = session_id or None

    def update(self, frame: np.ndarray, status: Mapping[int, Any], detections: Iterable[Any],
               ts_ms: int | None = None, per_bay: Mapping[int, list[int]] | None = None) -> list[Crop]:
        """One camera frame. status: BayStatus per bay (its units = the last stable contents; a tracker
        without them falls back to per_bay, this frame's tags). Returns the crops queued for saving."""
        with self._lock:
            wanted = self._wanted
        if wanted != self.session_id:
            self.session_id = wanted
            self._saved.clear()
            self._baseline_pending = set(self.bays) if wanted else set()
        if not self.session_id:
            return []
        ts_ms = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
        detections = list(detections)
        queued = []
        for bay, roi in self.bays.items():
            st = status.get(bay)
            if st is None or not st.stable:
                continue  # a hand in the bay, or contents still settling: wait for a clean picture
            reported = st.units if st.units is not None else (per_bay or {}).get(bay, ())
            units = tuple(sorted(int(u) for u in reported))
            if bay in self._baseline_pending:
                kind = "baseline"
            elif self._saved.get(bay) != units:
                kind = "change"
            else:
                continue
            box = roi_box(roi, frame.shape)
            x1, y1, x2, y2 = box
            if x2 <= x1 or y2 <= y1:
                continue
            crop = Crop(self.session_id, bay, kind, frame[y1:y2, x1:x2].copy(), ts_ms, list(units),
                        tag_boxes(detections, box))
            self._baseline_pending.discard(bay)
            self._saved[bay] = units
            try:
                self._queue.put_nowait(crop)
                queued.append(crop)
            except queue.Full:
                log.warning("evidence queue full, dropped a crop of bay %d", bay)
        return queued

    def save(self, crop: Crop) -> Path:
        folder = self.root / crop.session_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / file_name(crop.bay, crop.ts_ms, crop.kind)
        if not cv2.imwrite(str(path), crop.image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
            raise OSError(f"could not write {path}")
        return path

    def notice(self, crop: Crop, path: Path) -> dict:
        h, w = crop.image.shape[:2]
        return {"session_id": crop.session_id, "bay": crop.bay, "kind": crop.kind, "file": path.name,
                "ts": crop.ts_ms, "width": int(w), "height": int(h), "units": crop.units, "tags": crop.tags}

    def _post(self, crop: Crop, path: Path) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=HTTP_TIMEOUT_S)
        resp = self._client.post(self.url, json=self.notice(crop, path), headers=self._headers)
        if resp.status_code in (404, 409):  # session over, or disputes off: nobody will ask for this crop
            path.unlink(missing_ok=True)
        elif not resp.is_success:
            log.warning("evidence notice failed: HTTP %d %s", resp.status_code, resp.text[:200])

    def _run(self) -> None:
        while True:
            crop = self._queue.get()
            if crop is None:
                break
            try:
                path = self.save(crop)
                self._send(crop, path)
            except (OSError, httpx.HTTPError) as exc:
                log.warning("evidence crop of bay %d not saved/announced: %s", crop.bay, exc)
        if self._client is not None:
            self._client.close()

    def stop(self) -> None:
        try:
            self._queue.put(None, timeout=2)
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=3)
