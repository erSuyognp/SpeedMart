"""Kiosk agent (docs/voice.md, "Kiosk"): the token gate, the public page's privacy, TTS caching and fallback,
visit events for the step tracker, and shelf activity. ElevenLabs is mocked everywhere: no test reaches it."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import kiosk, kiosk_agent, shelf_state, store, tts, ws
from backend.main import app
from identity_helpers import login_as, set_env, set_features
from test_cart import FULL, TOKEN, events, snap, tmp_data, with_bays  # noqa: F401  (tmp_data: temp DB per test)

pytestmark = pytest.mark.usefixtures("tmp_data")

KIOSK_TOKEN = "kiosk-test-token-0123456789"
VOICE_ID = "voice_test_123"
XI_KEY = "xi_test_key_never_in_browser"
HEADERS = {"X-Internal-Token": TOKEN}

# Every kiosk-only route (method, path). Each must refuse a missing, wrong or unconfigured token.
KIOSK_ONLY = [
    ("GET", "/api/kiosk/phrases"),
    ("GET", "/api/kiosk/tts/" + "0" * 32),
]


@pytest.fixture(autouse=True)
def kiosk_setup(monkeypatch):
    set_features(monkeypatch, voice=True, llm=False, gates=False, hardware_leds=False, passkeys=False)
    set_env(monkeypatch, kiosk_token=KIOSK_TOKEN, elevenlabs_api_key="", elevenlabs_voice_id="")
    tts.reset()
    kiosk_agent.reset()
    yield
    tts.reset()
    kiosk_agent.reset()


@pytest.fixture
def speech_on(monkeypatch):
    set_env(monkeypatch, elevenlabs_api_key=XI_KEY, elevenlabs_voice_id=VOICE_ID)


def fake_post(audio=b"ID3fake-mp3-bytes", status=200, exc=None, content_type="audio/mpeg"):
    """Stands in for httpx.post in backend/tts.py and records every call."""
    def _post(url, **kwargs):
        _post.calls.append((url, kwargs))
        if exc is not None:
            raise exc
        req = httpx.Request("POST", url)
        return httpx.Response(status, content=audio, headers={"content-type": content_type}, request=req)
    _post.calls = []
    return _post


def add_member(name="Maya Lopez", budget=10.0) -> str:
    from backend import db
    member_id = db.new_id("mem")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO members (id, name, budget_usd, card_label, created_at) VALUES (?, ?, ?, ?, ?)",
                     (member_id, name, budget, "Demo card · Visa test •••• 4242", db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    return member_id


def k(path: str, token: str = KIOSK_TOKEN) -> str:
    return path + ("&" if "?" in path else "?") + "k=" + token


# --- the token gate ---

@pytest.mark.parametrize("method,path", KIOSK_ONLY)
def test_kiosk_routes_refuse_a_missing_or_wrong_token(method, path):
    c = TestClient(app)
    for url in (path, k(path, "wrong"), k(path, KIOSK_TOKEN[:-1]), k(path, "")):
        r = c.request(method, url)
        assert r.status_code == 403, (url, r.text)
        assert r.json()["error"] == "kiosk_only"
        assert KIOSK_TOKEN not in r.text


@pytest.mark.parametrize("method,path", KIOSK_ONLY)
def test_kiosk_routes_refuse_everything_while_no_token_is_configured(monkeypatch, method, path):
    set_env(monkeypatch, kiosk_token="")
    c = TestClient(app)
    for url in (path, k(path, ""), k(path, "anything")):
        assert c.request(method, url).status_code == 403


@pytest.mark.parametrize("method,path", KIOSK_ONLY)
def test_kiosk_routes_accept_the_token(method, path):
    r = TestClient(app).request(method, k(path))
    assert r.status_code != 403, r.text


def test_token_is_compared_in_constant_time(monkeypatch):
    calls = []
    real = kiosk.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)
    monkeypatch.setattr(kiosk.secrets, "compare_digest", spy)
    assert kiosk.token_ok(KIOSK_TOKEN) is True
    assert kiosk.token_ok("nope") is False
    assert len(calls) == 2
    assert kiosk.token_ok(None) is False and kiosk.token_ok("") is False


def test_bad_token_is_logged_without_the_token():
    TestClient(app).get(k("/api/kiosk/phrases", "guess-1234"))
    logged = events("kiosk_bad_token")
    assert logged and logged[-1]["path"] == "/api/kiosk/phrases"
    assert "guess-1234" not in json.dumps(logged)


def test_qr_codes_stay_public():
    assert TestClient(app).get("/api/kiosk/qr/join").status_code == 200


# --- phrases: the tour and the fixed lines ---

def test_phrases_tour_has_the_four_steps_in_order():
    body = TestClient(app).get(k("/api/kiosk/phrases")).json()
    assert [line["step"] for line in body["tour"]] == [0, 1, 2, 3, 4]
    assert body["speech"] is False and body["voice"] is True
    assert all(line["audio_url"] is None for line in body["tour"])  # no voice id: captions only
    words = sum(len(line["text"].split()) for line in body["tour"])
    assert 60 <= words <= 95  # about 30 s at a calm speaking pace
    assert "shelf camera builds your cart" in body["tour"][0]["text"]
    assert set(body["phrases"]) == {"offer", "exit_reminder"}
    for line in body["tour"] + list(body["phrases"].values()):
        assert tts.is_fixed(line["text"])  # no digits: generated once, cached as files


def test_tour_follows_the_feature_flags(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True)
    tour = [line["text"] for line in kiosk_agent.tour_lines()]
    assert "Scan the enter code" in tour[2] and "Face ID" in tour[2]
    assert "scan the exit code" in tour[4]
    assert kiosk_agent.phrases()["exit_reminder"] == "When you're ready, scan the exit code to review and pay."
    set_features(monkeypatch, gates=False, passkeys=False)
    tour = [line["text"] for line in kiosk_agent.tour_lines()]
    assert "Start shopping" in tour[2] and "Face ID" not in " ".join(tour)
    assert "tap Checkout on your phone" in tour[4]


def test_phrases_with_speech_on_carry_audio_urls_and_warm_the_cache(speech_on, monkeypatch):
    warmed = []
    monkeypatch.setattr(tts, "warm", lambda texts: warmed.extend(texts))
    body = TestClient(app).get(k("/api/kiosk/phrases")).json()
    assert body["speech"] is True
    for line in body["tour"]:
        assert line["audio_url"].startswith("/api/kiosk/tts/")
        assert KIOSK_TOKEN not in line["audio_url"]  # the kiosk adds its own token
    assert set(warmed) == {line["text"] for line in body["tour"]} | {p["text"] for p in body["phrases"].values()}


# --- TTS: cache hit, on-demand generation, timeout fallback ---

def test_fixed_phrase_is_generated_once_then_served_from_the_file_cache(speech_on, monkeypatch):
    post = fake_post()
    monkeypatch.setattr(tts.httpx, "post", post)
    text = kiosk_agent.phrases()["offer"]
    assert tts.speak(text) == b"ID3fake-mp3-bytes"
    assert len(post.calls) == 1
    url, kwargs = post.calls[0]
    assert url == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    assert kwargs["headers"]["xi-api-key"] == XI_KEY
    assert kwargs["json"] == {"text": text, "model_id": tts.MODEL_ID}
    assert kwargs["params"] == {"output_format": tts.OUTPUT_FORMAT}
    assert (tts.cache_dir() / f"{tts.clip_id(text)}.mp3").read_bytes() == b"ID3fake-mp3-bytes"

    tts.reset()  # a restarted backend: memory gone, files kept
    monkeypatch.setattr(tts.httpx, "post", fake_post(exc=AssertionError("cache hit must not call ElevenLabs")))
    assert tts.speak(text) == b"ID3fake-mp3-bytes"


def test_cache_hit_works_even_when_speech_is_switched_off(speech_on, monkeypatch):
    monkeypatch.setattr(tts.httpx, "post", fake_post())
    text = "Welcome to SpeedMart."
    tts.speak(text)
    monkeypatch.setattr(tts.httpx, "post", fake_post(exc=AssertionError("no call")))
    assert tts.cached(text) == b"ID3fake-mp3-bytes"


def test_dynamic_line_is_generated_on_demand_with_a_4_second_timeout_and_kept_in_memory_only(speech_on, monkeypatch):
    post = fake_post(audio=b"ID3dynamic")
    monkeypatch.setattr(tts.httpx, "post", post)
    text = "See, the Chips are already in your cart. You're at $2.70."
    assert not tts.is_fixed(text)
    assert tts.speak(text) == b"ID3dynamic"
    assert post.calls[0][1]["timeout"] == 4.0
    assert not (tts.cache_dir() / f"{tts.clip_id(text)}.mp3").exists()  # numbers change: never a file
    assert tts.speak(text) == b"ID3dynamic" and len(post.calls) == 1  # but a refetch does not pay twice


@pytest.mark.parametrize("stub,reason", [
    (fake_post(exc=httpx.ReadTimeout("slow")), "ReadTimeout"),
    (fake_post(exc=httpx.ConnectError("down")), "ConnectError"),
    (fake_post(status=401, audio=b'{"detail":"bad key"}', content_type="application/json"), "http_401"),
    (fake_post(content_type="application/json", audio=b"{}"), "ValueError"),
])
def test_tts_failure_falls_back_to_caption_only(speech_on, monkeypatch, stub, reason):
    monkeypatch.setattr(tts.httpx, "post", stub)
    assert tts.speak("That's $1.34 over your budget.") is None
    assert events("tts_error")[-1]["reason"] == reason
    assert XI_KEY not in json.dumps(events("tts_error"))


def test_no_voice_id_means_captions_only_and_no_call(monkeypatch):
    post = fake_post()
    monkeypatch.setattr(tts.httpx, "post", post)
    assert tts.available() is False
    assert tts.speak("Hello there.") is None
    assert post.calls == []


def test_voice_flag_off_means_captions_only(speech_on, monkeypatch):
    set_features(monkeypatch, voice=False)
    assert tts.available() is False
    assert kiosk_agent.say_item("offer", "Hello.")["audio_url"] is None


def test_tts_route_serves_registered_lines_only(speech_on, monkeypatch):
    monkeypatch.setattr(tts.httpx, "post", fake_post(audio=b"ID3route"))
    c = TestClient(app)
    item = kiosk_agent.say_item("pick", "See, the Water is already in your cart. You're at $1.62.")
    r = c.get(k(item["audio_url"]))
    assert r.status_code == 200 and r.content == b"ID3route"
    assert r.headers["content-type"] == "audio/mpeg" and r.headers["cache-control"] == "no-store"
    assert c.get(k("/api/kiosk/tts/" + "f" * 32)).status_code == 404  # never registered
    assert c.get(k("/api/kiosk/tts/not-a-clip-id")).status_code == 404


def test_tts_route_answers_503_when_elevenlabs_times_out(speech_on, monkeypatch):
    monkeypatch.setattr(tts.httpx, "post", fake_post(exc=httpx.ReadTimeout("slow")))
    item = kiosk_agent.say_item("pick", "You're at $2.70.")
    r = TestClient(app).get(k(item["audio_url"]))
    assert r.status_code == 503 and r.json()["error"] == "tts_unavailable"


def test_warm_generates_only_uncached_fixed_phrases(speech_on, monkeypatch):
    post = fake_post()
    monkeypatch.setattr(tts.httpx, "post", post)
    started = []

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            started.append(1)
            self.target()
    monkeypatch.setattr(tts.threading, "Thread", InlineThread)
    tts.speak("Already cached.")
    tts.warm(["Already cached.", "Needs audio.", "Costs $3.00."])
    assert [c[1]["json"]["text"] for c in post.calls] == ["Already cached.", "Needs audio."]
    assert post.calls[-1][1]["timeout"] == tts.WARM_TIMEOUT_S
    tts.warm(["Already cached.", "Needs audio."])
    assert len(started) == 1  # nothing left to warm: no thread


# --- visit events for the step tracker ---

@pytest.mark.parametrize("entry,expected", [
    ({"from": None, "to": "IN_STORE"}, ("entered", 3)),
    ({"from": "IN_STORE", "to": "CHECKOUT_PENDING"}, ("exit_pending", 4)),
    ({"from": "CHECKOUT_PENDING", "to": "IN_STORE"}, ("resumed", 3)),
    ({"from": "CHECKOUT_PENDING", "to": "PAID"}, ("paid", None)),
    ({"from": "PAID", "to": "CLOSED"}, ("ended", None)),
    ({"from": "IN_STORE", "to": "CLOSED"}, ("ended", None)),
    ({"from": "IN_STORE", "to": "CANCELLED"}, ("ended", None)),
    ({"from": None, "to": "RETURNING", "return_of": "ses_x"}, None),  # a return is not a shopping visit
    ({"from": "RETURNING", "to": "CLOSED"}, None),
    ({"from": "RETURNING", "to": "CANCELLED"}, None),
])
def test_visit_events(entry, expected):
    assert kiosk_agent.visit_event({"type": "session_state", "session_id": "ses_1", "member_id": "m", **entry}) \
        == expected


# --- shelf activity (public, no data) ---

def test_shelf_activity_is_throttled_and_only_while_the_store_is_free(monkeypatch):
    sent = []
    monkeypatch.setattr(ws, "broadcast_shelf_activity", lambda: sent.append(1))
    assert kiosk_agent.note_shelf_motion(True, occupied=True, now=100.0) is False
    assert kiosk_agent.note_shelf_motion(False, occupied=False, now=100.0) is False
    assert kiosk_agent.note_shelf_motion(True, occupied=False, now=100.0) is True
    assert kiosk_agent.note_shelf_motion(True, occupied=False, now=129.0) is False
    assert kiosk_agent.note_shelf_motion(True, occupied=False, now=130.0) is True
    assert len(sent) == 2


def test_internal_shelf_reports_motion_as_shelf_activity():
    c = TestClient(app)
    moving = snap(FULL, unstable=(1,))  # bay 1 has a hand in it: motion true
    assert c.post("/internal/shelf", json=moving, headers=HEADERS).status_code == 200
    assert len(events("shelf_activity")) == 1
    assert c.post("/internal/shelf", json=snap(FULL, unstable=(2,), frame_id=2), headers=HEADERS).status_code == 200
    assert len(events("shelf_activity")) == 1  # throttled


# --- the public page and sockets leak no shopper data ---

def read_until(sock, done, limit=40) -> list[dict]:
    out = []
    for _ in range(limit):
        msg = sock.receive_json()
        out.append(msg)
        if done(msg, out):
            return out
    raise AssertionError(f"no end marker in {limit} messages: {out}")


def occupied_false_again(msg, out):
    """The store went busy and then free again: the whole visit has been seen."""
    opened = any(m["type"] == "store_status" and m["data"]["occupied"] for m in out)
    return opened and msg["type"] == "store_status" and msg["data"]["occupied"] is False


def shop_a_visit(client: TestClient, member_id: str) -> None:
    """Enter as Maya, pick the chips (bay 2), then the visit is cancelled (store_status occupied false)."""
    login_as(client, member_id)
    assert client.post("/internal/shelf", json=snap(FULL), headers=HEADERS).status_code == 200
    assert client.post("/api/dev/start").status_code == 200
    picked = with_bays(b2=[u for u in FULL[2]][1:])
    assert client.post("/internal/shelf", json=snap(picked, frame_id=5), headers=HEADERS).status_code == 200
    store.cancel(reason="test")


@pytest.mark.parametrize("query", ["", "?role=kiosk", "?role=kiosk&k=wrong", "?k=" + KIOSK_TOKEN])
def test_public_socket_gets_no_shopper_data(query):
    maya = add_member()
    with TestClient(app) as client:
        client.cookies.clear()  # the kiosk tablet is not signed in
        with client.websocket_connect("/ws" + query) as sock:
            first = sock.receive_json()
            assert first["type"] == "store_status"
            shop_a_visit(client, maya)
            got = read_until(sock, occupied_false_again)
    assert {m["type"] for m in got} <= {"store_status", "plan_bays", "shelf_activity"}
    text = json.dumps(got)
    assert "Maya" not in text and maya not in text and "4242" not in text


def test_kiosk_socket_gets_visit_steps():
    maya = add_member()
    with TestClient(app) as client:
        client.cookies.clear()
        with client.websocket_connect(f"/ws?role=kiosk&k={KIOSK_TOKEN}") as sock:
            assert sock.receive_json()["type"] == "store_status"
            shop_a_visit(client, maya)
            got = read_until(sock, occupied_false_again)
    visits = [m["data"] for m in got if m["type"] == "kiosk_visit"]
    assert visits[0] == {"event": "entered", "step": 3}
    assert visits[-1] == {"event": "ended", "step": None}
    assert "4242" not in json.dumps(got)


def test_a_reconnecting_kiosk_gets_the_visit_in_progress():
    maya = add_member()
    with TestClient(app) as client:
        login_as(client, maya)
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
        assert client.post("/api/dev/start").status_code == 200
        client.cookies.clear()
        with client.websocket_connect(f"/ws?role=kiosk&k={KIOSK_TOKEN}") as sock:
            first = [sock.receive_json() for _ in range(2)]
        with client.websocket_connect("/ws?role=kiosk&k=wrong") as sock:
            public_first = sock.receive_json()
    assert first[0] == {"type": "store_status", "data": {"occupied": True}}
    assert first[1] == {"type": "kiosk_visit", "data": {"event": "sync", "step": 3}}
    assert public_first == {"type": "store_status", "data": {"occupied": True}}


def test_kiosk_page_itself_has_no_shopper_data():
    maya = add_member()
    c = TestClient(app)
    login_as(c, maya)
    c.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
    assert c.post("/api/dev/start").status_code == 200
    c.cookies.clear()
    for url in ("/kiosk.html", "/kiosk.html?k=" + KIOSK_TOKEN, "/js/pages/kiosk.js", "/js/kiosk_agent.js",
                "/api/config/public", "/api/catalog", "/api/store/current"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert "Maya" not in r.text and KIOSK_TOKEN not in r.text, url
    assert c.get("/api/store/current").json() == {"session": None}
    assert shelf_state.has_snapshot()
