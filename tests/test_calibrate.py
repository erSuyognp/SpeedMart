"""S2.1 tests: calibrate.py config save logic. No camera needed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vision.calibrate import SaveError, save_rois, validate_rois

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
N_BAYS = len(CONFIG["bays"])  # calibrate.py always draws one rectangle per bay in config.json


def strip(n: int = N_BAYS, width: int = 1280) -> list[list[int]]:
    """n non-overlapping rectangles laid out left to right inside a `width` px frame."""
    step = width // n
    return [[i * step + 10, 20, (i + 1) * step - 10, 600] for i in range(n)]


GOOD = strip()


@pytest.fixture
def cfg(tmp_path) -> Path:
    path = tmp_path / "config.json"
    shutil.copyfile(ROOT / "config.json", path)
    return path


def test_valid_rois_pass():
    assert validate_rois(GOOD, N_BAYS, 1280, 720) is None


def test_wrong_count_rejected():
    assert f"Need {N_BAYS}" in validate_rois(GOOD[:-1], N_BAYS, 1280, 720)
    assert f"Need {N_BAYS}" in validate_rois(GOOD + [[0, 0, 40, 10]], N_BAYS, 1280, 720)


def test_overlap_rejected():
    rois = [list(r) for r in GOOD]
    rois[1][0] = GOOD[0][2] - 10  # bay 1 now reaches back into bay 0
    assert "overlap" in validate_rois(rois, N_BAYS, 1280, 720)


def test_touching_edges_allowed():
    rois = [[i * 100, 0, (i + 1) * 100, 100] for i in range(N_BAYS)]
    assert validate_rois(rois, N_BAYS, 1280, 720) is None


def test_outside_frame_and_non_int_rejected():
    too_wide = GOOD[:-1] + [[GOOD[-1][0], 20, 1300, 600]]
    assert "outside" in validate_rois(too_wide, N_BAYS, 1280, 720)
    not_int = GOOD[:-1] + [[GOOD[-1][0] + 0.5, 20, GOOD[-1][2], 600]]
    assert "integers" in validate_rois(not_int, N_BAYS, 1280, 720)


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
    assert len(changed) == N_BAYS and all('"roi"' in a for a, _ in changed)
    assert f'"roi": [{GOOD[0][0]}, {GOOD[0][1]}, {GOOD[0][2]}, {GOOD[0][3]}]' in after_text
    assert '\n  "camera": {\n    "index"' in after_text  # 2 space indent kept


def test_refused_save_leaves_file_untouched(cfg):
    before = cfg.read_bytes()
    with pytest.raises(SaveError, match="overlap"):
        save_rois([GOOD[0]] + GOOD[1:-1] + [GOOD[0]], cfg)
    with pytest.raises(SaveError, match=f"Need {N_BAYS}"):
        save_rois(GOOD[:-1], cfg)
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
