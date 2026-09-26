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
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from backend import eventlog, intent, kiosk_agent, tts, voice
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


# Every route on agent_router needs the token; the check runs before the body is even parsed, so a request
# without it gets 403 whatever it sends.
agent_router = APIRouter(dependencies=[Depends(require_kiosk)])


@agent_router.get("/api/kiosk/phrases")
def kiosk_phrases():
    """The spoken tour (Join, Enter, Grab items, Scan exit) and the fixed phrases, each with its caption text and
    an audio URL (null when speech is off). The kiosk calls this once at start; it also checks the token."""
    return kiosk_agent.script()


@agent_router.get("/api/kiosk/shopper")
def kiosk_shopper():
    """The shopper in the store, for the kiosk's panel and greeting: first name, budget, what is left, cart total,
    item count and visit count, plus the greeting to say (fuller on a first visit). Both null while the store is
    free or someone is returning items. Never a card, a balance, a receipt or item names."""
    return kiosk_agent.shopper_payload()


@agent_router.get("/api/kiosk/tts/{clip_id}", responses={200: {"content": {"audio/mpeg": {}}}})
def kiosk_tts(clip_id: str):
    """MP3 for a line the backend registered (tts.register). Unknown ids are 404: the browser can never choose the
    text. 503 `tts_unavailable` when speech is off or ElevenLabs fails or takes over 4 s: show the caption only."""
    text = tts.text_for(clip_id) if _CLIP_ID.match(clip_id) else None
    if text is None:
        raise ApiError(404, "unknown_clip", "No such line.")
    audio = tts.speak(text)
    if audio is None:
        raise ApiError(503, "tts_unavailable", "Speech is not available right now; showing captions.")
    # Same text, same voice, same bytes: let the tablet keep fixed phrases across the tour's repeats.
    cache = "private, max-age=86400" if tts.is_fixed(text) else "no-store"
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": cache})


# --- the kiosk conversation (features.voice): an ElevenLabs agent talking with the shopper in the store ---

NO_SHOPPER_MESSAGE = "Nobody is shopping right now."
# Sent with every get_cart result so the agent answers "is this my card?" from a tool, not from memory
# (the same wording as the phone's voice.js).
PAYMENT_NOTE = ("Demo card: every member gets a test Visa card (•••• 4242) in Stripe test mode, linked "
                "automatically. It is not the shopper's card, SpeedMart never asks for a card, and no real money moves.")


def _shopper_or_409(in_store_only: bool = False):
    """(session, cart, member) of the shopping visit holding the store, else 409 no_shopper."""
    visit = kiosk_agent.current_visit()
    if visit is None or (in_store_only and visit[0]["state"] != "IN_STORE"):
        raise ApiError(409, "no_shopper", NO_SHOPPER_MESSAGE)
    return visit


@agent_router.get("/api/kiosk/voice-session")
def kiosk_voice_session():
    """A signed URL for the ElevenLabs agent (the API key stays here) and the dynamic variables for the shopper in
    the store, with mode "kiosk". 404 `not_available` (voice off), 409 `no_shopper`, 503 `voice_unavailable`."""
    voice.require_voice()
    session, cart, member = _shopper_or_409(in_store_only=True)
    signed_url = voice.signed_url_or_503("kiosk_voice_session", session_id=session["id"])
    view = kiosk_agent.shopper_view(cart, member, kiosk_agent.visit_count(member["id"]))
    eventlog.log("kiosk_voice_session", session_id=session["id"])  # never the signed URL: it is a bearer token
    return {
        "signed_url": signed_url,
        "dynamic_variables": {
            "first_name": view["first_name"],
            "budget_usd": view["budget_usd"],
            "remaining_usd": view["remaining_usd"],
            "dietary": member.get("dietary") or "none",
            "is_first_visit": view["visit_count"] <= 1,
            "mode": "kiosk",
        },
        "max_duration_s": kiosk_agent.MAX_CONVERSATION_S,
        "silence_timeout_s": kiosk_agent.SILENCE_S,
    }


class ConversationIn(BaseModel):
    active: bool
    reason: str = ""


@agent_router.post("/api/kiosk/conversation")
def kiosk_conversation(body: ConversationIn):
    """The kiosk reports its conversation starting (after it connects) and ending. While it is active the
    shopper's phone shows "Talk to the kiosk" and /api/voice/session answers 409 kiosk_conversation_active."""
    if body.active:
        session, _, member = _shopper_or_409(in_store_only=True)
        kiosk_agent.start_conversation(session["id"], member["id"])
    else:
        kiosk_agent.end_conversation(reason=(body.reason or "kiosk")[:40])
    return {"ok": True, "active": body.active}


# Client tools on the kiosk. They act on the CURRENT shopper, never on whoever holds the kiosk.

@agent_router.get("/api/kiosk/catalog")
def kiosk_catalog():
    """get_catalog: the same products, prices, tags and bay cards the phone's agent gets."""
    voice.require_voice()
    return {"products": voice.catalog_products()}


@agent_router.get("/api/kiosk/cart")
def kiosk_cart():
    """get_cart: the shopper's live cart for the agent (names, quantities, totals, budget). Spoken, not shown."""
    voice.require_voice()
    visit = kiosk_agent.current_visit()
    if visit is None:
        return {"in_store": False, "items": [], "note": NO_SHOPPER_MESSAGE, "payment": PAYMENT_NOTE}
    session, cart, member = visit
    view = kiosk_agent.shopper_view(cart, member, 0)
    return {
        "in_store": True,
        "items": [{"name": i["name"], "qty": i["qty"], "line_total_usd": i["line_total_usd"]} for i in cart["items"]],
        "total_usd": cart["total_usd"],
        "budget_usd": view["budget_usd"],
        "remaining_usd": view["remaining_usd"],
        "over_budget": cart["over_budget"],
        "payment": PAYMENT_NOTE,
    }


class PlanIn(BaseModel):
    goal_text: str


@agent_router.post("/api/kiosk/plan")
def kiosk_make_plan(body: PlanIn):
    """make_plan: the F18 planner for the shopper in the store (a Plan, 8.8). Its bays glow on every shelf map."""
    voice.require_voice()
    session, _, member = _shopper_or_409()
    try:
        return intent.create_plan(member["id"], body.goal_text, via="kiosk")
    except intent.IntentError as e:
        raise ApiError(e.status, e.code, e.message) from None


@agent_router.delete("/api/kiosk/plan")
def kiosk_clear_plan():
    """clear_plan: forget the shopper's plan; the bays stop glowing."""
    voice.require_voice()
    session, _, member = _shopper_or_409()
    intent.clear_plan(member["id"], via="kiosk")
    return {"ok": True}
