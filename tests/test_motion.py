"""S2.3 tests: per bay motion (9.2), bay stability with asymmetric confirmation (9.3), motion tuning save.
Synthetic numpy frames and a fake clock; no camera needed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from vision.motion import MotionDetector, expand_roi
from vision.overlay import bay_state_text, draw_overlay
from vision.worker import (
    BayContent,
    StabilityTracker,
    apply_tuning_key,
    build_snapshot,
    is_removal,
    save_motion_tuning,
)

ROOT = Path(__file__).resolve().parent.parent
H, W = 200, 400
BAYS = [
    {"id": 0, "roi": [40, 40, 140, 160], "sku": "elx"},
    {"id": 1, "roi": [240, 40, 340, 160], "sku": "rec"},
]
THRESHOLD, SETTLE_MS, MARGIN = 0.02, 300, 20
STABLE_MS, REMOVE_MS = 400, 700
FRAME_S = 0.05  # 20 fps


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms / 1000.0


def still() -> np.ndarray:
    return np.full((H, W), 100, np.uint8)


def blob(x: int, y: int, size: int = 50) -> np.ndarray:
    """A bright square on the still background: alternating two positions makes motion every frame."""
    frame = still()
    frame[y:y + size, x:x + size] = 250
    return frame


def detector() -> MotionDetector:
    return MotionDetector(BAYS, threshold=THRESHOLD, settle_ms=SETTLE_MS, margin_px=MARGIN)


def tracker(clock: FakeClock, motion: bool = True) -> StabilityTracker:
    return StabilityTracker([0, 1], STABLE_MS, REMOVE_MS, detector() if motion else None, clock=clock)


def run(tr: StabilityTracker, clock: FakeClock, ms: float, per_bay: dict, frames=None, yolo=None):
    """Feed frames for `ms` at 20 fps; `frames` is a callable(i) -> frame (default: still). Returns the
    status list, one entry per frame."""
    out = []
    for i in range(int(round(ms / (FRAME_S * 1000)))):
        clock.advance(FRAME_S * 1000)
        frame = still() if frames is None else frames(i)
        out.append(tr.update(frame, per_bay, yolo_counts=yolo))
    return out


def moving_hand_in_bay0(i: int) -> np.ndarray:
    return blob(60 if i % 2 else 80, 70)


# --- motion (9.2) -------------------------------------------------------------------------------


def test_expand_roi_clamps_to_frame():
    assert expand_roi([40, 40, 140, 160], 20, W, H) == (20, 20, 160, 180)
    assert expand_roi([0, 0, 390, 195], 40, W, H) == (0, 0, W, H)


def test_first_frame_has_no_motion():
    states = detector().update(still(), 0.0)
    assert states[0].changed_fraction == 0.0 and not states[0].motion and states[0].motion_free


def test_motion_trigger_only_in_the_changed_bay():
    d = detector()
    d.update(still(), 0.0)
    states = d.update(blob(60, 70), 0.05)
    assert states[0].motion and states[0].changed_fraction > THRESHOLD
    assert not states[0].motion_free
    assert not states[1].motion and states[1].changed_fraction == 0.0 and states[1].motion_free


def test_small_change_below_threshold_is_not_motion():
    d = detector()
    d.update(still(), 0.0)
    states = d.update(blob(60, 70, size=10), 0.05)  # ~100 px of a 140x160 area = 0.4 %
    assert 0 < states[0].changed_fraction < THRESHOLD
    assert not states[0].motion


def test_margin_catches_motion_just_outside_roi():
    d = detector()
    d.update(still(), 0.0)
    frame = still()
    frame[40:160, 142:158] = 250  # right of bay 0's ROI (x2 = 140) but inside its 20 px margin
    states = d.update(frame, 0.05)
    assert states[0].motion
    assert not states[1].motion  # bay 1's margin starts at x = 220


def test_motion_outside_margin_is_ignored():
    d = detector()
    d.update(still(), 0.0)
    frame = still()
    frame[40:160, 175:215] = 250  # between the two margins
    states = d.update(frame, 0.05)
    assert not states[0].motion and not states[1].motion


def test_settle_time():
    d = detector()
    d.update(still(), 0.0)
    assert d.update(blob(60, 70), 0.100)[0].motion  # last motion at t = 0.1
    s = d.update(blob(60, 70), 0.150)  # identical frame: no new motion
    assert not s[0].motion and not s[0].motion_free and s[0].settle_left_ms == 250
    s = d.update(blob(60, 70), 0.399)
    assert not s[0].motion_free and s[0].settle_left_ms == 1
    s = d.update(blob(60, 70), 0.400)
    assert s[0].motion_free and s[0].settle_left_ms == 0


def test_live_threshold_change_applies_next_frame():
    d = detector()
    d.update(still(), 0.0)
    d.threshold = 0.5
    assert not d.update(blob(60, 70), 0.05)[0].motion


def test_resized_frame_resets_without_crashing():
    d = detector()
    d.update(still(), 0.0)
    states = d.update(np.full((H // 2, W // 2), 100, np.uint8), 0.05)
    assert states[0].changed_fraction == 0.0


# --- stability (9.3) ----------------------------------------------------------------------------


def test_startup_becomes_stable_after_stable_ms():
    clock = FakeClock()
    tr = tracker(clock)
    statuses = run(tr, clock, 350, {0: [0, 1], 1: [2]})
    assert not statuses[-1][0].stable
    assert statuses[-1][0].units == ()  # nothing confirmed yet
    statuses = run(tr, clock, 100, {0: [0, 1], 1: [2]})
    assert statuses[-1][0].stable and statuses[-1][0].units == (0, 1)
    assert statuses[-1][1].stable and statuses[-1][1].units == (2,)


def _settled(clock: FakeClock, motion: bool = True) -> StabilityTracker:
    tr = tracker(clock, motion)
    run(tr, clock, 500, {0: [0, 1], 1: [2]})
    return tr


def test_removal_waits_for_stable_remove_ms():
    clock = FakeClock()
    tr = _settled(clock)
    statuses = run(tr, clock, 650, {0: [0], 1: [2]})
    assert all(not s[0].stable for s in statuses)  # past stable_ms (400) but not stable_remove_ms (700)
    assert statuses[-1][0].units == (0, 1)  # unstable bay keeps its last stable units
    assert statuses[-1][0].pending_units == (0,) and statuses[-1][0].need_ms == REMOVE_MS
    statuses = run(tr, clock, 100, {0: [0], 1: [2]})
    assert statuses[-1][0].stable and statuses[-1][0].units == (0,)
    assert statuses[-1][0].pending_units is None
    assert all(s[1].stable for s in statuses)  # the other bay never flickered


def test_addition_confirms_after_stable_ms():
    clock = FakeClock()
    tr = _settled(clock)
    statuses = run(tr, clock, 350, {0: [0, 1, 4], 1: [2]})
    assert not statuses[-1][0].stable and statuses[-1][0].need_ms == STABLE_MS
    assert statuses[-1][0].units == (0, 1)
    statuses = run(tr, clock, 100, {0: [0, 1, 4], 1: [2]})
    assert statuses[-1][0].stable and statuses[-1][0].units == (0, 1, 4)


def test_swap_counts_as_removal():
    assert is_removal(BayContent((0, 1)), BayContent((0, 4)))
    assert not is_removal(BayContent((0,)), BayContent((0, 4)))
    assert not is_removal(None, BayContent(()))
    assert is_removal(BayContent((), (("elx", 2),)), BayContent((), (("elx", 1),)))


def test_flicker_never_confirms():
    clock = FakeClock()
    tr = _settled(clock)
    # tag 1 disappears for 300 ms, reappears for 100 ms, repeatedly, for 4 s (glare / finger)
    for _ in range(10):
        for s in run(tr, clock, 300, {0: [0], 1: [2]}):
            assert s[0].units == (0, 1)
        for s in run(tr, clock, 100, {0: [0, 1], 1: [2]}):
            assert s[0].units == (0, 1)


def test_motion_blocks_confirmation():
    clock = FakeClock()
    tr = _settled(clock)
    # a hand moves in bay 0 for 2 s while the tag is hidden: never confirmed
    statuses = run(tr, clock, 2000, {0: [0], 1: [2]}, frames=moving_hand_in_bay0)
    assert all(not s[0].stable for s in statuses)
    assert all(s[0].units == (0, 1) for s in statuses)
    assert statuses[-1][0].motion and statuses[-1][0].changed_fraction > THRESHOLD
    assert all(s[1].stable for s in statuses)  # bay 1 is outside the hand's area
    # the hand leaves: the first still frame is itself a big change, then settle_ms must pass
    statuses = run(tr, clock, 300, {0: [0], 1: [2]})
    assert not statuses[-1][0].stable and not statuses[-1][0].motion and statuses[-1][0].settle_left_ms > 0
    statuses = run(tr, clock, 100, {0: [0], 1: [2]})
    assert statuses[-1][0].stable and statuses[-1][0].units == (0,)


def test_hand_over_bay_then_put_back_changes_nothing():
    """The S2.3 physical check in miniature: cover the tag with a moving hand, then uncover it."""
    clock = FakeClock()
    tr = _settled(clock)
    run(tr, clock, 3000, {0: [0], 1: [2]}, frames=moving_hand_in_bay0)
    statuses = run(tr, clock, 1000, {0: [0, 1], 1: [2]})
    assert all(s[0].units == (0, 1) for s in statuses)
    assert statuses[-1][0].stable


def test_flag_off_ignores_motion():
    clock = FakeClock()
    tr = _settled(clock, motion=False)
    statuses = run(tr, clock, 750, {0: [0], 1: [2]}, frames=moving_hand_in_bay0)
    assert statuses[-1][0].stable and statuses[-1][0].units == (0,)
    assert not statuses[-1][0].motion and statuses[-1][0].changed_fraction is None


def test_from_config_respects_motion_freeze_flag():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    config["features"]["motion_freeze"] = False
    assert StabilityTracker.from_config(config).motion is None
    config["features"]["motion_freeze"] = True
    tr = StabilityTracker.from_config(config)
    assert tr.motion is not None
    assert tr.motion.margin_px == config["vision"]["motion_margin_px"]
    assert tr.stable_remove_ms == config["vision"]["stable_remove_ms"]


def test_from_config_defaults_for_missing_new_keys():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    del config["vision"]["motion_margin_px"], config["vision"]["stable_remove_ms"]
    tr = StabilityTracker.from_config(config)
    assert tr.motion.margin_px == 40 and tr.stable_remove_ms == 700


def test_uses_injected_clock_when_now_omitted():
    clock = FakeClock(5.0)
    tr = tracker(clock)
    tr.update(still(), {0: [0], 1: []})
    clock.advance(STABLE_MS)
    assert tr.update(still(), {0: [0], 1: []})[0].stable


# --- snapshot + overlay -------------------------------------------------------------------------


def test_snapshot_reports_last_stable_units_for_unstable_bay():
    clock = FakeClock()
    tr = _settled(clock)
    per_bay = {0: [0], 1: [2]}
    status = run(tr, clock, 100, per_bay)[-1]
    snap = build_snapshot(1, 2, per_bay, status, [1])
    bay0 = snap["bays"][0]
    assert bay0 == {"bay": 0, "stable": False, "motion": False, "units": [0, 1], "yolo_counts": {}}
    from backend.shelf_state import ShelfSnapshot
    ShelfSnapshot.model_validate(snap)


def test_overlay_draws_every_state():
    clock = FakeClock()
    tr = _settled(clock)
    frame = np.zeros((H, W, 3), np.uint8)
    for frames in (None, moving_hand_in_bay0):
        status = run(tr, clock, 100, {0: [0], 1: [2]}, frames=frames)[-1]
        view = draw_overlay(frame, bays=BAYS, sku_names={}, detections=[], per_bay={0: [0], 1: [2]}, loose=[],
                            status=status, fps=20, backend_ok=True, last_post_age_ms=10, paused=False,
                            motion=tr.motion, message="hello")
        assert view.shape == frame.shape and view.any()
    assert frame.sum() == 0  # input untouched
    assert "MOTION" in bay_state_text(status[0]) and "chg" in bay_state_text(status[0])
    assert bay_state_text(status[1]).endswith("stable")


# --- live tuning --------------------------------------------------------------------------------


def test_tuning_keys():
    d = detector()
    assert apply_tuning_key(ord("]"), d) == "motion_threshold 0.025" and d.threshold == 0.025
    apply_tuning_key(ord("["), d)
    apply_tuning_key(ord("["), d)
    assert d.threshold == 0.015
    for _ in range(10):
        apply_tuning_key(ord("["), d)
    assert d.threshold == 0.005  # floor
    assert apply_tuning_key(ord("="), d) == "motion_settle_ms 350"
    for _ in range(10):
        apply_tuning_key(ord("-"), d)
    assert d.settle_ms == 0
    assert apply_tuning_key(ord("x"), d) is None
    assert "off" in apply_tuning_key(ord("]"), None)


def test_save_motion_tuning_preserves_other_keys(tmp_path):
    path = tmp_path / "config.json"
    shutil.copyfile(ROOT / "config.json", path)
    before_text = path.read_text(encoding="utf-8")
    before = json.loads(before_text)
    d = detector()
    d.threshold, d.settle_ms = 0.035, 450.0
    save_motion_tuning(d, path)
    after_text = path.read_text(encoding="utf-8")
    after = json.loads(after_text)
    assert after["vision"]["motion_threshold"] == 0.035 and after["vision"]["motion_settle_ms"] == 450
    for key in ("motion_threshold", "motion_settle_ms"):
        before["vision"].pop(key), after["vision"].pop(key)
    assert after == before
    assert len(after_text.splitlines()) == len(before_text.splitlines())
    assert '\n    {"id": 0, "roi": ' in after_text  # one-line bay objects untouched
