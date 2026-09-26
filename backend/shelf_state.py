"""Latest shelf snapshot from vision, per-bay stable contents (7.3), and POST /internal/shelf (8.2).

Counting follows config.json vision.mode (settings.counting_mode): "tags" counts unit tags, "fusion" takes
max(tag_count, yolo_count) per bay per SKU (9.4), "yolo" counts YOLO boxes only (no tags on the products).
Misplaced items come from tags in the first two modes and from YOLO counts in the last (a SKU counted in a
bay that is not its home bay).
"""

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


def counting_mode() -> str:
    """"tags" | "yolo" | "fusion": vision.mode, reduced to "tags" while features.yolo is off."""
    return settings.counting_mode


def sku_home_bay(sku: str) -> int | None:
    """The bay a SKU belongs in: the bay configured for it, else the home_bay of its first unit."""
    bay = next((b.id for b in settings.bays if b.sku == sku), None)
    if bay is None:
        bay = next((u.home_bay for u in settings.units.values() if u.sku == sku), None)
    return bay


def _counts_locked() -> dict[str, int]:
    counts = {sku: 0 for sku in settings.skus}
    mode = counting_mode()
    for bay_id, units in _bay_units.items():
        tag_counts: dict[str, int] = {}
        for tag in units:
            sku = settings.units[tag].sku
            tag_counts[sku] = tag_counts.get(sku, 0) + 1
        yolo = _bay_yolo.get(bay_id, {})
        if mode == "fusion":
            # 9.4 fusion: per bay, per SKU, max(tag_count, yolo_count).
            for sku in settings.skus:
                counts[sku] += max(tag_counts.get(sku, 0), yolo.get(sku, 0))
        elif mode == "yolo":
            for sku, n in yolo.items():
                counts[sku] += n
        else:
            for sku, n in tag_counts.items():
                counts[sku] += n
    return counts


def _misplaced_locked() -> list[dict[str, Any]]:
    out = []
    if counting_mode() == "yolo":
        # No tags: a SKU counted in a bay that is not its home bay is misplaced (it stays "on shelf").
        for bay_id in sorted(_bay_yolo):
            for sku, n in sorted(_bay_yolo[bay_id].items()):
                home = sku_home_bay(sku)
                if n > 0 and home is not None and home != bay_id:
                    out.append({"tag_id": None, "sku": sku, "name": settings.skus[sku].name,
                                "bay": bay_id, "home_bay": home, "count": int(n)})
        return out
    for bay_id in sorted(_bay_units):
        for tag in sorted(_bay_units[bay_id]):
            unit = settings.units[tag]
            if unit.home_bay != bay_id:
                out.append({"tag_id": tag, "sku": unit.sku, "name": settings.skus[unit.sku].name,
                            "bay": bay_id, "home_bay": unit.home_bay, "count": 1})
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
            _bay_yolo[report.bay] = {k: int(v) for k, v in report.yolo_counts.items()
                                     if k in settings.skus and int(v) > 0}
        _loose_units[:] = [t for t in snap.loose_units if t in settings.units]
        _last_snapshot_at = time.monotonic()
        _last_frame_id = snap.frame_id
        after = (_counts_locked(), _misplaced_locked())
    changed = before != after
    if changed:
        eventlog.log("shelf_changed", frame_id=snap.frame_id, shelf=after[0],
                     misplaced=[{"tag_id": m["tag_id"], "sku": m["sku"], "bay": m["bay"]} for m in after[1]])
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
        mode = counting_mode()
        return {b: (bool(u) and mode != "yolo") or (mode != "tags" and any(_bay_yolo.get(b, {}).values()))
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
            "mode": counting_mode(),
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
    from backend import kiosk_agent, store  # local import: store imports this module
    if changed:
        store.on_shelf_change()
    session = store.current_session()
    try:  # kiosk agent: motion at the shelf while the store is free may offer the tour (public, no data)
        kiosk_agent.note_shelf_motion(any(b.motion for b in snap.bays), occupied=session is not None)
    except Exception as e:  # decoration; never let it cost a snapshot
        eventlog.log("kiosk_error", where="shelf_activity", error=repr(e)[:300])
    # Cart disputes (8.13): the worker saves shelf crops for the shopping session named here, none when null.
    shopping = settings.features.disputes and session is not None and session["state"] in store.SHOPPING_STATES
    return {"ok": True, "changed": changed, "session_id": session["id"] if shopping else None}
