"""Combine tags + YOLO into per-bay SKU counts (9.4, F13).

Per bay, per SKU:

    tag_count  = number of unit tags of that SKU in the bay
    yolo_count = number of YOLO boxes of that SKU (conf >= yolo_conf) whose center is in the bay
    fused      = max(tag_count, yolo_count)

Tags can never overcount; YOLO fills in when a tag is covered or glared. Without YOLO, fused = tag_count.
The backend applies the same rule to the snapshot (backend/shelf_state.py); the worker uses this module
for the overlay and the tests check that both agree.
"""

from __future__ import annotations

from typing import Iterable, Mapping


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
