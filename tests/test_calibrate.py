"""S2.1 tests: calibrate.py config save logic. No camera needed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vision.calibrate import SaveError, save_rois, validate_rois

ROOT = Path(__file__).resolve().parent.parent
GOOD = [[10, 20, 300, 600], [320, 20, 640, 600], [660, 20, 1000, 600]]


@pytest.fixture
def cfg(tmp_path) -> Path:
    path = tmp_path / "config.json"
    shutil.copyfile(ROOT / "config.json", path)
    return path


def test_valid_rois_pass():
    assert validate_rois(GOOD, 3, 1280, 720) is None


def test_wrong_count_rejected():
    assert "Need 3" in validate_rois(GOOD[:2], 3, 1280, 720)
    assert "Need 3" in validate_rois(GOOD + [[1100, 0, 1200, 100]], 3, 1280, 720)


def test_overlap_rejected():
    rois = [GOOD[0], [290, 20, 640, 600], GOOD[2]]
    assert "overlap" in validate_rois(rois, 3, 1280, 720)


def test_touching_edges_allowed():
    rois = [[0, 0, 100, 100], [100, 0, 200, 100], [200, 0, 300, 100]]
    assert validate_rois(rois, 3, 1280, 720) is None


def test_outside_frame_and_non_int_rejected():
    assert "outside" in validate_rois([GOOD[0], GOOD[1], [660, 20, 1300, 600]], 3, 1280, 720)
    assert "integers" in validate_rois([GOOD[0], GOOD[1], [660.5, 20, 1000, 600]], 3, 1280, 720)


def test_save_writes_only_rois_and_keeps_formatting(cfg):
    before_text = cfg.read_text(encoding="utf-8")
    before = json.loads(before_text)

    save_rois(GOOD, cfg)

    after_text = cfg.read_text(encoding="utf-8")
    after = json.loads(after_text)
    assert [b["roi"] for b in after["bays"]] == GOOD
    for bay in before["bays"]:
        bay.pop("roi")
    for bay in after["bays"]:
        bay.pop("roi")
    assert after == before  # every other key unchanged

    # formatting: identical text apart from the roi arrays
    lines_before, lines_after = before_text.splitlines(), after_text.splitlines()
    assert len(lines_before) == len(lines_after)
    changed = [(a, b) for a, b in zip(lines_before, lines_after) if a != b]
    assert len(changed) == 3 and all('"roi"' in a for a, _ in changed)
    assert '"roi": [10, 20, 300, 600]' in after_text
    assert '\n  "camera": {\n    "index"' in after_text  # 2 space indent kept


def test_refused_save_leaves_file_untouched(cfg):
    before = cfg.read_bytes()
    with pytest.raises(SaveError, match="overlap"):
        save_rois([GOOD[0], GOOD[0], GOOD[2]], cfg)
    with pytest.raises(SaveError, match="Need 3"):
        save_rois(GOOD[:2], cfg)
    assert cfg.read_bytes() == before


def test_fallback_rewrite_uses_two_space_indent(tmp_path):
    # a layout the in-place replacer cannot handle (extra "roi" key outside bays) still saves correctly
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    config["notes"] = {"roi": [1, 2, 3, 4]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    save_rois(GOOD, path)

    text = path.read_text(encoding="utf-8")
    after = json.loads(text)
    assert [b["roi"] for b in after["bays"]] == GOOD
    assert after["notes"] == {"roi": [1, 2, 3, 4]}
    assert text.startswith('{\n  "features": {\n    "motion_freeze"')
