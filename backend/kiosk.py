"""Entrance kiosk: QR codes (public) and the kiosk agent's token-only routes (docs/voice.md, "Kiosk").

Public: GET /api/kiosk/qr/{join|enter|exit} -> PNG (S6.2 demo polish).

Kiosk only: every other /api/kiosk/* route needs ?k=<KIOSK_TOKEN>, compared in constant time. With KIOSK_TOKEN
empty they all answer 403, so the tablet stays the public page: QR codes, store status and the shelf map, no voice
and no shopper data. The token is the kiosk's only credential; it never signs anyone in.

The kiosk tablet at the door shows the JOIN and ENTER codes (or the EXIT code on the exit side). It renders
them from this router instead of the printed sheet from scripts/gen_qr.py, so a tunnel domain change needs
only a backend restart.

The gate tokens live in .env and are embedded in the PNG pixels only. Nothing here returns them as text:
there is no route that echoes a URL, and the 404 for an unknown name says nothing about what was asked
for. Tokens reach the browser exactly as they do on the printed codes, by being scanned.
"""

from __future__ import annotations

import io
import re
import secrets
import threading
from urllib.parse import quote

import qrcode
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend import eventlog, kiosk_agent, tts
from backend.routes_api import ApiError
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


# --- kiosk agent: token-only routes ---

KIOSK_ONLY_MESSAGE = "This screen is for the SpeedMart kiosk only."
_CLIP_ID = re.compile(r"^[0-9a-f]{32}$")


def token_ok(given: str | None) -> bool:
    """True when `given` is the configured KIOSK_TOKEN (constant time). Always False while it is unset."""
    expected = settings.env.kiosk_token
    if not expected or not given:
        return False
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_kiosk(request: Request) -> None:
    """403 unless the request carries ?k=<KIOSK_TOKEN>. Logged without the token."""
    if not token_ok(request.query_params.get("k")):
        eventlog.log("kiosk_bad_token", path=request.url.path)
        raise ApiError(403, "kiosk_only", KIOSK_ONLY_MESSAGE)


@router.get("/api/kiosk/phrases")
def kiosk_phrases(request: Request):
    """The spoken tour (Join, Enter, Grab items, Scan exit) and the fixed phrases, each with its caption text and
    an audio URL (null when speech is off). The kiosk calls this once at start; it also checks the token."""
    require_kiosk(request)
    return kiosk_agent.script()


@router.get("/api/kiosk/shopper")
def kiosk_shopper(request: Request):
    """The shopper in the store, for the kiosk's panel and greeting: first name, budget, what is left, cart total,
    item count and visit count, plus the greeting to say (fuller on a first visit). Both null while the store is
    free or someone is returning items. Never a card, a balance, a receipt or item names."""
    require_kiosk(request)
    return kiosk_agent.shopper_payload()


@router.get("/api/kiosk/tts/{clip_id}", responses={200: {"content": {"audio/mpeg": {}}}})
def kiosk_tts(clip_id: str, request: Request):
    """MP3 for a line the backend registered (tts.register). Unknown ids are 404: the browser can never choose the
    text. 503 `tts_unavailable` when speech is off or ElevenLabs fails or takes over 4 s: show the caption only."""
    require_kiosk(request)
    text = tts.text_for(clip_id) if _CLIP_ID.match(clip_id) else None
    if text is None:
        raise ApiError(404, "unknown_clip", "No such line.")
    audio = tts.speak(text)
    if audio is None:
        raise ApiError(503, "tts_unavailable", "Speech is not available right now; showing captions.")
    # Same text, same voice, same bytes: let the tablet keep fixed phrases across the tour's repeats.
    cache = "private, max-age=86400" if tts.is_fixed(text) else "no-store"
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": cache})
