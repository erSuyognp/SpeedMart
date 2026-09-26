"""Tag free YOLO mode (config.json vision.mode "yolo"): YOLO counts drive the cart, misplaced items come from
counts, dispute outlines and the review timeline come from YOLO boxes, the worker skips ArUco and shows the mode,
the capture tool reminds about tags, the dev scripts post counts without units, and "tags" mode is unchanged.
No camera, no ultralytics: fake snapshots, fake boxes, temp data dir."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import db, disputes, settings as settings_mod, shelf_state, store
from backend.main import app
from backend.settings import SettingsError, effective_vision_mode
from identity_helpers import login_as, set_features
from test_cart import BAY_IDS, CATALOG, CONFIG, FULL, full_baseline, member_id, qty, snap, tmp_data  # noqa: F401
from test_returns import ENTRY, HEADERS, face_id
from training.capture import NO_TAGS_REMINDER, CaptureSession, draw_capture_overlay
from vision import fusion
from vision import yolo_detect as yd
from vision.clips import ClipRecorder
from vision.evidence import EvidenceRecorder, file_name
from vision.motion import MotionDetector
from vision.overlay import MODE_LABEL, draw_overlay, mode_line
from vision.worker import StabilityTracker, build_snapshot, empty_bays, start_yolo

pytestmark = pytest.mark.usefixtures("tmp_data")

ROOT = Path(__file__).resolve().parent.parent
BAYS = CONFIG["bays"]
ROI = {int(b["id"]): [int(v) for v in b["roi"]] for b in BAYS}
CLASS_SKU = yd.class_to_sku(CATALOG)
SKU_CLASS = {v: k for k, v in CLASS_SKU.items()}
HOME = {b["sku"]: int(b["id"]) for b in BAYS}
NAME = {s["sku"]: s["name"] for s in CATALOG["skus"]}
FRAME = np.zeros((720, 1280, 3), np.uint8)
NO_TAGS = {b: [] for b in BAY_IDS}


# --- helpers ---

def set_mode(monkeypatch, mode: str, yolo: bool = True) -> None:
    """vision.mode and features.yolo everywhere the backend keeps a settings reference (like set_features)."""
    current = sys.modules["backend.settings"].settings
    new = dataclasses.replace(current, vision={**current.vision, "mode": mode},
                              features=dataclasses.replace(current.features, yolo=yolo))
    for name, mod in list(sys.modules.items()):
        if name.startswith("backend") and getattr(mod, "settings", None) is not None \
                and not isinstance(getattr(mod, "settings"), type(sys)):
            monkeypatch.setattr(mod, "settings", new)
    assert settings_mod.settings.counting_mode == effective_vision_mode(mode, yolo)


def full_counts() -> dict[int, dict[str, int]]:
    """A perfect YOLO on a full shelf: every unit counted by SKU in its home bay."""
    counts: dict[int, dict[str, int]] = {b: {} for b in BAY_IDS}
    for u in CATALOG["units"]:
        bay = counts.setdefault(int(u["home_bay"]), {})
        bay[u["sku"]] = bay.get(u["sku"], 0) + 1
    return counts


def with_counts(**changes: dict[str, int]) -> dict[int, dict[str, int]]:
    counts = full_counts()
    for key, c in changes.items():
        counts[int(key.removeprefix("b"))] = dict(c)
    return counts


def yolo_snap(counts: dict[int, dict[str, int]], frame_id: int = 1, unstable: tuple[int, ...] = (),
              units: dict[int, list[int]] | None = None) -> dict:
    """What the worker posts in yolo mode: yolo_counts per bay, units empty (unless a test says otherwise)."""
    return {
        "ts": 1727222400123 + frame_id,
        "frame_id": frame_id,
        "bays": [{"bay": b, "stable": b not in unstable, "motion": b in unstable, "units": (units or {}).get(b, []),
                  "yolo_counts": counts.get(b, {})} for b in BAY_IDS],
        "loose_units": [],
    }


def push_counts(counts, **kw) -> dict:
    if shelf_state.apply_snapshot(yolo_snap(counts, **kw)):
        store.on_shelf_change()
    return store.current_cart()


def box(sku: str, conf: float, bay: int, fx: float = 0.5, fy: float = 0.5, half: float = 30) -> yd.RawBox:
    x1, y1, x2, y2 = ROI[bay]
    cx, cy = x1 + (x2 - x1) * fx, y1 + (y2 - y1) * fy
    return (SKU_CLASS[sku], conf, (cx - half, cy - half, cx + half, cy + half))


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def feed(tracker, clock, ms, per_bay, yolo, gray=None):
    out = None
    for _ in range(int(ms / 50)):
        clock.t += 0.05
        out = tracker.update(np.zeros((720, 1280), np.uint8) if gray is None else gray, per_bay, yolo_counts=yolo)
    return out


# --- config: vision.mode ---

def test_repo_config_defaults_to_fusion_and_counts_tags_while_yolo_is_off():
    assert CONFIG["vision"]["mode"] == "fusion"
    assert settings_mod.settings.vision_mode == "fusion"
    assert settings_mod.settings.counting_mode == "tags"  # features.yolo is false in the repo config
    assert fusion.configured_mode(CONFIG) == "fusion"
    assert fusion.effective_mode(CONFIG, yolo_available=True) == "tags"


def test_effective_mode_rules(caplog):
    assert effective_vision_mode("tags", True) == "tags"
    assert effective_vision_mode("fusion", True) == "fusion"
    assert effective_vision_mode("fusion", False) == "tags"
    assert effective_vision_mode("yolo", True) == "yolo"
    assert effective_vision_mode("yolo", False) == "tags"
    on = json.loads(json.dumps(CONFIG))
    on["features"]["yolo"] = True
    on["vision"]["mode"] = "yolo"
    assert fusion.effective_mode(on, yolo_available=True) == "yolo"
    with caplog.at_level(logging.WARNING, logger="vision.fusion"):
        assert fusion.effective_mode(on, yolo_available=False) == "tags"  # model missing: never a blind shelf
    assert any("vision.mode is \"yolo\"" in r.message and "unavailable" in r.message for r in caplog.records)
    on["vision"]["mode"] = "tags"
    assert fusion.effective_mode(on, yolo_available=True) == "tags"


def test_bad_mode_is_rejected_by_both_sides():
    bad = json.loads(json.dumps(CONFIG))
    bad["vision"]["mode"] = "hybrid"
    with pytest.raises(ValueError, match="hybrid"):
        fusion.configured_mode(bad)
    with pytest.raises(SettingsError, match="vision.mode"):
        settings_mod._build(settings_mod.settings.env, bad, CATALOG)
    missing = json.loads(json.dumps(CONFIG))
    del missing["vision"]["mode"]
    assert settings_mod._build(settings_mod.settings.env, missing, CATALOG).vision_mode == "fusion"


# --- 1. counts drive the cart ---

def test_yolo_counts_drive_the_cart(monkeypatch, member_id):
    set_mode(monkeypatch, "yolo")
    shelf_state.reset()
    assert shelf_state.apply_snapshot(yolo_snap(full_counts()))
    assert shelf_state.shelf_counts() == full_baseline()
    assert shelf_state.state()["mode"] == "yolo" and all(u == [] for u in shelf_state.state()["bays"].values())
    session = store.start_session(member_id)
    assert session["baseline"] == full_baseline()
    assert store.current_cart()["items"] == []

    cart = push_counts(with_counts(b0={"elx": 1}), frame_id=2)  # one hydration drink gone by count
    assert qty(cart, "elx") == 1 and len(cart["items"]) == 1 and cart["warnings"] == []
    cart = push_counts(with_counts(b0={"elx": 1}), frame_id=3)  # repeated frame: no double count
    assert qty(cart, "elx") == 1
    # a hand in the bay: the unstable bay's report is ignored, the last stable counts stay
    cart = push_counts(with_counts(b0={}), frame_id=4, unstable=(0,))
    assert qty(cart, "elx") == 1
    # units lists are ignored in yolo mode even if something sends them
    cart = push_counts(with_counts(b0={"elx": 1}), frame_id=5, units={0: [0, 1]})
    assert qty(cart, "elx") == 1
    cart = push_counts(full_counts(), frame_id=6)  # put back
    assert cart["items"] == [] and cart["total_usd"] == 0.0
    # picked from bay 0 and bay 2 at once
    cart = push_counts(with_counts(b0={"elx": 1}, b2={"bar": 1}), frame_id=7)
    assert qty(cart, "elx") == 1 and qty(cart, "bar") == 1
    assert shelf_state.bay_occupancy()[0] and not shelf_state.bay_occupancy()[0] is None


def test_stability_and_motion_freeze_work_from_counts_alone():
    """No tags ever: per_bay is empty for every bay; the counts are what must hold still."""
    clock = Clock()
    tr = StabilityTracker(BAY_IDS, 400, 700, None, clock=clock)
    per_bay = empty_bays(BAYS)
    assert per_bay == NO_TAGS
    st = feed(tr, clock, 450, per_bay, {0: {"elx": 2}})
    assert st[0].stable and st[0].units == () and st[0].yolo_counts == {"elx": 2}
    # a count drops: a removal, 700 ms, the bay keeps reporting 2 meanwhile
    st = feed(tr, clock, 650, per_bay, {0: {"elx": 1}})
    assert not st[0].stable and st[0].yolo_counts == {"elx": 2} and st[0].pending_yolo == {"elx": 1}
    assert st[0].need_ms == 700
    st = feed(tr, clock, 100, per_bay, {0: {"elx": 1}})
    assert st[0].stable and st[0].yolo_counts == {"elx": 1}
    snap_ = build_snapshot(1, 1, per_bay, st, [])
    assert snap_["bays"][0] == {"bay": 0, "stable": True, "motion": False, "units": [], "yolo_counts": {"elx": 1}}
    shelf_state.ShelfSnapshot.model_validate(snap_)

    # motion freeze: a moving blob in bay 0 keeps it unstable even while the counts hold
    motion = MotionDetector(BAYS, threshold=0.02, settle_ms=300, margin_px=0)
    tr = StabilityTracker(BAY_IDS, 400, 700, motion, clock=clock)
    x1, y1, x2, y2 = ROI[0]

    def blob(shift: int) -> np.ndarray:
        g = np.full((720, 1280), 100, np.uint8)
        g[y1 + 20 + shift:y1 + 120 + shift, x1 + 20:x1 + 120] = 250
        return g

    for i in range(20):  # 1 s of a hand moving in bay 0
        clock.t += 0.05
        st = tr.update(blob(40 * (i % 2)), per_bay, yolo_counts={0: {"elx": 2}})
    assert st[0].motion and not st[0].stable
    st = feed(tr, clock, 800, per_bay, {0: {"elx": 2}}, gray=blob(0))  # still: settle 300 + hold 400
    assert st[0].stable and st[0].yolo_counts == {"elx": 2}


def test_yolo_mode_end_to_end_from_boxes_to_backend_counts(monkeypatch):
    """Fake detector -> tracker -> snapshot -> backend in yolo mode, with the units lists empty throughout."""
    set_mode(monkeypatch, "yolo")
    shelf_state.reset()
    det = yd.YoloDetector(lambda frame: [box("elx", 0.9, 0, 0.3, 0.4), box("elx", 0.8, 0, 0.7, 0.6),
                                         box("rec", 0.9, 1), box("bar", 0.7, 2), box("bar", 0.5, 2, 0.8, 0.8)],
                          BAYS, CLASS_SKU, 0.55, 1)
    clock = Clock()
    tr = StabilityTracker(BAY_IDS, 400, 700, None, clock=clock)
    status = None
    for _ in range(10):
        clock.t += 0.05
        status = tr.update(np.zeros((720, 1280), np.uint8), empty_bays(BAYS), yolo_counts=det.update(FRAME).per_bay)
    snap_ = build_snapshot(1, 1, empty_bays(BAYS), status, [])
    assert all(b["units"] == [] for b in snap_["bays"])
    shelf_state.apply_snapshot(snap_)
    counts = shelf_state.shelf_counts()
    assert (counts["elx"], counts["rec"], counts["bar"]) == (2, 1, 1)  # the 0.5 bar is below yolo_conf
    assert shelf_state.misplaced() == []


# --- 2. misplaced from counts ---

def test_misplaced_item_from_counts(monkeypatch, member_id):
    set_mode(monkeypatch, "yolo")
    shelf_state.reset()
    shelf_state.apply_snapshot(yolo_snap(full_counts()))
    store.start_session(member_id)
    assert qty(push_counts(with_counts(b2={"bar": 1}), frame_id=2), "bar") == 1  # a bag of chips picked
    # put back into the hydration drink bay: on the shelf again, not in the cart, and a warning
    cart = push_counts(with_counts(b0={"elx": 2, "bar": 1}, b2={"bar": 1}), frame_id=3)
    assert cart["items"] == []
    assert cart["warnings"] == [{"kind": "misplaced", "sku": "bar", "name": NAME["bar"], "bay": 0, "tag_id": None,
                                 "message": f"{NAME['bar']} is in the wrong bay"}]
    assert shelf_state.misplaced() == [{"tag_id": None, "sku": "bar", "name": NAME["bar"], "bay": 0,
                                        "home_bay": HOME["bar"], "count": 1}]
    # with something in the cart the agent policy leads with the misplaced item, naming the printed card
    from backend import agent
    cart = push_counts(with_counts(b0={"elx": 1, "bar": 1}, b2={"bar": 1}), frame_id=4)
    assert qty(cart, "elx") == 1 and cart["warnings"][0]["sku"] == "bar"
    decision = agent.policy(cart)
    assert decision["kind"] == "misplaced" and decision["return_to_bay"] == HOME["bar"] + 1
    # back in its own bay: warning gone
    cart = push_counts(full_counts(), frame_id=5)
    assert cart["warnings"] == [] and cart["items"] == []


# --- tags mode unchanged; fusion unchanged ---

def test_tags_mode_ignores_yolo_counts_even_with_the_flag_on(monkeypatch, member_id):
    set_mode(monkeypatch, "tags", yolo=True)
    shelf_state.reset()
    # tags say full shelf; a (wrong) YOLO claims extra and misplaced items: none of it counts
    snapshot = snap(FULL)
    for b in snapshot["bays"]:
        b["yolo_counts"] = {"elx": 5, "bar": 3}
    shelf_state.apply_snapshot(snapshot)
    assert shelf_state.shelf_counts() == full_baseline()
    assert shelf_state.misplaced() == [] and shelf_state.state()["mode"] == "tags"
    store.start_session(member_id)
    picked = snap({**FULL, 0: [1]}, frame_id=2)
    for b in picked["bays"]:
        b["yolo_counts"] = {"elx": 2}  # YOLO would keep it on the shelf in fusion mode; not here
    shelf_state.apply_snapshot(picked)
    store.on_shelf_change()
    assert qty(store.current_cart(), "elx") == 1
    # yolo mode with features.yolo off is tags mode: the core loop never breaks
    set_mode(monkeypatch, "yolo", yolo=False)
    assert settings_mod.settings.counting_mode == "tags"
    assert shelf_state.shelf_counts() == full_baseline(elx=1)


def test_fusion_mode_still_takes_the_max(monkeypatch):
    set_mode(monkeypatch, "fusion", yolo=True)
    shelf_state.reset()
    snapshot = snap({**FULL, 0: [0]}, frame_id=1)  # tag 1 covered
    snapshot["bays"][0]["yolo_counts"] = {"elx": 2}
    shelf_state.apply_snapshot(snapshot)
    assert shelf_state.shelf_counts()["elx"] == 2 and shelf_state.state()["mode"] == "fusion"


# --- worker: no ArUco in yolo mode, the mode on the overlay ---

def test_start_yolo_follows_the_mode():
    calls = []

    def loader(config, catalog):
        calls.append(1)
        return yd.YoloDetector(lambda f: [], BAYS, CLASS_SKU, 0.55, 3, device="cpu")

    on = json.loads(json.dumps(CONFIG))
    on["features"]["yolo"] = True
    on["vision"]["mode"] = "tags"
    assert start_yolo(on, CATALOG, loader) == (None, "") and not calls  # tags mode never loads the model
    on["vision"]["mode"] = "yolo"
    det, status = start_yolo(on, CATALOG, loader)
    assert det is not None and status.startswith("YOLO only, tags off") and calls == [1]
    on["vision"]["mode"] = "fusion"
    det, status = start_yolo(on, CATALOG, loader)
    assert det is not None and status.startswith("YOLO on (")
    det, status = start_yolo(on, CATALOG, lambda c, k: None)
    assert det is None and "unavailable" in status


def test_overlay_names_the_mode_and_draws_boxes_in_every_yolo_mode():
    assert mode_line("yolo", 20.0).startswith("MODE yolo (no tags)")
    assert mode_line("fusion", 20.0, 30.0).startswith("MODE fusion") and "FPS 20.0" in mode_line("fusion", 20.0)
    assert mode_line("tags", 1.0).startswith("MODE tags")
    assert set(MODE_LABEL) == set(fusion.VISION_MODES)
    det = yd.YoloDetector(lambda f: [box("elx", 0.9, 0), box("bar", 0.9, 1)], BAYS, CLASS_SKU, 0.55, 1)
    result = det.update(FRAME)
    clock = Clock()
    tr = StabilityTracker(BAY_IDS, 400, 700, None, clock=clock)
    feed(tr, clock, 450, empty_bays(BAYS), result.per_bay)
    status = feed(tr, clock, 100, empty_bays(BAYS), {0: {"elx": 1}})  # a pending count in yolo mode
    common = dict(bays=BAYS, sku_names=NAME, detections=[], per_bay=empty_bays(BAYS), loose=[], status=status,
                  fps=20, backend_ok=True, last_post_age_ms=5, paused=False)
    views = {m: draw_overlay(FRAME, yolo_boxes=result.boxes, yolo_status="x", mode=m, **common)
             for m in fusion.VISION_MODES}
    for m, view in views.items():
        assert np.all(view == (255, 0, 255), axis=2).any(), m  # magenta boxes whenever YOLO boxes are given
    assert not np.array_equal(views["yolo"], views["fusion"])  # the mode line differs
    assert not FRAME.any()


# --- evidence: outlines and metadata from boxes ---

class Status:
    def __init__(self, stable: bool, yolo_counts=None, units=(), motion: bool = False):
        self.stable, self.units, self.yolo_counts, self.motion = stable, tuple(units), yolo_counts, motion


def test_worker_crops_change_on_counts_and_carry_boxes(tmp_path):
    rec = EvidenceRecorder(BAYS, tmp_path, "http://x/internal/evidence", "t", start=False)
    rec.set_session("ses_abc")
    det = yd.YoloDetector(lambda f: [box("elx", 0.9, 0, 0.3, 0.3), box("elx", 0.8, 0, 0.7, 0.7), box("rec", 0.9, 1)],
                          BAYS, CLASS_SKU, 0.55, 1)
    boxes = det.update(FRAME).boxes
    full = {b: Status(True, {}) for b in BAY_IDS}
    full[0], full[1] = Status(True, {"elx": 2}), Status(True, {"rec": 1})
    first = rec.update(FRAME, full, [], ts_ms=1000, per_bay=empty_bays(BAYS), yolo_boxes=boxes)
    assert [(c.bay, c.kind) for c in first][:2] == [(0, "baseline"), (1, "baseline")]
    crop0 = first[0]
    assert crop0.units == [] and crop0.tags == {} and crop0.yolo_counts == {"elx": 2}
    x1, y1, x2, y2 = ROI[0]
    w, h = x2 - x1, y2 - y1
    assert sorted(crop0.boxes) == ["elx"] and len(crop0.boxes["elx"]) == 2
    bx, by, bw, bh, conf = crop0.boxes["elx"][0]
    assert (bx, by, bw, bh, conf) == (round(w * 0.3 - 30, 1), round(h * 0.3 - 30, 1), 60.0, 60.0, 0.9)
    assert rec.update(FRAME, full, [], ts_ms=1100, per_bay=empty_bays(BAYS), yolo_boxes=boxes) == []  # unchanged
    # the units never change (there are none); a count change is a change
    after = dict(full)
    after[0] = Status(True, {"elx": 1})
    changed = rec.update(FRAME, after, [], ts_ms=1200, per_bay=empty_bays(BAYS), yolo_boxes=boxes[1:])
    assert [(c.bay, c.kind, c.units, c.yolo_counts) for c in changed] == [(0, "change", [], {"elx": 1})]
    assert len(changed[0].boxes["elx"]) == 1
    path = rec.save(changed[0])
    notice = rec.notice(changed[0], path)
    assert notice["units"] == [] and notice["tags"] == {} and notice["yolo_counts"] == {"elx": 1}
    assert notice["boxes"] == {"elx": [changed[0].boxes["elx"][0]]}
    assert disputes.EvidenceNotice.model_validate(notice)


class Change:
    def __init__(self, bay, kind, units, ts_ms, yolo_counts=None, session_id="ses_abc"):
        self.bay, self.kind, self.units, self.ts_ms, self.session_id = bay, kind, list(units), ts_ms, session_id
        self.yolo_counts = yolo_counts or {}


def test_clips_carry_counts_before_and_after(tmp_path):
    rec = ClipRecorder(BAYS, tmp_path, "http://x/internal/clips", "t", start=False)
    stable = {b: Status(True, {}) for b in BAY_IDS}
    rec.set_session("ses_abc")
    for ts in range(0, 3000, 200):
        rec.update(np.full((720, 1280, 3), 50, np.uint8), stable, [], ts)
    rec.update(FRAME, stable, [Change(0, "baseline", [], 3000, {"elx": 2})], 3000)
    for ts in range(3200, 10000, 200):
        rec.update(FRAME, stable, [], ts)
    rec.update(FRAME, stable, [Change(0, "change", [], 10000, {"elx": 1})], 10000)
    done = []
    for ts in range(10200, 12200, 200):
        done += rec.update(FRAME, stable, [], ts)
    assert len(done) == 1
    clip = done[0]
    assert (clip.units_before, clip.units_after) == ([], [])
    assert (clip.counts_before, clip.counts_after) == ({"elx": 2}, {"elx": 1})
    path, keys = rec.save(clip)
    notice = rec.notice(clip, path, keys)
    assert notice["counts_before"] == {"elx": 2} and notice["counts_after"] == {"elx": 1}


def yolo_crop(client: TestClient, sid: str, kind: str, counts: dict, boxes: dict, ts: int, bay: int = 0):
    """A crop as the worker announces it in yolo mode: no units, no tags, counts and boxes."""
    name = file_name(bay, ts, kind)
    folder = disputes.evidence_root() / sid
    folder.mkdir(parents=True, exist_ok=True)
    ok, jpg = cv2.imencode(".jpg", np.full((420, 220, 3), 128, np.uint8))
    (folder / name).write_bytes(jpg.tobytes())
    body = {"session_id": sid, "bay": bay, "kind": kind, "file": name, "ts": ts, "width": 220, "height": 420,
            "units": [], "tags": {}, "yolo_counts": counts, "boxes": boxes}
    r = client.post("/internal/evidence", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    return name


def enter_yolo(client: TestClient, mid: str, taken: dict[int, dict[str, int]]) -> str:
    assert client.post("/internal/shelf", json=yolo_snap(full_counts(), 1), headers=HEADERS).status_code == 200
    face_id(client, mid)
    r = client.post("/api/gate/enter", json=ENTRY)
    assert r.status_code == 200, r.text
    assert client.post("/internal/shelf", json=yolo_snap(taken, 2), headers=HEADERS).status_code == 200
    return r.json()["session"]["id"]


BOX_A = [20.0, 40.0, 60.0, 60.0, 0.91]  # the unit that goes missing
BOX_B = [120.0, 40.0, 60.0, 60.0, 0.87]


def test_dispute_outline_and_timeline_come_from_yolo_boxes(monkeypatch, member_id):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True, disputes=True)
    set_mode(monkeypatch, "yolo")
    from backend import admin, review
    monkeypatch.setattr(admin, "force_decline", False)
    monkeypatch.setattr(review, "schedule", review.run)  # no background thread
    shelf_state.reset()
    with TestClient(app) as client:
        sid = enter_yolo(client, member_id, with_counts(b0={"elx": 1}))
        assert qty(client.get("/api/store/current").json()["cart"], "elx") == 1
        yolo_crop(client, sid, "baseline", {"elx": 2}, {"elx": [BOX_A, BOX_B]}, 1_800_000_000_000)
        yolo_crop(client, sid, "change", {"elx": 1}, {"elx": [[121.0, 41.0, 59.0, 60.0, 0.9]]}, 1_800_000_005_000)
        rows = disputes.crops(sid, 0)
        assert rows[0]["yolo_counts"] == {"elx": 2} and rows[0]["boxes"]["elx"][0] == BOX_A and rows[0]["tags"] == {}

        r = client.post("/api/disputes", json={"sku": "elx"})
        assert r.status_code == 200, r.text
        d = r.json()["dispute"]
        assert d["outcome"] == "needs_review" and d["status"] == "open"
        ev = d["evidence"]
        assert ev["bay"] == 0 and ev["tag_id"] is None
        # the outline is the box that disappeared (BOX_A, least overlap with what is still there), as fractions
        want = {"x": round(20 / 220, 4), "y": round(40 / 420, 4), "w": round(60 / 220, 4), "h": round(60 / 420, 4)}
        assert ev["before"]["outline"] == want and ev["now"]["outline"] == want
        stored = json.loads(disputes._row(d["dispute_id"])["evidence_json"])
        assert stored["outline_from"] == "yolo" and (stored["count_before"], stored["count_now"]) == (2, 1)

        # the review timeline speaks counts, not tag ids
        inputs = review.gather(disputes._row(d["dispute_id"]))
        t = inputs["timeline"]
        assert t["detection"] == "yolo" and t["missing_tag_id"] is None and t["missing_count"] == 1
        assert (t["baseline_counts"], t["latest_counts"]) == ({"elx": 2}, {"elx": 1})
        assert [x["yolo_counts"] for x in t["tags_seen"]] == [{"elx": 2}, {"elx": 1}]
        assert all(x["tag_ids"] == [] for x in t["tags_seen"])
        assert [f["yolo_counts"] for f in inputs["frames"]] == [{"elx": 2}, {"elx": 1}]
        text, _ = review.compose(inputs, with_images=False)
        assert json.loads(text)["frames"][0]["yolo_counts"] == {"elx": 2}
        assert "no tags" in review.SYSTEM_PROMPT.lower()

        login_as(client, member_id, admin=True)
        admin_view = next(x for x in client.get("/admin/state").json()["disputes"] if x["dispute_id"] == d["dispute_id"])
        assert admin_view["timeline"]["detection"] == "yolo"
        assert admin_view["evidence"]["before"]["outline"] == want


def test_clip_notice_accepts_counts_and_the_review_picks_the_clip_by_count(monkeypatch, member_id):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True, disputes=True)
    set_mode(monkeypatch, "yolo")
    from backend import admin, evidence, review
    monkeypatch.setattr(admin, "force_decline", False)
    monkeypatch.setattr(review, "schedule", lambda dispute_id: None)
    shelf_state.reset()
    with TestClient(app) as client:
        sid = enter_yolo(client, member_id, with_counts(b0={"elx": 1}))
        folder = evidence.clips_dir(sid)
        folder.mkdir(parents=True, exist_ok=True)
        ok, jpg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))

        def clip(stamp: str, before: dict, after: dict, change_ts: int) -> int:
            (folder / f"clip_bay0_{stamp}.mp4").write_bytes(b"\x00")
            (folder / f"clip_bay0_{stamp}_k0.jpg").write_bytes(jpg.tobytes())
            body = {"session_id": sid, "bay": 0, "sku": "elx", "file": f"clip_bay0_{stamp}.mp4",
                    "keyframes": [{"id": "k0", "file": f"clip_bay0_{stamp}_k0.jpg", "ts": change_ts - 4000}],
                    "change_ts": change_ts, "starts_ts": change_ts - 4000, "ends_ts": change_ts + 2000,
                    "units_before": [], "units_after": [], "counts_before": before, "counts_after": after,
                    "motion": [], "width": 512, "height": 468, "fps": 5, "frames": 31}
            r = client.post("/internal/clips", json=body, headers=HEADERS)
            assert r.status_code == 200, r.text
            return r.json()["clip_id"]

        first = clip("20260926T021410000Z", {"elx": 2}, {"elx": 1}, 1_800_000_010_000)  # the pick
        second = clip("20260926T021420000Z", {"elx": 1}, {"elx": 1, "rec": 1}, 1_800_000_020_000)  # something added
        rows = evidence.clips_for(sid, 0)
        assert [(r["id"], r["counts_before"], r["counts_after"]) for r in rows] == \
            [(first, {"elx": 2}, {"elx": 1}), (second, {"elx": 1}, {"elx": 1, "rec": 1})]
        yolo_crop(client, sid, "baseline", {"elx": 2}, {"elx": [BOX_A, BOX_B]}, 1_800_000_000_000)
        yolo_crop(client, sid, "change", {"elx": 1}, {"elx": [BOX_B]}, 1_800_000_010_000)
        r = client.post("/api/disputes", json={"sku": "elx"})
        assert r.status_code == 200, r.text
        d = r.json()["dispute"]
        inputs = review.gather(disputes._row(d["dispute_id"]))
        # the newest clip in which the hydration drink count dropped, not simply the newest clip
        assert [f["id"] for f in inputs["frames"]] == ["baseline", "latest", f"clip{first}_k0"]
        assert inputs["timeline"]["clips"][0]["counts_before"] == {"elx": 2}
        assert d["clips"][0]["counts_before"] == {"elx": 2} and d["clips"][0]["units_before"] == []


# --- 4. capture tool ---

def test_capture_reminds_about_tags_and_counts_empty_frames(tmp_path):
    assert NO_TAGS_REMINDER == "No tags: remove all tags before capturing"
    s = CaptureSession(tmp_path, "run", mode="yolo")
    assert s.tag_free and s.no_tag_target == 1.0 and not s.empty
    frame = np.full((72, 128, 3), 77, np.uint8)
    s.save(frame, tags_visible=0)
    assert s.toggle_empty()
    s.save(frame, tags_visible=0)
    s.save(frame, tags_visible=1)
    assert not s.toggle_empty()
    s.save(frame, tags_visible=0)
    assert (s.saved, s.saved_without_tags, s.saved_empty) == (4, 3, 2)
    assert s.empty_share == 0.5 and s.no_tag_share == 0.75
    plain = CaptureSession(tmp_path, "run2")
    assert not plain.tag_free and plain.no_tag_target == 0.5
    view_yolo = draw_capture_overlay(FRAME, BAYS, s, tags_visible=1, fps=30.0)
    view_plain = draw_capture_overlay(FRAME, BAYS, CaptureSession(tmp_path, "run3", saved=4, saved_without_tags=3,
                                                                  saved_empty=2), tags_visible=1, fps=30.0)
    assert view_yolo.any() and not np.array_equal(view_yolo, view_plain) and not FRAME.any()  # the reminder line
    # a red reminder while a tag is in view, not while none is
    red = np.all(view_yolo == (0, 0, 255), axis=2).sum()
    calm = np.all(draw_capture_overlay(FRAME, BAYS, s, tags_visible=0, fps=30.0) == (0, 0, 255), axis=2).sum()
    assert red > calm


def test_training_guide_is_tag_free_with_catalog_classes():
    guide = (ROOT / "training" / "TRAINING.md").read_text(encoding="utf-8")
    for s in CATALOG["skus"]:
        assert f"`{s['yolo_class']}`" in guide
    assert "10 %" in guide and "empty shelf" in guide.lower() and "remove all tags" in guide
    assert '"mode": "yolo"' in guide


# --- 6. dev scripts ---

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fake_shelf_yolo_only_sends_counts_without_units():
    fs = _load("fake_shelf")
    units, bay_ids, skus = fs.load_units(), fs.load_bay_ids(), fs.load_unit_skus()
    snap_ = fs.build_snapshot(units, bay_ids, removed={4}, frame_id=1, covered={1}, unit_skus=skus, yolo_only=True)
    bays = {b["bay"]: b for b in snap_["bays"]}
    assert all(b["units"] == [] for b in bays.values())
    assert bays[HOME["elx"]]["yolo_counts"] == {"elx": 2}  # covering is meaningless without tags
    assert bays[HOME["bar"]]["yolo_counts"] == {"bar": 1}  # tag 4 removed: one bar left by count
    shelf_state.ShelfSnapshot.model_validate(snap_)
    # the plain and --yolo shapes are unchanged
    assert fs.build_snapshot(units, bay_ids, set(), 1)["bays"][0]["units"] == sorted(
        t for t, h in units.items() if h == bay_ids[0])


def test_e2e_sim_yolo_only_poster(monkeypatch):
    sim = _load("e2e_sim")
    catalog = sim.Catalog()
    poster = sim.ShelfPoster("http://x", "t", catalog, yolo_only=True)
    poster.set_removed({catalog.tag_for("elx")})
    snap_ = poster.snapshot()
    assert all(b["units"] == [] for b in snap_["bays"]) and snap_["loose_units"] == []
    counts = {b["bay"]: b["yolo_counts"] for b in snap_["bays"]}
    assert counts[HOME["elx"]] == {"elx": full_baseline()["elx"] - 1}
    shelf_state.ShelfSnapshot.model_validate(snap_)
    tagged = sim.ShelfPoster("http://x", "t", catalog).snapshot()
    assert tagged["bays"][HOME["elx"]]["units"] and all(b["yolo_counts"] == {} for b in tagged["bays"])
    # --yolo-only refuses a backend that would count an empty shelf
    catalog.yolo_flag = False
    with pytest.raises(sim.Fail, match="features.yolo"):
        sim.step_full_shelf(poster, None)


def test_yolo_only_snapshot_drives_the_backend_over_http(monkeypatch, member_id):
    """The same body fake_shelf --yolo-only posts, through the real route, in yolo mode."""
    set_mode(monkeypatch, "yolo")
    shelf_state.reset()
    fs = _load("fake_shelf")
    units, bay_ids, skus = fs.load_units(), fs.load_bay_ids(), fs.load_unit_skus()
    with TestClient(app) as client:
        r = client.post("/internal/shelf", json=fs.build_snapshot(units, bay_ids, set(), 1, None, skus, True),
                        headers=HEADERS)
        assert r.status_code == 200 and r.json()["ok"]
        assert shelf_state.shelf_counts() == full_baseline()
        session = store.start_session(member_id)
        r = client.post("/internal/shelf", json=fs.build_snapshot(units, bay_ids, {4}, 2, None, skus, True),
                        headers=HEADERS)
        assert r.json()["changed"] and r.json()["session_id"] == session["id"]
        assert qty(store.current_cart(), "bar") == 1
