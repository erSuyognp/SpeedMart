"""scripts/gen_bay_cards.py: printable bay number cards for the shelf front (no bay LEDs in this build)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_bay_cards  # noqa: E402


def test_cards_follow_config_bays_with_card_one_for_bay_zero():
    config = {"bays": [{"id": 1, "sku": "rec"}, {"id": 0, "sku": "elx"}]}
    catalog = {"skus": [{"sku": "elx", "name": "Electrolyte tabs"}, {"sku": "rec", "name": "Recovery drink"}]}
    assert gen_bay_cards.cards_from_config(config, catalog) == [("1", "Electrolyte tabs"), ("2", "Recovery drink")]


def test_real_config_gives_one_card_per_bay():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    cards = gen_bay_cards.cards_from_config(config, catalog)
    assert [c[0] for c in cards] == [str(i + 1) for i in range(len(config["bays"]))]


def test_main_writes_a_pdf(tmp_path):
    out = tmp_path / "cards.pdf"
    assert gen_bay_cards.main(["--out", str(out)]) == 0
    assert out.read_bytes()[:5] == b"%PDF-"
