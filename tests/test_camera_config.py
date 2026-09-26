"""S2.1 exposure fix tests: saving camera exposure and gain into config.json. No camera needed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vision.camera import save_camera_settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path) -> Path:
    path = tmp_path / "config.json"
    shutil.copyfile(ROOT / "config.json", path)
    return path


def _without_camera_keys(config: dict, *keys: str) -> dict:
    for key in keys:
        config["camera"].pop(key, None)
    return config


def test_save_exposure_and_gain_only_changes_those_keys(cfg):
    before_text = cfg.read_text(encoding="utf-8")
    before = json.loads(before_text)

    save_camera_settings({"exposure": -5, "gain": 180}, cfg)

    after_text = cfg.read_text(encoding="utf-8")
    after = json.loads(after_text)
    assert after["camera"]["exposure"] == -5
    assert after["camera"]["gain"] == 180
    assert _without_camera_keys(after, "exposure", "gain") == _without_camera_keys(before, "exposure", "gain")

    # formatting kept: same line count, only the two lines changed
    changed = [(a, b) for a, b in zip(before_text.splitlines(), after_text.splitlines()) if a != b]
    assert len(before_text.splitlines()) == len(after_text.splitlines())
    assert sorted(b.strip() for _, b in changed) == ['"exposure": -5,', '"gain": 180,']
    assert '\n    {"id": 0, "roi": ' in after_text  # one-line bay objects untouched


def test_save_back_to_null(cfg):
    save_camera_settings({"exposure": -4, "gain": 222}, cfg)
    save_camera_settings({"exposure": None, "gain": None}, cfg)
    camera = json.loads(cfg.read_text(encoding="utf-8"))["camera"]
    assert camera["exposure"] is None and camera["gain"] is None


def test_float_values_saved(cfg):
    save_camera_settings({"exposure": 156.5, "gain": 0}, cfg)
    camera = json.loads(cfg.read_text(encoding="utf-8"))["camera"]
    assert camera["exposure"] == 156.5 and camera["gain"] == 0


def test_missing_gain_key_is_added_with_same_indent(cfg):
    config = json.loads(cfg.read_text(encoding="utf-8"))
    del config["camera"]["gain"]
    text = json.dumps(config, indent=2)
    cfg.write_text(text + "\n", encoding="utf-8")

    save_camera_settings({"exposure": -5, "gain": 100}, cfg)

    after_text = cfg.read_text(encoding="utf-8")
    after = json.loads(after_text)
    assert after["camera"]["gain"] == 100 and after["camera"]["exposure"] == -5
    assert _without_camera_keys(after, "exposure", "gain") == _without_camera_keys(config, "exposure", "gain")
    assert '\n    "gain": 100\n  },' in after_text


def test_crlf_line_endings_preserved(cfg):
    text = cfg.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    cfg.write_bytes(text.encode("utf-8"))
    save_camera_settings({"exposure": -3, "gain": 50}, cfg)
    raw = cfg.read_bytes().decode("utf-8")
    assert raw.count("\r\n") == raw.count("\n")
    assert json.loads(raw)["camera"]["exposure"] == -3
