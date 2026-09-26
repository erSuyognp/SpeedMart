"""Printable PDF of the bay number cards for the shelf front.

    python scripts/gen_bay_cards.py [--out tags/bay_cards.pdf]

One card per bay in config.json, left to right: a big number and the product name under it (from
catalog.json). Bay id 0 is card "1", the same numbering every shelf map and the gate screen use. 300 DPI US
Letter, 6 cards per page (2 x 3), each 10 x 8 cm inside a dashed cut line.

Print at 100% scale with "fit to page" OFF, cut along the dashed lines and tape each card to the shelf front
under its bay, outside the camera's bay ROIs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

DPI = 300
PAGE_W, PAGE_H = 2550, 3300  # US Letter at 300 DPI
CARD_W, CARD_H = 1181, 945  # 10 x 8 cm at 300 DPI
COLS, ROWS = 2, 3
MARGIN = 105  # ~0.35 in; most printers cannot print the outer 0.25 in
HEADER_H = 175
NUMBER_PX = 620  # glyph size of the card number, about 5 cm
NAME_PX = 80


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf") if bold else ()
    for name in (*names, "arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _dashed_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], dash: int = 20,
                 color=(170, 170, 170)) -> None:
    x1, y1, x2, y2 = box
    for x in range(x1, x2, 2 * dash):
        draw.line([(x, y1), (min(x + dash, x2), y1)], fill=color, width=2)
        draw.line([(x, y2), (min(x + dash, x2), y2)], fill=color, width=2)
    for y in range(y1, y2, 2 * dash):
        draw.line([(x1, y), (x1, min(y + dash, y2))], fill=color, width=2)
        draw.line([(x2, y), (x2, min(y + dash, y2))], fill=color, width=2)


def cards_from_config(config: dict, catalog: dict) -> list[tuple[str, str]]:
    """[(card number, product name)] left to right. Bay id 0 is card "1"."""
    names = {s["sku"]: s["name"] for s in catalog["skus"]}
    return [(str(int(b["id"]) + 1), names.get(b["sku"], b["sku"]))
            for b in sorted(config["bays"], key=lambda b: int(b["id"]))]


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: int, bold: bool = False) -> ImageFont.ImageFont:
    font = _font(size, bold)
    while size > 20 and draw.textlength(text, font=font) > max_w:
        size -= 4
        font = _font(size, bold)
    return font


def build_pages(cards: list[tuple[str, str]]) -> list[Image.Image]:
    per_page = COLS * ROWS
    n_pages = (len(cards) + per_page - 1) // per_page
    header_font = _font(36)
    pages = []
    for p in range(n_pages):
        page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        draw = ImageDraw.Draw(page)
        header = (f"SpeedMart bay cards  page {p + 1}/{n_pages}.  Print at 100% scale, fit to page OFF. "
                  "Tape each card to the shelf front under its bay, left to right.")
        draw.text((PAGE_W // 2, MARGIN), header, fill="black", font=header_font, anchor="mm")
        row_h = (PAGE_H - HEADER_H - MARGIN) // ROWS
        for i, (number, name) in enumerate(cards[p * per_page:(p + 1) * per_page]):
            col, row = i % COLS, i // COLS
            x = col * (PAGE_W // COLS) + (PAGE_W // COLS - CARD_W) // 2
            y = HEADER_H + row * row_h + (row_h - CARD_H) // 2
            _dashed_rect(draw, (x, y, x + CARD_W, y + CARD_H))
            cx = x + CARD_W // 2
            draw.text((cx, y + 40), "BAY", fill="black", font=_font(70, bold=True), anchor="mt")
            draw.text((cx, y + CARD_H // 2 + 10), number, fill="black",
                      font=_fit(draw, number, NUMBER_PX, CARD_W - 80, bold=True), anchor="mm")
            draw.text((cx, y + CARD_H - 50), name, fill="black",
                      font=_fit(draw, name, NAME_PX, CARD_W - 80), anchor="mb")
        pages.append(page)
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Printable PDF of the bay number cards for the shelf front.")
    parser.add_argument("--out", type=Path, default=ROOT / "tags" / "bay_cards.pdf")
    args = parser.parse_args(argv)

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    cards = cards_from_config(config, catalog)
    if not cards:
        print("config.json has no bays.", file=sys.stderr)
        return 1
    pages = build_pages(cards)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(args.out, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    print(f"Wrote {len(cards)} bay cards on {len(pages)} page(s) to {args.out}")
    print('Print at 100% scale with "fit to page" OFF and tape card 1 under the leftmost bay.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
