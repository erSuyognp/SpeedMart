"""Generate SpeedMart PNG icons from the logo geometry, using Pillow only.

Mirrors web/assets/logo.svg (512-unit canvas): gradient rounded square, three
speed lines, a white shopping bag with a handle, and a brand-blue bolt.

Outputs (all in this folder):
    icon-192.png               rounded square, transparent corners   (manifest, purpose any)
    icon-512.png               rounded square, transparent corners   (manifest, purpose any)
    icon-maskable-512.png      full-bleed square, art in the 80% safe zone (manifest, purpose maskable)
    apple-touch-icon.png       180 px full-bleed square (iOS applies its own mask)
    icon-32.png                small favicon fallback

Run from the repo root (Windows):
    .venv\\Scripts\\python.exe web\\assets\\make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
CANVAS = 512            # logo.svg coordinate space
SUPERSAMPLE = 4         # draw big, shrink with LANCZOS for clean edges

BRAND = (26, 86, 219)       # #1a56db, bottom of the gradient and the bolt
BRAND_TOP = (46, 110, 245)  # #2e6ef5, top of the gradient
WHITE = (255, 255, 255)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def gradient_square(size: int) -> Image.Image:
    """Vertical gradient BRAND_TOP -> BRAND over a size x size RGBA image."""
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        draw.line([(0, y), (size, y)], fill=_lerp(BRAND_TOP, BRAND, y / max(1, size - 1)) + (255,))
    return img


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def artwork(size: int, scale: float = 1.0) -> Image.Image:
    """The bag, bolt and speed lines on a transparent layer, in logo.svg coordinates
    scaled to `size` and optionally shrunk toward the centre (for maskable icons)."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    k = size / CANVAS * scale
    off = size * (1 - scale) / 2

    def p(x: float, y: float) -> tuple[float, float]:
        return (off + x * k, off + y * k)

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        return (*p(x0, y0), *p(x1, y1))

    # speed lines: x, y, w, alpha  (h = 34, fully round ends)
    for x, y, w, alpha in ((52, 225, 140, 1.0), (92, 277, 100, 0.72), (132, 329, 60, 0.45)):
        draw.rounded_rectangle(box(x, y, x + w, y + 34), radius=17 * k, fill=WHITE + (round(255 * alpha),))

    # handle: semicircle centred (336, 206), centreline radius 50, stroke 22, plus straight legs to y=226
    stroke = 22
    outer = 50 + stroke / 2
    draw.arc(box(336 - outer, 206 - outer, 336 + outer, 206 + outer), start=180, end=360,
             fill=WHITE + (255,), width=round(stroke * k))
    for cx in (286, 386):
        draw.rectangle(box(cx - stroke / 2, 206, cx + stroke / 2, 226), fill=WHITE + (255,))

    # bag body
    draw.rounded_rectangle(box(216, 206, 456, 382), radius=34 * k, fill=WHITE + (255,))

    # bolt
    bolt = [(346, 234), (288, 301), (331, 301), (317, 354), (384, 282), (338, 282)]
    draw.polygon([p(x, y) for x, y in bolt], fill=BRAND + (255,))
    return layer


def render(size: int, rounded: bool, art_scale: float = 1.0) -> Image.Image:
    big = size * SUPERSAMPLE
    img = gradient_square(big)
    img.alpha_composite(artwork(big, art_scale))
    if rounded:
        img.putalpha(rounded_mask(big, round(big * 116 / CANVAS)))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    jobs = [
        ("icon-192.png", 192, True, 1.0),
        ("icon-512.png", 512, True, 1.0),
        ("icon-maskable-512.png", 512, False, 0.8),
        ("apple-touch-icon.png", 180, False, 1.0),
        ("icon-32.png", 32, True, 1.0),
    ]
    for name, size, rounded, art_scale in jobs:
        img = render(size, rounded, art_scale)
        if not rounded:
            img = img.convert("RGB")  # iOS / maskable icons must be opaque
        out = HERE / name
        img.save(out, optimize=True)
        print(f"wrote {out.relative_to(HERE.parent.parent)}  {size}x{size}")


if __name__ == "__main__":
    main()
