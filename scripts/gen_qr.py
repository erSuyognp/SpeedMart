"""Printable QR codes 1 · JOIN, 2 · ENTER, 3 · EXIT (13.2).

    python scripts/gen_qr.py                 reads PUBLIC_ORIGIN, ENTRY_GATE_TOKEN, EXIT_GATE_TOKEN from .env
    python scripts/gen_qr.py --out qr        output folder (default qr/, gitignored: the codes hold gate tokens)

Writes qr/1_join.png, qr/2_enter.png, qr/3_exit.png and qr/all.pdf (one US Letter page per code, 300 DPI).
Re-run whenever PUBLIC_ORIGIN or a gate token changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

import qrcode
from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
PAGE = (2550, 3300)  # US Letter at 300 DPI
FONT_CANDIDATES = ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
                   "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def font(size: int) -> ImageFont.ImageFont:
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)  # Pillow >= 10.1 scales its built-in font


def codes(origin: str, entry: str, exit_: str) -> list[tuple[str, str, str]]:
    """(file stem, label, url) for each code."""
    return [
        ("1_join", "1 · JOIN", f"{origin}/"),
        ("2_enter", "2 · ENTER", f"{origin}/enter.html?g={quote(entry, safe='')}"),
        ("3_exit", "3 · EXIT", f"{origin}/exit.html?g={quote(exit_, safe='')}"),
    ]


def make_card(label: str, url: str) -> Image.Image:
    """QR (error correction M, box 20, border 4) with a huge bold label below and the URL in small text."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=20, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    big, small = font(180), font(40)
    probe = ImageDraw.Draw(code)
    label_box = probe.textbbox((0, 0), label, font=big)
    url_box = probe.textbbox((0, 0), url, font=small)
    width = max(code.width, label_box[2] - label_box[0] + 120, url_box[2] - url_box[0] + 120)
    height = code.height + 40 + (label_box[3] - label_box[1]) + 60 + (url_box[3] - url_box[1]) + 80

    card = Image.new("RGB", (width, height), "white")
    card.paste(code, ((width - code.width) // 2, 0))
    draw = ImageDraw.Draw(card)
    y = code.height + 40
    draw.text(((width - (label_box[2] - label_box[0])) // 2 - label_box[0], y - label_box[1]), label,
              font=big, fill="black")
    y += (label_box[3] - label_box[1]) + 60
    draw.text(((width - (url_box[2] - url_box[0])) // 2 - url_box[0], y - url_box[1]), url,
              font=small, fill=(90, 90, 90))
    return card


def on_page(card: Image.Image) -> Image.Image:
    page = Image.new("RGB", PAGE, "white")
    scale = min((PAGE[0] - 300) / card.width, (PAGE[1] - 300) / card.height, 1.0)
    if scale < 1.0:
        card = card.resize((int(card.width * scale), int(card.height * scale)), Image.LANCZOS)
    page.paste(card, ((PAGE[0] - card.width) // 2, (PAGE[1] - card.height) // 2))
    return page


def generate(origin: str, entry: str, exit_: str, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written, pages = [], []
    for stem, label, url in codes(origin, entry, exit_):
        card = make_card(label, url)
        path = out / f"{stem}.png"
        card.save(path, dpi=(300, 300))
        written.append(path)
        pages.append(on_page(card))
    pdf = out / "all.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=300)
    written.append(pdf)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the SpeedMart QR codes.")
    parser.add_argument("--out", default=str(ROOT / "qr"), help="output folder (default: qr/)")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="env file (default: .env)")
    args = parser.parse_args(argv)

    env = dotenv_values(args.env)
    origin = (env.get("PUBLIC_ORIGIN") or "").strip().rstrip("/")
    entry = (env.get("ENTRY_GATE_TOKEN") or "").strip()
    exit_ = (env.get("EXIT_GATE_TOKEN") or "").strip()
    missing = [k for k, v in (("PUBLIC_ORIGIN", origin), ("ENTRY_GATE_TOKEN", entry), ("EXIT_GATE_TOKEN", exit_)) if not v]
    if missing:
        print(f"Missing in {args.env}: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not origin.startswith("https://"):
        print(f"Warning: PUBLIC_ORIGIN is {origin}, not https. Face ID needs the https tunnel domain.", file=sys.stderr)

    for path in generate(origin, entry, exit_, Path(args.out)):
        print(f"wrote {path}")
    print("Print all.pdf at 100% scale. Codes 2 and 3 contain the gate tokens: keep them off public posts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
