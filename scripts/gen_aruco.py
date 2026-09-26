"""Printable PDF of ArUco tags at exact size (13.1).

    python scripts/gen_aruco.py [--out tags/aruco_tags.pdf]

One tag per unit in catalog.json, dictionary from config.json vision.aruco_dict (DICT_4X4_50).
300 DPI US Letter pages, 6 tags per page (2 x 3). Black marker exactly 6.0 cm (709 px) inside a 1 cm
white border (dashed cut line), label "ID <n> · <SKU name>" underneath.

Print at 100% scale with "fit to page" OFF, then check a black square with a ruler: 6.0 cm.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

DPI = 300
PAGE_W, PAGE_H = 2550, 3300  # US Letter at 300 DPI
MARKER_PX = 709  # 6.0 cm at 300 DPI
BORDER_PX = 118  # 1.0 cm at 300 DPI
TILE_PX = MARKER_PX + 2 * BORDER_PX
COLS, ROWS = 2, 3
MARGIN = 105  # ~0.35 in; most printers cannot print the outer 0.25 in
HEADER_H = 175
LABEL_H = 60
ROW_H = (PAGE_H - HEADER_H - MARGIN) // ROWS


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _dashed_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], dash: int = 20, color=(170, 170, 170)) -> None:
    x1, y1, x2, y2 = box
    for x in range(x1, x2, 2 * dash):
        draw.line([(x, y1), (min(x + dash, x2), y1)], fill=color, width=2)
        draw.line([(x, y2), (min(x + dash, x2), y2)], fill=color, width=2)
    for y in range(y1, y2, 2 * dash):
        draw.line([(x1, y), (x1, min(y + dash, y2))], fill=color, width=2)
        draw.line([(x2, y), (x2, min(y + dash, y2))], fill=color, width=2)


def marker_image(dictionary, tag_id: int) -> Image.Image:
    return Image.fromarray(cv2.aruco.generateImageMarker(dictionary, tag_id, MARKER_PX, borderBits=1)).convert("RGB")


def build_pages(units: list[dict], sku_names: dict[str, str], dict_name: str) -> list[Image.Image]:
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dict_name))
    label_font, header_font = _font(46), _font(36)
    per_page = COLS * ROWS
    n_pages = (len(units) + per_page - 1) // per_page
    pages = []
    for p in range(n_pages):
        page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        draw = ImageDraw.Draw(page)
        header = (f"SpeedMart ArUco tags  {dict_name}  page {p + 1}/{n_pages}.  Print at 100% scale, "
                  "fit to page OFF. Black square must measure 6.0 cm.")
        draw.text((PAGE_W // 2, MARGIN), header, fill="black", font=header_font, anchor="mm")
        for i, unit in enumerate(units[p * per_page:(p + 1) * per_page]):
            col, row = i % COLS, i // COLS
            cell_x = col * (PAGE_W // COLS)
            cell_y = HEADER_H + row * ROW_H
            x = cell_x + (PAGE_W // COLS - TILE_PX) // 2
            y = cell_y + (ROW_H - TILE_PX - LABEL_H) // 2
            _dashed_rect(draw, (x, y, x + TILE_PX, y + TILE_PX))
            page.paste(marker_image(dictionary, int(unit["tag_id"])), (x + BORDER_PX, y + BORDER_PX))
            label = f"ID {unit['tag_id']} · {sku_names.get(unit['sku'], unit['sku'])}"
            draw.text((x + TILE_PX // 2, y + TILE_PX + LABEL_H // 2), label, fill="black", font=label_font, anchor="mm")
        pages.append(page)
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Printable PDF of every unit tag in catalog.json.")
    parser.add_argument("--out", type=Path, default=ROOT / "tags" / "aruco_tags.pdf")
    args = parser.parse_args(argv)

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    units = sorted(catalog["units"], key=lambda u: u["tag_id"])
    if not units:
        print("catalog.json has no units.", file=sys.stderr)
        return 1
    sku_names = {s["sku"]: s["name"] for s in catalog["skus"]}
    pages = build_pages(units, sku_names, config["vision"]["aruco_dict"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(args.out, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    print(f"Wrote {len(units)} tags on {len(pages)} page(s) to {args.out}")
    print('Print at 100% scale with "fit to page" OFF, then measure a black square: it must be 6.0 cm.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
