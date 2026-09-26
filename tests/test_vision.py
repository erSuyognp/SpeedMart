"""S2.2 tests: tag detection, bay assignment, snapshot shape, poster queue. No camera needed."""

from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from vision.aruco_detect import Detection, TagDetector, assign_to_bays
from vision.worker import AlwaysStable, BayStatus, SnapshotPoster, build_snapshot, shelf_url

ROOT = Path(__file__).resolve().parent.parent
BAYS = [
    {"id": 0, "roi": [0, 0, 100, 100]},
    {"id": 1, "roi": [100, 0, 200, 100]},  # shares the x = 100 edge with bay 0
    {"id": 2, "roi": [300, 0, 400, 100]},
]
KNOWN = [u["tag_id"] for u in json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))["units"]]


def det(tag_id: int, x: float, y: float) -> Detection:
    return Detection(tag_id, (x, y), np.zeros((4, 2), np.float32))


# --- assign_to_bays ---------------------------------------------------------------------------


def test_inside_goes_to_its_bay_sorted():
    per_bay, loose = assign_to_bays([det(1, 50, 50), det(0, 10, 90), det(4, 350, 50)], BAYS)
    assert per_bay == {0: [0, 1], 1: [], 2: [4]}
    assert loose == []


def test_outside_every_roi_is_loose():
    per_bay, loose = assign_to_bays([det(3, 250, 50), det(2, 50, 150), det(5, -5, 50)], BAYS)
    assert per_bay == {0: [], 1: [], 2: []}
    assert loose == [2, 3, 5]


def test_edges_are_half_open():
    # left/top edges belong to the bay, right/bottom edges do not
    per_bay, loose = assign_to_bays([det(0, 0, 0), det(1, 100, 50), det(2, 200, 50), det(3, 50, 100)], BAYS)
    assert per_bay[0] == [0]
    assert per_bay[1] == [1]  # on the shared edge: exactly one bay
    assert loose == [2, 3]


def test_every_bay_present_when_empty():
    per_bay, loose = assign_to_bays([], BAYS)
    assert per_bay == {0: [], 1: [], 2: []} and loose == []


# --- detector ---------------------------------------------------------------------------------


def _scene(ids: list[int]) -> np.ndarray:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    img = np.full((300, 200 * len(ids) + 100), 255, np.uint8)
    for i, tag_id in enumerate(ids):
        x = 100 + 200 * i
        img[100:200, x:x + 100] = cv2.aruco.generateImageMarker(dictionary, tag_id, 100)
    return img


@pytest.mark.parametrize("clahe", [True, False])
def test_detects_known_tags_with_centers(clahe):
    found = TagDetector("DICT_4X4_50", KNOWN, clahe=clahe).detect(_scene([0, 3]))
    assert sorted(d.tag_id for d in found) == [0, 3]
    centers = {d.tag_id: d.center for d in found}
    assert centers[0] == pytest.approx((150, 150), abs=1.5)
    assert centers[3] == pytest.approx((350, 150), abs=1.5)
    assert all(d.corners.shape == (4, 2) for d in found)


def test_unknown_tag_ids_ignored():
    assert 40 not in KNOWN
    found = TagDetector("DICT_4X4_50", KNOWN).detect(_scene([40, 2]))
    assert [d.tag_id for d in found] == [2]


def test_bad_dictionary_name_rejected():
    with pytest.raises(ValueError):
        TagDetector("DICT_NOPE", KNOWN)


# --- snapshot (8.2) ---------------------------------------------------------------------------


def test_snapshot_matches_section_8_2():
    per_bay = {0: [0, 1], 1: [], 2: [3, 4, 5]}
    status = AlwaysStable().update(np.zeros((10, 10), np.uint8), per_bay, time.monotonic())
    snap = build_snapshot(1727222400123, 18422, per_bay, status, [7])

    assert set(snap) == {"ts", "frame_id", "bays", "loose_units"}
    assert type(snap["ts"]) is int and type(snap["frame_id"]) is int
    assert snap["loose_units"] == [7]
    assert [b["bay"] for b in snap["bays"]] == [0, 1, 2]
    for bay in snap["bays"]:
        assert set(bay) == {"bay", "stable", "motion", "units", "yolo_counts"}
        assert type(bay["bay"]) is int
        assert bay["stable"] is True and bay["motion"] is False
        assert all(type(u) is int for u in bay["units"])
        assert bay["yolo_counts"] == {}
    assert snap["bays"][2]["units"] == [3, 4, 5]
    json.dumps(snap)  # serialisable as is


def test_snapshot_accepted_by_backend_model():
    from backend.shelf_state import ShelfSnapshot

    per_bay = {0: [0], 1: [2, 3], 2: []}
    status = {0: BayStatus(True, False), 1: BayStatus(False, True), 2: BayStatus(True, False)}
    snap = build_snapshot(1, 2, per_bay, status, [])
    parsed = ShelfSnapshot.model_validate(snap)
    assert parsed.model_dump() == snap


def test_numpy_ids_become_plain_ints():
    snap = build_snapshot(np.int64(5), np.int64(6), {0: [np.int32(1)]}, {0: BayStatus(True, False)}, [np.int32(2)])
    assert type(snap["bays"][0]["units"][0]) is int and type(snap["loose_units"][0]) is int
    json.dumps(snap)


# --- poster -----------------------------------------------------------------------------------


def test_poster_queue_keeps_only_newest():
    poster = SnapshotPoster("http://127.0.0.1:9/internal/shelf", "t")  # thread not started
    for i in range(5):
        poster.submit({"frame_id": i})
    assert poster._queue.qsize() == 1
    assert poster._queue.get_nowait() == {"frame_id": 4}


def test_poster_unreachable_backend_never_blocks_submit():
    poster = SnapshotPoster("http://127.0.0.1:9/internal/shelf", "t")
    poster.start()
    try:
        t0 = time.monotonic()
        for i in range(200):
            poster.submit({"frame_id": i})
        assert time.monotonic() - t0 < 0.1
        deadline = time.monotonic() + 3
        while poster.status()[0] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert poster.status() == (False, None)
    finally:
        poster.stop()


def test_shelf_url():
    assert shelf_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/internal/shelf"
    assert shelf_url("http://10.0.0.5:8000/") == "http://10.0.0.5:8000/internal/shelf"
    assert shelf_url("http://127.0.0.1:8000/internal/shelf") == "http://127.0.0.1:8000/internal/shelf"
