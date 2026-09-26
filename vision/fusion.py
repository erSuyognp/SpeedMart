"""Combine tags + YOLO into per-bay SKU counts (9.4, F13).

Per bay, per SKU:

    tag_count  = number of unit tags of that SKU in the bay
    yolo_count = number of YOLO boxes of that SKU (conf >= yolo_conf) whose center is in the bay
    fused      = max(tag_count, yolo_count)

Tags can never overcount; YOLO fills in when a tag is covered or glared. Without YOLO, fused = tag_count.
The backend applies the same rule to the snapshot (backend/shelf_state.py); the worker uses this module
for the overlay and the tests check that both agree.

config.json vision.mode picks how the shelf is counted (configured_mode / effective_mode):

    "tags"    ArUco tags only; YOLO is never loaded.
    "yolo"    YOLO only, no tags on the products: ArUco detection is skipped, counts, stability, misplaced
              items and evidence come from YOLO boxes. Needs features.yolo and a working model, else the
              worker falls back to tags with a warning.
    "fusion"  tags + YOLO with the rule above (the default). With features.yolo off it is tags only.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

VISION_MODES = ("tags", "yolo", "fusion")
DEFAULT_MODE = "fusion"

log = logging.getLogger("vision.fusion")


def configured_mode(config: dict) -> str:
    """vision.mode from config.json ("fusion" when missing). Raises ValueError for anything else."""
    mode = str(config.get("vision", {}).get("mode", DEFAULT_MODE))
    if mode not in VISION_MODES:
        raise ValueError(f"config.json vision.mode {mode!r} is not one of {', '.join(VISION_MODES)}")
    return mode


def effective_mode(config: dict, yolo_available: bool) -> str:
    """The mode the worker runs in: "yolo" and "fusion" need features.yolo and a loaded model, else "tags".
    Logs a warning when "yolo" was asked for and tags have to stand in (the shelf would otherwise be blind)."""
    mode = configured_mode(config)
    yolo_on = bool(config.get("features", {}).get("yolo", False))
    if mode == "tags":
        return "tags"
    if not yolo_on or not yolo_available:
        if mode == "yolo":
            why = "features.yolo is false" if not yolo_on else "the YOLO model is unavailable"
            log.warning("WARNING: vision.mode is \"yolo\" but %s. Running tags only until that is fixed.", why)
        return "tags"
    return mode


def unit_skus(catalog: dict) -> dict[int, str]:
    """tag_id -> sku from catalog.json units."""
    return {int(u["tag_id"]): u["sku"] for u in catalog["units"]}


def tag_counts(units: Iterable[int], unit_sku: Mapping[int, str]) -> dict[str, int]:
    """SKU counts of the unit tags in one bay. Unknown tag ids are ignored."""
    counts: dict[str, int] = {}
    for tag in units:
        sku = unit_sku.get(int(tag))
        if sku is not None:
            counts[sku] = counts.get(sku, 0) + 1
    return counts


def fuse_bay(tags: Mapping[str, int], yolo: Mapping[str, int] | None) -> dict[str, int]:
    """max(tag_count, yolo_count) per SKU; SKUs with a fused count of 0 are left out."""
    if not yolo:
        return {sku: n for sku, n in tags.items() if n > 0}
    fused = {sku: max(int(tags.get(sku, 0)), int(yolo.get(sku, 0))) for sku in set(tags) | set(yolo)}
    return {sku: n for sku, n in fused.items() if n > 0}


def fuse(per_bay_units: Mapping[int, Iterable[int]], per_bay_yolo: Mapping[int, Mapping[str, int]] | None,
         unit_sku: Mapping[int, str]) -> dict[int, dict[str, int]]:
    """Fused SKU counts for every bay. per_bay_yolo None means YOLO is off (tags only)."""
    return {
        bay_id: fuse_bay(tag_counts(units, unit_sku), None if per_bay_yolo is None else per_bay_yolo.get(bay_id, {}))
        for bay_id, units in per_bay_units.items()
    }


def shelf_totals(fused: Mapping[int, Mapping[str, int]]) -> dict[str, int]:
    """Sum of fused counts over bays (the backend's shelf_now for those bays)."""
    total: dict[str, int] = {}
    for counts in fused.values():
        for sku, n in counts.items():
            total[sku] = total.get(sku, 0) + n
    return total
