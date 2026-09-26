"""F19 voice store agent: hands the browser a short-lived ElevenLabs signed URL, never the API key.

The agent itself runs in ElevenLabs and can only act through client tools in web/js/pages/voice.js, which are
thin wrappers over the store's own endpoints (/api/intent, /api/store/current, /api/voice/catalog). See
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


@router.get("/api/voice/session")
def voice_session(request: Request):
    require_voice()
    member_id = require_member(request)
    member = _load_member(member_id)
    if member is None:
        raise ApiError(404, "unknown_member", "We couldn't find your membership. Please sign in again.")

    env = settings.env
    if not env.elevenlabs_api_key or not env.elevenlabs_agent_id:
        eventlog.log("voice_session_error", member_id=member_id, reason="not_configured")
        raise ApiError(503, "voice_unavailable", UNAVAILABLE_MESSAGE)
    try:
        signed_url = fetch_signed_url(env.elevenlabs_api_key, env.elevenlabs_agent_id)
    except httpx.HTTPStatusError as e:
        eventlog.log("voice_session_error", member_id=member_id, reason=f"http_{e.response.status_code}")
        raise ApiError(503, "voice_unavailable", UNAVAILABLE_MESSAGE) from None
    except Exception as e:  # timeout, network, odd response shape
        eventlog.log("voice_session_error", member_id=member_id, reason=type(e).__name__)
        raise ApiError(503, "voice_unavailable", UNAVAILABLE_MESSAGE) from None

    first_name = (member["name"] or "").split()[0] if (member["name"] or "").split() else "there"
    eventlog.log("voice_session", member_id=member_id)  # never log the signed URL: it is a bearer token
    return {
        "signed_url": signed_url,
        "member_first_name": first_name,
        "budget_usd": member["budget_usd"],
        "dietary": member.get("dietary") or "",
    }


@router.get("/api/voice/catalog")
def voice_catalog(request: Request):
    """What get_catalog tells the agent: products with price, tags and the bay they sit in."""
    require_voice()
    require_member(request)
    bays: dict[str, list[int]] = {}
    for b in settings.bays:
        bays.setdefault(b.sku, []).append(b.id)
    return {"products": [
        {"sku": s.sku, "name": s.name, "price_usd": to_usd(to_cents(s.price_usd)),
         "tags": [t for t in s.tags if not t.startswith("complements:")],
         "pairs_with": [t.split(":", 1)[1] for t in s.tags if t.startswith("complements:")],
         "bays": bays.get(s.sku, [])}
        for s in settings.skus.values()
    ]}
