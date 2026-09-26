"""F19 voice store agent: hands the browser a short-lived ElevenLabs signed URL, never the API key.

The agent itself runs in ElevenLabs and can only act through client tools: on the phone in web/js/pages/voice.js
(thin wrappers over /api/intent, /api/store/current, /api/voice/catalog), on the entrance kiosk in
web/js/kiosk_agent.js (the kiosk token routes in backend/kiosk.py, acting on the shopper in the store). See
docs/voice.md for the agent configuration and the doc pages these calls come from.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request

from backend import db, eventlog
from backend.cart import to_cents, to_usd
from backend.routes_api import ApiError, require_member
from backend.settings import settings

# https://elevenlabs.io/docs/api-reference/conversations/get-signed-url
SIGNED_URL_ENDPOINT = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
ELEVENLABS_TIMEOUT_S = 5.0
UNAVAILABLE_MESSAGE = "Voice isn't available right now. You can type what you need instead."
KIOSK_ACTIVE_MESSAGE = "You're talking to the kiosk by the shelf right now."

router = APIRouter()


def require_voice() -> None:
    if not settings.features.voice:
        raise ApiError(404, "not_available", "Voice is turned off. You can type what you need instead.")


def _load_member(member_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_signed_url(api_key: str, agent_id: str) -> str:
    """Signed wss URL for a private agent (valid 15 minutes). Raises on any HTTP or shape problem."""
    r = httpx.get(SIGNED_URL_ENDPOINT, params={"agent_id": agent_id}, headers={"xi-api-key": api_key},
                  timeout=ELEVENLABS_TIMEOUT_S)
    r.raise_for_status()
    url = r.json()["signed_url"]
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise ValueError("signed_url_missing")
    return url


def signed_url_or_503(event: str, **fields: Any) -> str:
    """A signed URL for the configured agent, or ApiError 503 voice_unavailable. Failures are logged as
    `<event>_error` with a reason; the URL itself is never logged (anyone holding it can open a session)."""
    env = settings.env
    if not env.elevenlabs_api_key or not env.elevenlabs_agent_id:
        eventlog.log(f"{event}_error", **fields, reason="not_configured")
        raise ApiError(503, "voice_unavailable", UNAVAILABLE_MESSAGE)
    try:
        return fetch_signed_url(env.elevenlabs_api_key, env.elevenlabs_agent_id)
    except httpx.HTTPStatusError as e:
        eventlog.log(f"{event}_error", **fields, reason=f"http_{e.response.status_code}")
        raise ApiError(503, "voice_unavailable", UNAVAILABLE_MESSAGE) from None
    except Exception as e:  # timeout, network, odd response shape
        eventlog.log(f"{event}_error", **fields, reason=type(e).__name__)
        raise ApiError(503, "voice_unavailable", UNAVAILABLE_MESSAGE) from None


@router.get("/api/voice/session")
def voice_session(request: Request):
    from backend import kiosk_agent, store  # local: kiosk_agent imports modules that import this one

    require_voice()
    member_id = require_member(request)
    member = _load_member(member_id)
    if member is None:
        raise ApiError(404, "unknown_member", "We couldn't find your membership. Please sign in again.")
    # One conversation at a time: while the kiosk by the shelf is talking with this shopper, the phone waits.
    if kiosk_agent.conversation_active_for(member_id):
        eventlog.log("voice_session_error", member_id=member_id, reason="kiosk_conversation_active")
        raise ApiError(409, "kiosk_conversation_active", KIOSK_ACTIVE_MESSAGE)

    signed_url = signed_url_or_503("voice_session", member_id=member_id)

    # The same dynamic variables as the kiosk (docs/voice.md), so one agent serves both: mode says which.
    session = store.current_session()
    in_store = (session is not None and session["member_id"] == member_id and not session.get("return_of")
                and session["state"] in store.SHOPPING_STATES)
    total = to_cents(store.cart_for(session)["total_usd"]) if in_store else 0
    earlier_visits = kiosk_agent.visit_count(member_id) - (1 if in_store else 0)
    eventlog.log("voice_session", member_id=member_id)  # never log the signed URL: it is a bearer token
    return {
        "signed_url": signed_url,
        "member_first_name": kiosk_agent.first_name(member["name"]),
        "budget_usd": member["budget_usd"],
        "remaining_usd": to_usd(max(0, to_cents(member["budget_usd"]) - total)),
        "dietary": member.get("dietary") or "",
        "is_first_visit": earlier_visits <= 0,
        "mode": "phone",
    }


def catalog_products() -> list[dict[str, Any]]:
    """What get_catalog tells the agent (phone and kiosk): products with price, tags and the bay they sit in. Bays
    are the numbers on the printed shelf cards (bay id 0 is card 1), so the agent says what the shopper can see."""
    bays: dict[str, list[int]] = {}
    for b in settings.bays:
        bays.setdefault(b.sku, []).append(b.id + 1)
    return [
        {"sku": s.sku, "name": s.name, "price_usd": to_usd(to_cents(s.price_usd)),
         "tags": [t for t in s.tags if not t.startswith("complements:")],
         "pairs_with": [t.split(":", 1)[1] for t in s.tags if t.startswith("complements:")],
         "bays": bays.get(s.sku, [])}
        for s in settings.skus.values()
    ]


@router.get("/api/voice/catalog")
def voice_catalog(request: Request):
    require_voice()
    require_member(request)
    return {"products": catalog_products()}
