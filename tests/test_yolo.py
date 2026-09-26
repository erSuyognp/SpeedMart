"""S5.2 tests: YOLO inference wrapper, bay assignment, fusion (9.4), stability with YOLO counts, snapshot
and overlay. A fake predictor stands in for the model; ultralytics is never needed."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from vision import fusion
from vision import yolo_detect as yd
from vision.overlay import draw_overlay
from vision.worker import StabilityTracker, build_snapshot, start_yolo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
BAYS = CONFIG["bays"]  # 0: [80,200,360,620] elx, 1: [400,200,680,620] rec, 2: [720,200,1000,620] bar
CLASS_SKU = yd.class_to_sku(CATALOG)
UNIT_SKU = fusion.unit_skus(CATALOG)
FRAME = np.zeros((720, 1280, 3), np.uint8)


def box(cls: str, conf: float, cx: float, cy: float, half: float = 30) -> yd.RawBox:
    return (cls, conf, (cx - half, cy - half, cx + half, cy + half))


class FakePredictor:
    """Returns the next scripted list of boxes on each call (the last one repeats)."""

    def __init__(self, *scripts):
        self.scripts = list(scripts) or [[]]
        self.calls = 0

    def __call__(self, frame):
        out = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return out


def detector(predict, every_n: int = 3, conf: float = 0.55) -> yd.YoloDetector:
    return yd.YoloDetector(predict, BAYS, CLASS_SKU, conf, every_n)


# --- mapping and bay assignment -----------------------------------------------------------------


def test_class_to_sku_from_catalog():
    assert CLASS_SKU == {"electrolytes": "elx", "recovery_drink": "rec", "protein_bar": "bar"}


def test_assign_boxes_by_center_conf_and_class():
    raw = [
        box("electrolytes", 0.9, 200, 400),
        box("electrolytes", 0.7, 300, 300),
        box("electrolytes", 0.5, 250, 400),  # below conf
        box("protein_bar", 0.8, 500, 400),  # misplaced: in bay 1
        box("recovery_drink", 0.95, 1200, 100),  # outside every bay (in a hand)
        box("banana", 0.99, 800, 400),  # not in the catalog
        ("protein_bar", 0.9, (340, 300, 420, 360)),  # box spans bays 0 and 1, center x = 380 is in the gap
    ]
    result = yd.assign_boxes(raw, BAYS, CLASS_SKU, 0.55)
    assert result.per_bay == {0: {"elx": 2}, 1: {"bar": 1}, 2: {}}
    assert len(result.boxes) == 6  # everything above conf is kept for drawing
    by_cls = {(b.cls_name, b.bay) for b in result.boxes}
    assert ("recovery_drink", None) in by_cls and ("banana", 2) in by_cls
    assert next(b for b in result.boxes if b.cls_name == "banana").sku is None


def test_center_on_shared_edge_uses_half_open_rule():
    bays = [{"id": 0, "roi": [0, 0, 100, 100]}, {"id": 1, "roi": [100, 0, 200, 100]}]
    result = yd.assign_boxes([("electrolytes", 0.9, (90, 40, 110, 60))], bays, CLASS_SKU, 0.5)
    assert result.per_bay == {0: {}, 1: {"elx": 1}}


# --- detector: every N frames, cache, failures ---------------------------------------------------


def test_runs_every_n_frames_and_caches():
    fake = FakePredictor([box("electrolytes", 0.9, 200, 400)], [], [box("protein_bar", 0.9, 800, 400)] * 2)
    det = detector(fake, every_n=3)
    results = [det.update(FRAME) for _ in range(7)]
    assert fake.calls == 3  # frames 0, 3, 6
    assert [r.frame_index for r in results] == [0, 0, 0, 3, 3, 3, 6]
    assert results[2].per_bay[0] == {"elx": 1}  # cached
    assert results[4].per_bay == {0: {}, 1: {}, 2: {}}
    assert results[6].per_bay[2] == {"bar": 2}
    assert det.last is results[6]


def test_every_n_one_runs_each_frame_and_zero_is_clamped():
    fake = FakePredictor([])
    det = detector(fake, every_n=0)
    for _ in range(4):
        det.update(FRAME)
    assert fake.calls == 4


def test_inference_error_never_raises_and_clears_counts(caplog):
    fake = FakePredictor([box("electrolytes", 0.9, 200, 400)], RuntimeError("CUDA out of memory"),
                         RuntimeError("again"), [box("electrolytes", 0.9, 200, 400)])
    det = detector(fake, every_n=1)
    assert det.update(FRAME).per_bay[0] == {"elx": 1}
    with caplog.at_level(logging.WARNING, logger="vision.yolo"):
        r1, r2 = det.update(FRAME), det.update(FRAME)
    assert not r1.ok and r1.per_bay == {0: {}, 1: {}, 2: {}}  # no stale counts kept
    assert not r2.ok
    assert sum("inference failed" in r.message for r in caplog.records) == 1  # rate limited
    assert det.update(FRAME).ok


def test_unknown_class_warned_once(caplog):
    det = detector(FakePredictor([box("banana", 0.9, 200, 400)]), every_n=1)
    with caplog.at_level(logging.WARNING, logger="vision.yolo"):
        for _ in range(3):
            det.update(FRAME)
    assert sum("banana" in r.message for r in caplog.records) == 1


# --- loading: missing pieces never crash ----------------------------------------------------------


def _config_with_model(path: str) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["features"]["yolo"] = True
    config["vision"]["yolo_model"] = path
    return config


def test_missing_model_file_warns_once_and_returns_none(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="vision.yolo"):
        assert yd.load_yolo(_config_with_model("models/nope.pt"), CATALOG, root=tmp_path) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "nope.pt" in warnings[0].message and "tags only" in warnings[0].message


def test_missing_ultralytics_warns_once_and_returns_none(tmp_path, caplog, monkeypatch):
    (tmp_path / "m.pt").write_bytes(b"not a model")
    monkeypatch.setitem(sys.modules, "ultralytics", None)  # import raises ImportError
    with caplog.at_level(logging.WARNING, logger="vision.yolo"):
        assert yd.load_yolo(_config_with_model("m.pt"), CATALOG, root=tmp_path) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "requirements-yolo.txt" in warnings[0].message


def test_model_that_fails_to_load_returns_none(tmp_path, caplog, monkeypatch):
    (tmp_path / "m.pt").write_bytes(b"x")
    fake = types.ModuleType("ultralytics")

    def broken(path):
        raise RuntimeError("corrupt checkpoint")

    fake.YOLO = broken
    monkeypatch.setitem(sys.modules, "ultralytics", fake)
    with caplog.at_level(logging.WARNING, logger="vision.yolo"):
        assert yd.load_yolo(_config_with_model("m.pt"), CATALOG, root=tmp_path) is None
    assert any("corrupt checkpoint" in r.message for r in caplog.records)


class _T:
    """Minimal stand in for a torch tensor: .cpu().numpy()."""

    def __init__(self, a):
        self.a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = _T(xyxy), _T(conf), _T(cls)

    def __len__(self):
        return len(self.xyxy.a)


class FakeModel:
    names = {0: "electrolytes", 1: "protein_bar", 2: "recovery_drink"}

    def __init__(self, path=None):
        self.calls = []

    def predict(self, frame, **kwargs):
        self.calls.append(kwargs)
        r = types.SimpleNamespace(names=self.names,
                                  boxes=_Boxes([[170, 370, 230, 430], [470, 370, 530, 430]], [0.91, 0.66], [0, 1]))
        return [r, types.SimpleNamespace(names=self.names, boxes=None)]


def test_load_with_fake_ultralytics_builds_working_detector(tmp_path, monkeypatch):
    (tmp_path / "m.pt").write_bytes(b"x")
    fake = types.ModuleType("ultralytics")
    fake.YOLO = FakeModel
    monkeypatch.setitem(sys.modules, "ultralytics", fake)
    monkeypatch.setattr(yd, "select_device", lambda: "cpu")
    det = yd.load_yolo(_config_with_model("m.pt"), CATALOG, root=tmp_path)
    assert det is not None and det.every_n == CONFIG["vision"]["yolo_every_n_frames"]
    result = det.update(FRAME)
    assert result.per_bay == {0: {"elx": 1}, 1: {"bar": 1}, 2: {}}
    assert [(b.cls_name, round(b.conf, 2)) for b in result.boxes] == [("electrolytes", 0.91), ("protein_bar", 0.66)]


def test_predictor_passes_spec_arguments():
    model = FakeModel()
    predict = yd.ultralytics_predictor(model, 0.55, "mps")
    out = predict(FRAME)
    assert model.calls == [{"imgsz": 640, "conf": 0.55, "verbose": False, "device": "mps"}]
    assert out[0] == ("electrolytes", pytest.approx(0.91), (170.0, 370.0, 230.0, 430.0))


def _fake_torch(mps: bool, cuda: bool):
    t = types.ModuleType("torch")
    t.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    t.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    return t


def test_select_device(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert yd.select_device() == "cpu"
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=False, cuda=True))
    assert yd.select_device() == 0
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=False, cuda=False))
    assert yd.select_device() == "cpu"
    monkeypatch.setattr(yd.sys, "platform", "darwin")
    monkeypatch.setattr(yd.platform, "machine", lambda: "arm64")
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=True, cuda=False))
    assert yd.select_device() == "mps"


def test_start_yolo_respects_flag():
    calls = []

    def loader(config, catalog):
        calls.append(1)
        return None

    assert start_yolo(CONFIG, CATALOG, loader) == (None, "")  # flag false in the repo config
    assert not calls
    on = _config_with_model("x.pt")
    det, status = start_yolo(on, CATALOG, loader)
    assert det is None and "unavailable" in status and calls == [1]
    det, status = start_yolo(on, CATALOG, lambda c, k: detector(FakePredictor([])))
    assert det is not None and status.startswith("YOLO on")


def test_repo_config_keeps_yolo_off_and_requirements_split():
    assert CONFIG["features"]["yolo"] is False
    assert "ultralytics" not in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "ultralytics>=8.3" in (ROOT / "requirements-yolo.txt").read_text(encoding="utf-8")


def test_vision_imports_without_ultralytics():
    if importlib.util.find_spec("ultralytics") is not None:
        pytest.skip("ultralytics installed here")
    assert "ultralytics" not in sys.modules


# --- fusion (9.4) -------------------------------------------------------------------------------


def test_fuse_bay_takes_max_per_sku():
    assert fusion.fuse_bay({"elx": 2}, {"elx": 1}) == {"elx": 2}  # tags never overcount
    assert fusion.fuse_bay({"elx": 1}, {"elx": 2}) == {"elx": 2}  # YOLO fills in a covered tag
    assert fusion.fuse_bay({"elx": 1}, {"bar": 1}) == {"elx": 1, "bar": 1}
    assert fusion.fuse_bay({"elx": 1}, None) == {"elx": 1}
    assert fusion.fuse_bay({}, {"elx": 0}) == {}


def test_fuse_all_bays_and_totals():
    per_bay = {0: [0, 1], 1: [2], 2: [4, 99]}  # 99 unknown
    yolo = {0: {"elx": 1}, 1: {"rec": 2}, 2: {}}
    fused = fusion.fuse(per_bay, yolo, UNIT_SKU)
    assert fused == {0: {"elx": 2}, 1: {"rec": 2}, 2: {"bar": 1}}
    assert fusion.shelf_totals(fused) == {"elx": 2, "rec": 2, "bar": 1}
    assert fusion.fuse(per_bay, None, UNIT_SKU) == {0: {"elx": 2}, 1: {"rec": 1}, 2: {"bar": 1}}


@pytest.mark.parametrize("yolo_on", [True, False])
def test_fusion_matches_backend_counts(monkeypatch, yolo_on):
    from backend import shelf_state

    s = shelf_state.settings
    monkeypatch.setattr(shelf_state, "settings", dataclasses.replace(s, features=dataclasses.replace(s.features, yolo=yolo_on)))
    shelf_state.reset()
    try:
        per_bay = {0: [0], 1: [2, 3], 2: [4, 1]}  # tag 1 (elx) misplaced into bay 2
        yolo = {0: {"elx": 2}, 1: {"rec": 1}, 2: {"bar": 2, "elx": 1}}
        status = StabilityTracker([0, 1, 2], 0, 0).update(np.zeros((720, 1280), np.uint8), per_bay, 0.0,
                                                           yolo_counts=yolo if yolo_on else None)
        snap = build_snapshot(1, 1, per_bay, status, [])
        shelf_state.apply_snapshot(snap)
        expected = fusion.shelf_totals(fusion.fuse(per_bay, yolo if yolo_on else None, UNIT_SKU))
        backend = {k: v for k, v in shelf_state.shelf_counts().items() if v}
        assert backend == expected
        assert expected == ({"elx": 3, "rec": 2, "bar": 2} if yolo_on else {"elx": 2, "rec": 2, "bar": 1})
    finally:
        shelf_state.reset()


# --- stability, snapshot and overlay with YOLO ----------------------------------------------------


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def _feed(tr, clock, ms, per_bay, yolo):
    out = None
    for _ in range(int(ms / 50)):
        clock.t += 0.05
        out = tr.update(np.zeros((720, 1280), np.uint8), per_bay, yolo_counts=yolo)
    return out


def test_stability_content_includes_yolo_counts():
    clock = Clock()
    tr = StabilityTracker([0, 1, 2], 400, 700, None, clock=clock)
    per_bay = {0: [0, 1], 1: [], 2: []}
    st = _feed(tr, clock, 450, per_bay, {0: {"elx": 2}})
    assert st[0].stable and st[0].yolo_counts == {"elx": 2}
    # tags unchanged, YOLO count drops: a removal, needs 700 ms, bay reports the last stable counts meanwhile
    st = _feed(tr, clock, 650, per_bay, {0: {"elx": 1}})
    assert not st[0].stable and st[0].yolo_counts == {"elx": 2} and st[0].pending_yolo == {"elx": 1}
    assert st[0].need_ms == 700
    st = _feed(tr, clock, 100, per_bay, {0: {"elx": 1}})
    assert st[0].stable and st[0].yolo_counts == {"elx": 1}


def test_covered_tag_with_yolo_keeps_shelf_count():
    """Tag 1 gets covered but YOLO still sees two electrolytes: fused count stays 2 after confirmation."""
    clock = Clock()
    tr = StabilityTracker([0, 1, 2], 400, 700, None, clock=clock)
    _feed(tr, clock, 450, {0: [0, 1], 1: [], 2: []}, {0: {"elx": 2}})
    st = _feed(tr, clock, 800, {0: [0], 1: [], 2: []}, {0: {"elx": 2}})
    assert st[0].stable and st[0].units == (0,)
    fused = fusion.fuse({b: s.units for b, s in st.items()}, {b: s.yolo_counts for b, s in st.items()}, UNIT_SKU)
    assert fused[0] == {"elx": 2}


def test_snapshot_has_yolo_counts_only_when_on():
    per_bay = {0: [0], 1: [], 2: []}
    on = StabilityTracker([0, 1, 2], 0, 0).update(np.zeros((10, 10), np.uint8), per_bay, 0.0,
                                                   yolo_counts={0: {"elx": 2}, 1: {}, 2: {}})
    off = StabilityTracker([0, 1, 2], 0, 0).update(np.zeros((10, 10), np.uint8), per_bay, 0.0)
    assert build_snapshot(1, 1, per_bay, on, [])["bays"][0]["yolo_counts"] == {"elx": 2}
    assert all(b["yolo_counts"] == {} for b in build_snapshot(1, 1, per_bay, off, [])["bays"])
    from backend.shelf_state import ShelfSnapshot
    ShelfSnapshot.model_validate(build_snapshot(1, 1, per_bay, on, []))


def test_worker_pipeline_with_fake_detector():
    """Detector -> tracker -> snapshot, as the worker loop wires them."""
    det = detector(FakePredictor([box("electrolytes", 0.9, 200, 400), box("electrolytes", 0.8, 300, 500)]))
    clock = Clock()
    tr = StabilityTracker([0, 1, 2], 400, 700, None, clock=clock)
    per_bay = {0: [0], 1: [], 2: []}
    status = None
    for _ in range(10):
        clock.t += 0.05
        result = det.update(FRAME)
        status = tr.update(np.zeros((720, 1280), np.uint8), per_bay, yolo_counts=result.per_bay)
    snap = build_snapshot(1, 1, per_bay, status, [])
    assert snap["bays"][0] == {"bay": 0, "stable": True, "motion": False, "units": [0], "yolo_counts": {"elx": 2}}


def test_overlay_draws_yolo_boxes():
    det = detector(FakePredictor([box("electrolytes", 0.9, 200, 400), box("banana", 0.9, 800, 400)]))
    result = det.update(FRAME)
    status = StabilityTracker([0, 1, 2], 0, 0).update(np.zeros((720, 1280), np.uint8), {0: [], 1: [], 2: []}, 0.0,
                                                       yolo_counts=result.per_bay)
    common = dict(bays=BAYS, sku_names={}, detections=[], per_bay={0: [], 1: [], 2: []}, loose=[], status=status,
                  fps=20, backend_ok=True, last_post_age_ms=5, paused=False)
    with_boxes = draw_overlay(FRAME, yolo_boxes=result.boxes, yolo_status="YOLO on (cpu, every 3)", **common)
    without = draw_overlay(FRAME, **common)
    magenta = np.all(with_boxes == (255, 0, 255), axis=2)
    assert magenta.any() and not np.all(without == (255, 0, 255), axis=2).any()
    assert not FRAME.any()


# --- fake_shelf --yolo ---------------------------------------------------------------------------


def _fake_shelf():
    spec = importlib.util.spec_from_file_location("fake_shelf", ROOT / "scripts" / "fake_shelf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fake_shelf_cover_with_yolo():
    fs = _fake_shelf()
    units, bay_ids, skus = fs.load_units(), fs.load_bay_ids(), fs.load_unit_skus()
    snap = fs.build_snapshot(units, bay_ids, removed={4}, frame_id=1, covered={1}, unit_skus=skus)
    bays = {b["bay"]: b for b in snap["bays"]}
    assert bays[0]["units"] == [0] and bays[0]["yolo_counts"] == {"elx": 2}
    assert bays[2]["units"] == [5] and bays[2]["yolo_counts"] == {"bar": 1}
    plain = fs.build_snapshot(units, bay_ids, set(), 1)
    assert all(b["yolo_counts"] == {} for b in plain["bays"])
    from backend.shelf_state import ShelfSnapshot
    ShelfSnapshot.model_validate(snap)
