"""YOLO data tooling tests: capture session logic and the dataset checker. No camera, no ultralytics."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from training import check_dataset as cd
from training.capture import CaptureSession, draw_capture_overlay, session_name

ROOT = Path(__file__).resolve().parent.parent
CLASSES = ["electrolytes", "protein_bar", "recovery_drink"]  # Roboflow's alphabetical order


# --- capture --------------------------------------------------------------------------------------


def test_session_name_is_sortable_timestamp():
    assert session_name(datetime(2026, 9, 26, 7, 5, 3)) == "20260926-070503"


def test_auto_save_every_interval(tmp_path):
    s = CaptureSession(tmp_path, "run", interval_s=0.5)
    assert not s.auto_due(0.0)  # off by default
    s.toggle_auto(10.0)
    due = [t for t in np.arange(10.0, 12.01, 0.1) if s.auto_due(round(float(t), 2))]
    assert [round(float(t), 1) for t in due] == [10.0, 10.5, 11.0, 11.5, 12.0]
    s.toggle_auto(12.1)
    assert not s.auto_due(20.0)


def test_auto_save_does_not_burst_after_stall(tmp_path):
    s = CaptureSession(tmp_path, "run", interval_s=0.5)
    s.toggle_auto(0.0)
    assert s.auto_due(0.0)
    assert s.auto_due(5.0)  # 5 s stall: one frame, not ten
    assert not s.auto_due(5.1)
    assert s.auto_due(5.5)


def test_save_writes_clean_frames_into_run_folder(tmp_path):
    s = CaptureSession(tmp_path, "20260926-070503")
    assert not s.folder.exists()  # created lazily
    frame = np.full((72, 128, 3), 77, np.uint8)
    p1 = s.save(frame, tags_visible=2)
    p2 = s.save(frame, tags_visible=0)
    assert p1 == tmp_path / "20260926-070503" / "20260926-070503_0001.jpg"
    assert p2.name == "20260926-070503_0002.jpg"
    assert s.saved == 2 and s.saved_without_tags == 1 and s.no_tag_share == 0.5
    img = cv2.imread(str(p1))
    assert img.shape == frame.shape and abs(float(img.mean()) - 77) < 2  # no overlay drawn on it


def test_overlay_is_preview_only():
    frame = np.zeros((720, 1280, 3), np.uint8)
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    s = CaptureSession(Path("x"), "run", auto=True, saved=10, saved_without_tags=3)
    view = draw_capture_overlay(frame, config["bays"], s, tags_visible=4, fps=30.0, message="saved")
    assert view.any() and not frame.any()


# --- dataset checker --------------------------------------------------------------------------


def make_dataset(root: Path, names=CLASSES, per_split=None, yaml_text: str | None = None) -> Path:
    """Roboflow YOLOv8 layout. per_split: {split: [label text per image]}."""
    per_split = per_split or {
        "train": ["0 0.5 0.5 0.2 0.3\n1 0.2 0.2 0.1 0.1\n", "2 0.7 0.7 0.1 0.2\n", ""],
        "valid": ["0 0.4 0.4 0.2 0.2\n2 0.6 0.6 0.2 0.2\n1 0.1 0.1 0.05 0.05\n"],
    }
    root.mkdir(parents=True, exist_ok=True)
    if yaml_text is None:
        yaml_text = ("train: ../train/images\nval: ../valid/images\ntest: ../test/images\n\n"
                     f"nc: {len(names)}\nnames: {names!r}\n")
    (root / "data.yaml").write_text(yaml_text, encoding="utf-8")
    for split, labels in per_split.items():
        (root / split / "images").mkdir(parents=True)
        (root / split / "labels").mkdir(parents=True)
        for i, text in enumerate(labels):
            cv2.imwrite(str(root / split / "images" / f"img{i}.jpg"), np.zeros((8, 8, 3), np.uint8))
            if text is not None:
                (root / split / "labels" / f"img{i}.txt").write_text(text, encoding="utf-8")
    return root


def test_catalog_classes_match_catalog():
    assert sorted(cd.catalog_classes()) == CLASSES


def test_good_dataset_passes_with_counts(tmp_path):
    rep = cd.check_dataset(make_dataset(tmp_path / "ds"), min_boxes=1)
    assert rep.ok, rep.errors
    assert rep.boxes["train"] == {"electrolytes": 1, "protein_bar": 1, "recovery_drink": 1}
    assert rep.total_boxes() == {"electrolytes": 2, "protein_bar": 2, "recovery_drink": 2}
    assert rep.images == {"train": 3, "valid": 1}
    assert rep.background["train"] == 1
    assert not rep.warnings
    text = cd.format_report(rep)
    assert "RESULT: OK" in text and "electrolytes" in text


def test_warns_below_100_boxes(tmp_path):
    rep = cd.check_dataset(make_dataset(tmp_path / "ds"))
    assert rep.ok
    assert sum("fewer" in w or "only" in w for w in rep.warnings) == 3


def test_class_name_mismatch_is_error(tmp_path):
    rep = cd.check_dataset(make_dataset(tmp_path / "ds", names=["electrolytes", "protein bar", "recovery_drink"]),
                           min_boxes=1)
    assert not rep.ok
    assert any("protein_bar" in e for e in rep.errors) and any("'protein bar'" in e for e in rep.errors)


def test_nc_mismatch_is_error(tmp_path):
    ds = make_dataset(tmp_path / "ds", yaml_text=f"nc: 4\nnames: {CLASSES!r}\n")
    assert any("nc is 4" in e for e in cd.check_dataset(ds, min_boxes=1).errors)


def test_malformed_labels_are_errors(tmp_path):
    ds = make_dataset(tmp_path / "ds", per_split={"train": [
        "0 0.5 0.5 0.2\n",  # too short
        "1 0.1 0.1 0.2 0.1 0.3 0.3\n",  # polygon
        "7 0.5 0.5 0.2 0.2\n",  # class out of range
        "0 1.5 0.5 0.2 0.2\n",  # not normalized
        "x 0.5 0.5 0.2 0.2\n",  # not a number
    ], "valid": ["0 0.5 0.5 0.1 0.1\n"]})
    rep = cd.check_dataset(ds, min_boxes=1)
    assert len(rep.errors) == 5
    assert any("segmentation" in e for e in rep.errors)
    assert any("out of range" in e for e in rep.errors)


def test_missing_label_and_orphan_label_warn(tmp_path):
    ds = make_dataset(tmp_path / "ds", per_split={"train": ["0 0.5 0.5 0.1 0.1\n", None],
                                                   "valid": ["1 0.5 0.5 0.1 0.1\n"]})
    (ds / "valid" / "labels" / "ghost.txt").write_text("2 0.5 0.5 0.1 0.1\n", encoding="utf-8")
    rep = cd.check_dataset(ds, min_boxes=1)
    assert rep.ok
    assert any("without a label file" in w for w in rep.warnings)
    assert any("without an image" in w for w in rep.warnings)


def test_missing_data_yaml_and_missing_valid(tmp_path):
    assert not cd.check_dataset(tmp_path).ok
    ds = make_dataset(tmp_path / "ds", per_split={"train": ["0 0.5 0.5 0.1 0.1\n"]})
    assert any("no valid/" in w for w in cd.check_dataset(ds, min_boxes=1).warnings)


@pytest.mark.parametrize("names_block", [
    "names: ['electrolytes', 'protein_bar', 'recovery_drink']\n",
    "names:\n- electrolytes\n- protein_bar\n- recovery_drink\n",
    "names:\n  0: electrolytes\n  1: protein_bar\n  2: recovery_drink\n",
])
def test_fallback_yaml_parser(names_block):
    names, nc = cd._parse_names_fallback("path: x\nnc: 3\n" + names_block + "roboflow:\n  workspace: w\n")
    assert names == CLASSES and nc == 3


def test_repo_data_yaml_matches_catalog():
    names, nc = cd.read_data_yaml(ROOT / "training" / "data.yaml")
    assert sorted(names) == CLASSES and nc == 3


def test_cli_exit_codes(tmp_path, capsys):
    assert cd.main([str(make_dataset(tmp_path / "ok")), "--min-boxes", "1"]) == 0
    assert cd.main([str(tmp_path / "missing")]) == 1
    assert "RESULT" in capsys.readouterr().out


def test_notebook_is_valid_and_uses_spec_settings():
    nb = json.loads((ROOT / "training" / "train_colab.ipynb").read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    code = "".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
    for needle in ("ultralytics", "yolo11n.pt", "epochs=80", "imgsz=640", "batch=16", "patience=20",
                   "files.download", "class_result"):
        assert needle in code
    for name in CLASSES:
        assert name in code
