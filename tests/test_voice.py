"""F19 voice store agent: /api/voice/session with the ElevenLabs call mocked, and the endpoints the client tools wrap."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import db, eventlog, intent, shelf_state, voice
from backend.main import app
from identity_helpers import login_as, set_env, set_features
from test_cart import FULL, snap, tmp_data  # noqa: F401  (tmp_data: temp DB + event log per test)

pytestmark = pytest.mark.usefixtures("tmp_data")

KEY = "sk_test_voice_key_never_in_browser"
AGENT = "agent_test123"
SIGNED = "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=agent_test123&conversation_signature=sig"


@pytest.fixture(autouse=True)
def voice_on(monkeypatch):
    set_features(monkeypatch, voice=True, llm=False, gates=False, hardware_leds=False)
    intent._plans.clear()
    yield
    intent._plans.clear()


@pytest.fixture
def keys(monkeypatch):
    set_env(monkeypatch, elevenlabs_api_key=KEY, elevenlabs_agent_id=AGENT)


def add_member(name="Maya Lopez", budget=15.0, dietary="vegan") -> str:
    member_id = db.new_id("mem")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO members (id, name, budget_usd, dietary, created_at) VALUES (?, ?, ?, ?, ?)",
                     (member_id, name, budget, dietary, db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    return member_id


def client_for(member_id: str | None) -> TestClient:
    c = TestClient(app)
    if member_id:
        login_as(c, member_id)
    return c


def events(kind: str) -> list[dict]:
    if not eventlog.EVENTS_PATH.exists():
        return []
    lines = eventlog.EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == kind]


def fake_get(status=200, body=None, exc=None):
    def _get(url, **kwargs):
        _get.calls.append((url, kwargs))
        if exc is not None:
            raise exc
        req = httpx.Request("GET", url)
        return httpx.Response(status, json=body if body is not None else {"signed_url": SIGNED}, request=req)
    _get.calls = []
    return _get


# --- /api/voice/session ---


def test_session_success(keys, monkeypatch):
    get = fake_get()
    monkeypatch.setattr(voice.httpx, "get", get)
    r = client_for(add_member()).get("/api/voice/session")
    assert r.status_code == 200, r.text
    assert r.json() == {"signed_url": SIGNED, "member_first_name": "Maya", "budget_usd": 15.0, "dietary": "vegan"}
    url, kwargs = get.calls[0]
    assert url == "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
    assert kwargs["params"] == {"agent_id": AGENT}
    assert kwargs["headers"] == {"xi-api-key": KEY}
    assert KEY not in r.text
    logged = events("voice_session")
    assert len(logged) == 1 and SIGNED not in json.dumps(logged)  # the signed URL is a bearer token


def test_session_no_dietary_is_empty_string(keys, monkeypatch):
    monkeypatch.setattr(voice.httpx, "get", fake_get())
    r = client_for(add_member(dietary=None)).get("/api/voice/session")
    assert r.json()["dietary"] == ""


@pytest.mark.parametrize("missing", [{"elevenlabs_api_key": ""}, {"elevenlabs_agent_id": ""},
                                     {"elevenlabs_api_key": "", "elevenlabs_agent_id": ""}])
def test_session_missing_keys_503(keys, monkeypatch, missing):
    set_env(monkeypatch, **missing)
    get = fake_get()
    monkeypatch.setattr(voice.httpx, "get", get)
    r = client_for(add_member()).get("/api/voice/session")
    assert r.status_code == 503
    assert r.json() == {"error": "voice_unavailable", "message": voice.UNAVAILABLE_MESSAGE}
    assert get.calls == []
    assert events("voice_session_error")[-1]["reason"] == "not_configured"


@pytest.mark.parametrize("stub,reason", [
    (fake_get(status=401, body={"detail": "invalid key"}), "http_401"),
    (fake_get(status=500, body={"detail": "boom"}), "http_500"),
    (fake_get(exc=httpx.ConnectTimeout("slow")), "ConnectTimeout"),
    (fake_get(body={"unexpected": True}), "KeyError"),
])
def test_session_upstream_error_503(keys, monkeypatch, stub, reason):
    monkeypatch.setattr(voice.httpx, "get", stub)
    r = client_for(add_member()).get("/api/voice/session")
    assert r.status_code == 503 and r.json()["error"] == "voice_unavailable"
    assert KEY not in r.text
    assert events("voice_session_error")[-1]["reason"] == reason


def test_session_not_logged_in(keys, monkeypatch):
    get = fake_get()
    monkeypatch.setattr(voice.httpx, "get", get)
    r = client_for(None).get("/api/voice/session")
    assert r.status_code == 401 and r.json()["error"] == "not_logged_in"
    assert get.calls == []


def test_session_flag_off(keys, monkeypatch):
    set_features(monkeypatch, voice=False)
    get = fake_get()
    monkeypatch.setattr(voice.httpx, "get", get)
    c = client_for(add_member())
    assert c.get("/api/voice/session").status_code == 404
    assert c.get("/api/voice/catalog").status_code == 404
    assert get.calls == []
    assert c.get("/api/config/public").json()["features"]["voice"] is False
    # the text flow keeps working with voice off
    assert c.post("/api/intent", json={"text": "study session fuel"}).status_code == 200


# --- get_catalog tool ---


def test_voice_catalog_has_bays_and_tags():
    c = client_for(add_member())
    products = c.get("/api/voice/catalog").json()["products"]
    by_sku = {p["sku"]: p for p in products}
    assert set(by_sku) == set(voice.settings.skus)
    for b in voice.settings.bays:
        assert b.id + 1 in by_sku[b.sku]["bays"]  # printed card numbers, 1-based
    elx = by_sku["elx"]
    assert elx == {"sku": "elx", "name": "Hydration drink", "price_usd": 3.5, "tags": ["drink", "hydration"],
                   "pairs_with": ["wat"], "bays": [1]}
    rec = by_sku["rec"]  # the voice agent reads the caffeine tag to say so when it recommends it
    assert rec["pairs_with"] == ["bar"] and rec["tags"] == ["drink", "caffeine"] and rec["bays"] == [2]
    assert client_for(None).get("/api/voice/catalog").status_code == 401


# --- the existing endpoints the client tools wrap are unchanged ---


def test_catalog_endpoint_unchanged():
    body = client_for(None).get("/api/catalog").json()
    assert list(body) == ["skus", "bays"]  # bays: added for the shelf map, skus as before
    assert all(set(s) == {"sku", "name", "price_usd"} for s in body["skus"])


def test_make_plan_and_clear_plan_endpoints_unchanged():
    c = client_for(add_member())
    plan = c.post("/api/intent", json={"text": "rehydrate after a run under $15"}).json()
    assert set(plan) == {"plan_id", "goal_summary", "items", "est_total_usd", "budget_usd", "fits_budget", "bays",
                         "source", "created_at"}
    assert all(set(i) == {"sku", "name", "qty", "unit_price_usd", "reason"} for i in plan["items"])
    assert plan["items"] and plan["bays"] and plan["budget_usd"] == 15.0
    assert c.get("/api/intent/current").json() == plan
    assert c.delete("/api/intent").json() == {"ok": True}
    assert c.get("/api/intent/current").json() is None


def test_get_cart_endpoint_unchanged():
    member_id = add_member()
    c = client_for(member_id)
    assert c.get("/api/store/current").json() == {"session": None}
    shelf_state.apply_snapshot(snap(FULL))
    assert c.post("/api/dev/start").status_code == 200
    body = c.get("/api/store/current").json()
    assert set(body) == {"session", "cart"}
    assert {"items", "total_usd", "budget_usd", "over_budget"} <= set(body["cart"])
