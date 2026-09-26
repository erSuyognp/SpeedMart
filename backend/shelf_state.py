"""Latest shelf snapshot from vision, per-bay stable contents (7.3), and POST /internal/shelf (8.2)."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from backend import eventlog
from backend.settings import settings


class BayReport(BaseModel):
    bay: int
    stable: bool
    motion: bool = False
    units: list[int] = []
    yolo_counts: dict[str, int] = {}


class ShelfSnapshot(BaseModel):
    ts: int
    frame_id: int
    bays: list[BayReport]
    loose_units: list[int] = []


_lock = threading.Lock()
# bay id -> last stable contents. Unstable reports never touch these (7.3).
_bay_units: dict[int, set[int]] = {}
_bay_yolo: dict[int, dict[str, int]] = {}
_loose_units: list[int] = []
_bay_status: dict[int, dict[str, bool]] = {}  # latest report per bay, stable or not (admin view only)
_last_snapshot_at: float | None = None  # time.monotonic() of the last accepted snapshot
_last_frame_id: int | None = None


def reset() -> None:
    """Forget everything, as after a backend restart."""
    global _last_snapshot_at, _last_frame_id
    with _lock:
        _bay_units.clear()
        _bay_yolo.clear()
        for b in settings.bays:
            _bay_units[b.id] = set()
            _bay_yolo[b.id] = {}
        _loose_units.clear()
        _bay_status.clear()
        _last_snapshot_at = None
        _last_frame_id = None


reset()


def _counts_locked() -> dict[str, int]:
    counts = {sku: 0 for sku in settings.skus}
    for bay_id, units in _bay_units.items():
        tag_counts: dict[str, int] = {}
        for tag in units:
            sku = settings.units[tag].sku
            tag_counts[sku] = tag_counts.get(sku, 0) + 1
        if settings.features.yolo:
            # 9.4 fusion: per bay, per SKU, max(tag_count, yolo_count).
            yolo = _bay_yolo.get(bay_id, {})
            for sku in settings.skus:
                counts[sku] += max(tag_counts.get(sku, 0), yolo.get(sku, 0))
        else:
            for sku, n in tag_counts.items():
                counts[sku] += n
    return counts


def _misplaced_locked() -> list[dict[str, Any]]:
    out = []
    for bay_id in sorted(_bay_units):
        for tag in sorted(_bay_units[bay_id]):
            unit = settings.units[tag]
            if unit.home_bay != bay_id:
                out.append({"tag_id": tag, "sku": unit.sku, "name": settings.skus[unit.sku].name,
                            "bay": bay_id, "home_bay": unit.home_bay})
    return out


def apply_snapshot(snapshot: dict[str, Any] | ShelfSnapshot) -> bool:
    """Update stable bays only. Returns True if shelf counts or misplaced units changed."""
    global _last_snapshot_at, _last_frame_id
    snap = snapshot if isinstance(snapshot, ShelfSnapshot) else ShelfSnapshot.model_validate(snapshot)
    with _lock:
        before = (_counts_locked(), _misplaced_locked())
        for report in snap.bays:
            if report.bay in _bay_units:
                _bay_status[report.bay] = {"stable": report.stable, "motion": report.motion}
            if report.bay not in _bay_units or not report.stable:
                continue
            units = {t for t in report.units if t in settings.units}  # ignore unknown tag ids (9.1)
            # A unit can only be in one place: if another bay still holds it from an older stable
            # reading (e.g. that bay is unstable right now), the newest stable sighting wins.
            for other_id, other_units in _bay_units.items():
                if other_id != report.bay:
                    other_units -= units
            _bay_units[report.bay] = units
            _bay_yolo[report.bay] = {k: int(v) for k, v in report.yolo_counts.items() if k in settings.skus}
        _loose_units[:] = [t for t in snap.loose_units if t in settings.units]
        _last_snapshot_at = time.monotonic()
        _last_frame_id = snap.frame_id
        after = (_counts_locked(), _misplaced_locked())
    changed = before != after
    if changed:
        eventlog.log("shelf_changed", frame_id=snap.frame_id, shelf=after[0],
                     misplaced=[{"tag_id": m["tag_id"], "bay": m["bay"]} for m in after[1]])
    return changed


def shelf_counts() -> dict[str, int]:
    with _lock:
        return _counts_locked()


def misplaced() -> list[dict[str, Any]]:
    with _lock:
        return _misplaced_locked()


def bay_occupancy() -> dict[int, bool]:
    """bay id -> True if its last stable contents hold at least one unit."""
    with _lock:
        return {b: bool(u) or (settings.features.yolo and any(_bay_yolo.get(b, {}).values()))
                for b, u in _bay_units.items()}


def has_snapshot() -> bool:
    with _lock:
        return _last_snapshot_at is not None


def last_snapshot_age_ms() -> int:
    """Milliseconds since the last snapshot, or -1 if none has arrived since startup."""
    with _lock:
        if _last_snapshot_at is None:
            return -1
        return int((time.monotonic() - _last_snapshot_at) * 1000)


def state() -> dict[str, Any]:
    """Full shelf view for admin/debug."""
    with _lock:
        return {
            "bays": {b: sorted(u) for b, u in _bay_units.items()},
            "bay_status": {b: dict(_bay_status.get(b, {"stable": None, "motion": None})) for b in _bay_units},
            "yolo_counts": {b: dict(y) for b, y in _bay_yolo.items()},
            "loose_units": list(_loose_units),
            "counts": _counts_locked(),
            "misplaced": _misplaced_locked(),
            "frame_id": _last_frame_id,
        }


router = APIRouter()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


@router.post("/internal/shelf")
async def post_shelf(request: Request):
    token = request.headers.get("X-Internal-Token", "")
    expected = settings.env.internal_token
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        return _error(401, "bad_internal_token", "Missing or wrong X-Internal-Token.")
    try:
        snap = ShelfSnapshot.model_validate(await request.json())
    except (ValueError, ValidationError) as e:
        return _error(422, "bad_snapshot", f"Snapshot body is invalid: {e}")
    changed = apply_snapshot(snap)
    if changed:
        from backend import store  # local import: store imports this module
        store.on_shelf_change()
    return {"ok": True, "changed": changed}
