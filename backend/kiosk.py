"""Entrance kiosk QR codes (S6.2 demo polish): GET /api/kiosk/qr/{join|enter|exit} -> PNG.

The kiosk tablet at the door shows the JOIN and ENTER codes (or the EXIT code on the exit side). It renders
them from this router instead of the printed sheet from scripts/gen_qr.py, so a tunnel domain change needs
only a backend restart.

The gate tokens live in .env and are embedded in the PNG pixels only. Nothing here returns them as text:
there is no route that echoes a URL, and the 404 for an unknown name says nothing about what was asked
for. Tokens reach the browser exactly as they do on the printed codes, by being scanned.
"""

from __future__ import annotations

import io
import threading
from urllib.parse import quote

import qrcode
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from backend.settings import settings

# QR name -> path on PUBLIC_ORIGIN. "enter" and "exit" carry their gate token as ?g=.
KINDS = ("join", "enter", "exit")

# qrcode tuning matches scripts/gen_qr.py (13.2) so a scanned screen behaves like a scanned print.
ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_M
BOX_SIZE = 12  # px per module; a 10" tablet shows two of these side by side comfortably
BORDER = 4  # quiet zone in modules, the spec minimum

_lock = threading.Lock()
_cache: dict[str, bytes] = {}  # url -> PNG bytes; built once per process


class UnknownKioskQr(ValueError):
    """Raised by url_for() when `which` is not one of KINDS."""


def url_for(which: str) -> str:
    """The URL encoded in the {which} QR code. Raises UnknownKioskQr for any other name."""
    origin = settings.env.public_origin.rstrip("/")
    if which == "join":
        return f"{origin}/"
    if which == "enter":
        return f"{origin}/enter.html?g={quote(settings.env.entry_gate_token, safe='')}"
    if which == "exit":
        return f"{origin}/exit.html?g={quote(settings.env.exit_gate_token, safe='')}"
    raise UnknownKioskQr(which)


def png_for_url(url: str) -> bytes:
    """PNG bytes for `url`, cached in memory (the origin and tokens never change while we run)."""
    with _lock:
        cached = _cache.get(url)
    if cached is not None:
        return cached
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECTION, box_size=BOX_SIZE, border=BORDER)
    qr.add_data(url)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
    png = buffer.getvalue()
    with _lock:
        _cache[url] = png
    return png


def clear_cache() -> None:
    """Drop the cached PNGs (used by tests)."""
    with _lock:
        _cache.clear()


router = APIRouter()


@router.get("/api/kiosk/qr/{which}", responses={200: {"content": {"image/png": {}}}})
def kiosk_qr(which: str):
    """The join / enter / exit QR code as a PNG for the entrance kiosk screen."""
    try:
        url = url_for(which)
    except UnknownKioskQr:
        # The requested name is not echoed back: this route must never reflect caller input.
        return JSONResponse(status_code=404,
                            content={"error": "unknown_qr",
                                     "message": "No such kiosk QR. Use join, enter or exit."})
    return Response(content=png_for_url(url), media_type="image/png",
                    # Same bytes for the life of the process; let the tablet keep them across reloads.
                    headers={"Cache-Control": "public, max-age=3600"})
