"""Kiosk agent (docs/voice.md, "Kiosk"): the token gate, the public page's privacy, TTS caching and fallback,
visit events for the step tracker, and shelf activity. ElevenLabs is mocked everywhere: no test reaches it."""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import cart as cart_mod
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
    ("GET", "/api/kiosk/shopper"),
    ("GET", "/api/kiosk/voice-session"),
    ("POST", "/api/kiosk/conversation"),
    ("GET", "/api/kiosk/catalog"),
    ("GET", "/api/kiosk/cart"),
    ("POST", "/api/kiosk/plan"),
    ("DELETE", "/api/kiosk/plan"),
]


@pytest.fixture(autouse=True)
def kiosk_setup(monkeypatch):
    # yolo off: tag snapshots drive the cart whatever vision.mode the local config.json has
    set_features(monkeypatch, voice=True, llm=False, gates=False, hardware_leds=False, passkeys=False, yolo=False)
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
    kiosk_agent.narrator.run_due(time.monotonic())  # what the narrator thread does at once: the entry baseline
    picked = with_bays(b2=[u for u in FULL[2]][1:])
    assert client.post("/internal/shelf", json=snap(picked, frame_id=5), headers=HEADERS).status_code == 200
    kiosk_agent.narrator.run_due(time.monotonic() + 5)  # flush the panel update and the debounced line
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
    panel = [m["data"] for m in got if m["type"] == "kiosk_cart"][-1]
    assert panel == {"first_name": "Maya", "budget_usd": 10.0, "remaining_usd": 7.3, "cart_total_usd": 2.7,
                     "item_count": 1, "visit_count": 1}
    said = [m["data"] for m in got if m["type"] == "kiosk_say"]
    assert [d["text"] for d in said] == ["See, the Chips are already in your cart. You're at $2.70."]
    text = json.dumps(got)
    for leak in ("4242", "Lopez", "balance", "card_label", maya):
        assert leak not in text, leak


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


def test_a_reconnecting_kiosk_gets_the_panel_too():
    maya = add_member()
    with TestClient(app) as client:
        login_as(client, maya)
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
        assert client.post("/api/dev/start").status_code == 200
        client.cookies.clear()
        with client.websocket_connect(f"/ws?role=kiosk&k={KIOSK_TOKEN}") as sock:
            first = [sock.receive_json() for _ in range(3)]
    assert first[2] == {"type": "kiosk_cart", "data": {"first_name": "Maya", "budget_usd": 10.0,
                                                       "remaining_usd": 10.0, "cart_total_usd": 0.0,
                                                       "item_count": 0, "visit_count": 1}}


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


# --- Phase 2: the shopper endpoint, greetings, narration ---

def enter_as(client: TestClient, member_id: str) -> dict:
    login_as(client, member_id)
    assert client.post("/internal/shelf", json=snap(FULL), headers=HEADERS).status_code == 200
    r = client.post("/api/dev/start")
    assert r.status_code == 200, r.text
    return r.json()


def finish_visit(member_id: str, state: str = "CLOSED") -> None:
    """A past visit on record (the same row shape store.py writes)."""
    from backend import db
    conn = db.connect()
    try:
        conn.execute("INSERT INTO store_sessions (id, member_id, state, baseline_json, started_at, ended_at) "
                     "VALUES (?, ?, ?, '{}', ?, ?)", (db.new_id("ses"), member_id, state, db.now_iso(), db.now_iso()))
        conn.commit()
    finally:
        conn.close()


SHOPPER_FIELDS = {"first_name", "budget_usd", "remaining_usd", "cart_total_usd", "item_count", "visit_count"}


def test_shopper_endpoint_is_null_while_the_store_is_free():
    assert TestClient(app).get(k("/api/kiosk/shopper")).json() == {"shopper": None, "greeting": None}


def test_shopper_endpoint_fields_and_nothing_else():
    maya = add_member(budget=10.0)
    c = TestClient(app)
    enter_as(c, maya)
    picked = with_bays(b2=list(FULL[2])[1:])  # one Chips: $2.50 + 8% = $2.70
    c.post("/internal/shelf", json=snap(picked, frame_id=5), headers=HEADERS)
    c.cookies.clear()
    body = c.get(k("/api/kiosk/shopper")).json()
    assert set(body) == {"shopper", "greeting"}
    assert set(body["shopper"]) == SHOPPER_FIELDS
    assert body["shopper"] == {"first_name": "Maya", "budget_usd": 10.0, "remaining_usd": 7.3,
                               "cart_total_usd": 2.7, "item_count": 1, "visit_count": 1}
    text = json.dumps(body)
    for leak in ("Lopez", "4242", "Visa", "balance", "card", "mem_", "ses_", "Chips"):
        assert leak not in text, leak


def test_shopper_endpoint_ignores_a_return_in_progress(monkeypatch):
    maya = add_member()
    c = TestClient(app)
    enter_as(c, maya)
    session = store.current_session()
    store.cancel(session["id"], reason="test")
    monkeypatch.setattr(store, "current_session", lambda: {**session, "state": "RETURNING", "return_of": "ses_x"})
    assert c.get(k("/api/kiosk/shopper")).json() == {"shopper": None, "greeting": None}


def test_first_visit_greeting():
    maya = add_member(budget=10.0)
    c = TestClient(app)
    enter_as(c, maya)
    g = c.get(k("/api/kiosk/shopper")).json()["greeting"]
    assert g["kind"] == "greeting_first"
    assert g["text"] == ("Welcome to your first visit, Maya. Just grab what you want; the camera adds it to your "
                         "cart. When you're done, tap Checkout on your phone.")
    assert tts.is_fixed(g["text"])  # no numbers: cached as a file


def test_returning_greeting(monkeypatch):
    set_features(monkeypatch, gates=True)
    maya = add_member(budget=10.0)
    finish_visit(maya)
    c = TestClient(app)
    login_as(c, maya)
    c.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
    store.start_session(maya)
    body = c.get(k("/api/kiosk/shopper")).json()
    assert body["shopper"]["visit_count"] == 2
    assert body["greeting"]["kind"] == "greeting_returning"
    assert body["greeting"]["text"] == "Welcome back, Maya. You have $10 to spend."


def test_cancelled_and_return_visits_do_not_make_a_first_timer_returning():
    maya = add_member()
    finish_visit(maya, state="CANCELLED")  # an admin reset their first try
    from backend import db
    conn = db.connect()
    try:
        conn.execute("INSERT INTO store_sessions (id, member_id, state, baseline_json, started_at, return_of) "
                     "VALUES ('ses_ret', ?, 'CLOSED', '{}', ?, 'ses_x')", (maya, db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    c = TestClient(app)
    enter_as(c, maya)
    assert c.get(k("/api/kiosk/shopper")).json()["greeting"]["kind"] == "greeting_first"


# --- narration: debounce and line selection (Narrator with its own clock, no thread, no vision) ---

SESSION = {"id": "ses_narr", "state": "IN_STORE", "member_id": "mem_narr"}


def cart_of(budget: float = 10.0, misplaced=None, state: str = "IN_STORE", **qty: int) -> dict:
    """A CartSnapshot priced by the real cart code (8.5), so the totals are the backend's own."""
    return cart_mod.priced_cart({**SESSION, "state": state}, qty, {"budget_usd": budget}, misplaced=misplaced)


class Harness:
    """A Narrator driven by hand: change() moves the cart and the clock, run(t) does what is due at t."""

    def __init__(self, budget: float = 10.0):
        self.now = 0.0
        self.budget = budget
        self.session = dict(SESSION)
        self.cart = cart_of(budget)
        self.member = {"id": "mem_narr", "name": "Maya Lopez", "budget_usd": budget}
        self.sent: list[dict] = []
        self.n = kiosk_agent.Narrator(threaded=False, clock=lambda: self.now,
                                      current=lambda: (self.session, self.cart, self.member), emit=self.sent.append)
        self.n.visit_started(SESSION["id"])
        self.run(0.0)  # baseline: the cart at entry

    def change(self, at: float, misplaced=None, **qty: int) -> None:
        self.now = at
        self.cart = cart_of(self.budget, misplaced=misplaced, state=self.session["state"], **qty)
        self.n.cart_changed(SESSION["id"], self.session["state"])

    def run(self, at: float) -> None:
        self.now = at
        self.n.run_due(at)

    def said(self) -> list[str]:
        return [m["data"]["text"] for m in self.sent if m["type"] == "kiosk_say"]

    def panels(self) -> list[dict]:
        return [m["data"] for m in self.sent if m["type"] == "kiosk_cart"]


def test_pick_line_uses_the_carts_own_total():
    h = Harness()
    h.change(1.0, bar=1)
    h.run(2.49)
    assert h.said() == []
    h.run(2.5)
    assert h.said() == ["See, the Chips are already in your cart. You're at $2.70."]


def test_single_item_that_is_not_plural():
    h = Harness()
    h.change(1.0, wat=1)
    h.run(3.0)
    assert h.said() == ["See, the Water is already in your cart. You're at $1.62."]


def test_debounce_waits_for_the_cart_to_be_still_and_says_one_line_for_the_net_change():
    h = Harness()
    h.change(1.0, bar=1)
    h.change(2.0, bar=1, wat=1)
    h.change(3.0, bar=1, wat=1, elx=1)
    h.run(4.49)
    assert h.said() == []  # still inside 1.5 s of the last change
    h.run(4.5)
    assert h.said() == ["See, the Hydration drink, Chips and Water are already in your cart. You're at $8.10."]


def test_pick_and_put_back_inside_the_window_says_nothing():
    h = Harness()
    h.change(1.0, bar=1)
    h.change(1.8)  # back on the shelf 0.8 s later
    h.run(10.0)
    assert h.said() == []


def test_put_back_line():
    h = Harness()
    h.change(1.0, wat=1, bar=1)
    h.run(3.0)
    h.change(5.0, bar=1)
    h.run(7.0)
    assert h.said()[-1] == "Water is back on the shelf, removed from your cart."


def test_over_budget_line_follows_the_policy():
    h = Harness(budget=10.0)
    h.change(1.0, elx=1, rec=1)  # $7.02
    h.run(3.0)
    h.change(5.0, elx=1, rec=1, mix=1)  # $11.34 on a $10 budget
    h.run(7.0)
    assert h.said()[-1] == "That's $1.34 over your budget. Putting back the Vegan snack fixes it."
    h.change(9.0, elx=1, rec=1)
    h.run(11.0)
    assert h.said()[-1] == ("Vegan snack is back on the shelf, removed from your cart. "
                            "You're back under your budget.")


def test_still_over_budget_by_a_new_amount_is_said_again():
    h = Harness(budget=10.0)
    h.change(1.0, elx=1, rec=1, mix=1, bar=1)  # $14.04: $4.04 over
    h.run(3.0)
    assert h.said() == ["That's $4.04 over your budget. Putting back the Vegan snack fixes it."]
    h.change(5.0, elx=1, rec=1, mix=1)  # still over, now $1.34
    h.run(7.0)
    assert h.said()[-1] == "That's $1.34 over your budget. Putting back the Vegan snack fixes it."


def test_misplaced_line_names_the_bay_to_return_it_to():
    h = Harness()
    h.change(1.0, bar=1)
    h.run(3.0)
    wrong = [{"tag_id": 6, "sku": "wat", "name": "Water", "bay": 0, "home_bay": 3, "count": 1}]
    h.change(5.0, misplaced=wrong, bar=1)
    h.run(7.0)
    card = next(b.id for b in kiosk_agent.settings.bays if b.sku == "wat") + 1
    assert h.said()[-1] == f"Oops, the Water is in the wrong bay. Please put it back in bay {card}."
    h.change(9.0, misplaced=wrong, bar=1, wat=0)
    h.run(11.0)
    assert len(h.said()) == 2  # the same misplaced item is said once


def test_exit_reminder_after_60_s_idle_with_items_once():
    h = Harness()
    h.change(1.0, bar=1)
    h.run(2.5)  # the pick line; the reminder is due 60 s after it
    h.run(62.49)
    assert h.said() == ["See, the Chips are already in your cart. You're at $2.70."]
    h.run(62.5)
    assert h.said()[-1] == "When you're ready, tap Checkout on your phone to review and pay."
    h.run(500.0)
    assert len(h.said()) == 2  # once per quiet spell
    reminder = [m["data"] for m in h.sent if m["type"] == "kiosk_say"][-1]
    assert reminder["kind"] == "exit_reminder" and tts.is_fixed(reminder["text"])


def test_no_exit_reminder_with_an_empty_cart():
    h = Harness()
    h.run(500.0)
    h.change(501.0, bar=1)
    h.change(501.5)
    h.run(1000.0)
    assert h.said() == []


def test_nothing_is_said_after_the_exit_scan_or_the_end_of_the_visit():
    h = Harness()
    h.change(1.0, bar=1)
    h.session["state"] = "CHECKOUT_PENDING"
    h.n.visit_paused(SESSION["id"])
    h.run(100.0)
    assert h.said() == []
    h.session["state"] = "IN_STORE"
    h.n.visit_resumed(SESSION["id"])
    h.change(101.0, bar=1, wat=1)
    h.run(103.0)
    assert h.said() == ["See, the Chips and Water are already in your cart. You're at $4.32."]
    h.n.visit_ended()
    h.change(104.0, bar=2)
    h.run(500.0)
    assert len(h.said()) == 1


def test_panel_updates_at_once_with_only_the_allowed_fields():
    h = Harness()
    h.change(1.0, bar=1)
    h.run(1.0)  # no debounce for the panel
    assert h.panels()[-1] == {"first_name": "Maya", "budget_usd": 10.0, "remaining_usd": 7.3,
                              "cart_total_usd": 2.7, "item_count": 1, "visit_count": 0}
    assert h.said() == []


def test_every_amount_said_comes_from_the_cart_or_the_policy():
    h = Harness(budget=10.0)
    steps = [dict(bar=1), dict(bar=1, rec=1), dict(bar=1, rec=1, mix=1, elx=1), dict(bar=1, rec=1), dict()]
    allowed = set()
    for i, qty in enumerate(steps):
        h.change(10.0 * i + 1, **qty)
        c = h.cart
        allowed |= {cart_mod.to_cents(c["total_usd"])}
        decision = kiosk_agent.agent.policy(c, h.member)
        if decision["kind"] == "over_budget":
            allowed.add(cart_mod.to_cents(decision["over_by"]))
        h.run(10.0 * i + 5)
    said = " ".join(h.said())
    for amount in re.findall(r"\$(\d+(?:\.\d{2})?)", said):
        assert cart_mod.to_cents(amount) in allowed, amount


def test_a_visit_already_under_way_is_joined_without_narrating_its_past():
    """After a backend restart the narrator has no visit; the first cart change of an IN_STORE session adopts it."""
    sent = []
    cart = {"now": cart_of(bar=2)}
    n = kiosk_agent.Narrator(threaded=False, clock=lambda: 0.0,
                             current=lambda: (SESSION, cart["now"], {"id": "m", "name": "Maya", "budget_usd": 10}),
                             emit=sent.append)
    n.cart_changed("ses_narr", "CHECKOUT_PENDING")
    assert n.run_due(10.0) is None  # not a shopping change: ignored
    n.cart_changed("ses_narr", "IN_STORE")
    n.run_due(10.0)
    assert [m for m in sent if m["type"] == "kiosk_say"] == []  # the baseline is today's cart
    cart["now"] = cart_of(bar=2, wat=1)
    n.cart_changed("ses_narr", "IN_STORE")
    n.run_due(20.0)
    assert [m["data"]["text"] for m in sent if m["type"] == "kiosk_say"] == \
        ["See, the Water is already in your cart. You're at $7.02."]


# --- Phase 3: the kiosk conversation (ElevenLabs agent, mode "kiosk") ---

AGENT_ID = "agent_kiosk_test"
SIGNED = "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=agent_kiosk_test&conversation_signature=sig"


@pytest.fixture
def agent_on(monkeypatch):
    set_env(monkeypatch, elevenlabs_api_key=XI_KEY, elevenlabs_agent_id=AGENT_ID)
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"signed_url": SIGNED}, request=httpx.Request("GET", url))
    from backend import voice
    monkeypatch.setattr(voice.httpx, "get", fake_get)
    return calls


def test_voice_session_needs_a_shopper(agent_on):
    r = TestClient(app).get(k("/api/kiosk/voice-session"))
    assert r.status_code == 409 and r.json()["error"] == "no_shopper"
    assert agent_on == []  # no signed URL is fetched for nobody


def test_voice_session_dynamic_variables(agent_on):
    maya = add_member(budget=10.0)
    from backend import db
    conn = db.connect()
    conn.execute("UPDATE members SET dietary = 'vegan' WHERE id = ?", (maya,))
    conn.commit()
    conn.close()
    c = TestClient(app)
    enter_as(c, maya)
    c.cookies.clear()
    r = c.get(k("/api/kiosk/voice-session"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "signed_url": SIGNED,
        "dynamic_variables": {"first_name": "Maya", "budget_usd": 10.0, "remaining_usd": 10.0, "dietary": "vegan",
                              "is_first_visit": True, "mode": "kiosk"},
        "max_duration_s": 300.0,
        "silence_timeout_s": 120.0,
    }
    url, kwargs = agent_on[0]
    assert url == "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
    assert kwargs["params"] == {"agent_id": AGENT_ID} and kwargs["headers"] == {"xi-api-key": XI_KEY}
    assert XI_KEY not in r.text
    assert events("kiosk_voice_session") and SIGNED not in json.dumps(events("kiosk_voice_session"))


def test_voice_session_returning_shopper_and_no_dietary(agent_on):
    maya = add_member(budget=10.0)
    finish_visit(maya)
    c = TestClient(app)
    enter_as(c, maya)
    variables = c.get(k("/api/kiosk/voice-session")).json()["dynamic_variables"]
    assert variables["is_first_visit"] is False and variables["dietary"] == "none"


def test_voice_session_off_or_unconfigured(agent_on, monkeypatch):
    maya = add_member()
    c = TestClient(app)
    enter_as(c, maya)
    set_env(monkeypatch, elevenlabs_agent_id="")
    r = c.get(k("/api/kiosk/voice-session"))
    assert r.status_code == 503 and r.json()["error"] == "voice_unavailable"
    assert events("kiosk_voice_session_error")[-1]["reason"] == "not_configured"
    set_features(monkeypatch, voice=False)
    assert c.get(k("/api/kiosk/voice-session")).status_code == 404
    assert c.get(k("/api/kiosk/phrases")).json()["conversation"] is False


def test_phrases_say_whether_a_conversation_is_possible(agent_on):
    assert TestClient(app).get(k("/api/kiosk/phrases")).json()["conversation"] is True


def test_voice_session_upstream_failure_is_503(agent_on, monkeypatch):
    from backend import voice
    maya = add_member()
    c = TestClient(app)
    enter_as(c, maya)

    def boom(url, **kwargs):
        raise httpx.ConnectTimeout("slow")
    monkeypatch.setattr(voice.httpx, "get", boom)
    r = c.get(k("/api/kiosk/voice-session"))
    assert r.status_code == 503 and XI_KEY not in r.text
    assert events("kiosk_voice_session_error")[-1]["reason"] == "ConnectTimeout"


# client tools: they act on the CURRENT shopper through kiosk token routes

def test_catalog_tool_is_the_phone_agents_catalog():
    from backend import voice
    assert TestClient(app).get(k("/api/kiosk/catalog")).json() == {"products": voice.catalog_products()}


def test_cart_tool_reads_the_current_shoppers_cart():
    c = TestClient(app)
    assert c.get(k("/api/kiosk/cart")).json()["in_store"] is False
    maya = add_member(budget=10.0)
    enter_as(c, maya)
    c.post("/internal/shelf", json=snap(with_bays(b2=list(FULL[2])[1:]), frame_id=5), headers=HEADERS)
    c.cookies.clear()
    body = c.get(k("/api/kiosk/cart")).json()
    assert body["in_store"] is True
    assert body["items"] == [{"name": "Chips", "qty": 1, "line_total_usd": 2.5}]
    assert (body["total_usd"], body["budget_usd"], body["remaining_usd"], body["over_budget"]) == (2.7, 10.0, 7.3, False)
    assert "not the shopper's card" in body["payment"]


def test_plan_tools_plan_for_the_current_shopper_and_light_the_shelf():
    from backend import intent
    c = TestClient(app)
    r = c.post(k("/api/kiosk/plan"), json={"goal_text": "thirsty after a run"})
    assert r.status_code == 409 and r.json()["error"] == "no_shopper"
    maya = add_member(budget=10.0)
    enter_as(c, maya)
    c.cookies.clear()  # the kiosk is nobody: the plan is the shopper's
    plan = c.post(k("/api/kiosk/plan"), json={"goal_text": "thirsty after a run"}).json()
    assert {i["sku"] for i in plan["items"]} == {"elx", "wat"}
    assert intent.current_plan(maya) == plan
    assert intent.shown_bays() == sorted(plan["bays"]) and plan["bays"]
    assert events("intent_plan")[-1]["via"] == "kiosk"
    r = c.post(k("/api/kiosk/plan"), json={"goal_text": "   "})
    assert r.status_code == 400 and r.json()["error"] == "empty_text"
    assert c.delete(k("/api/kiosk/plan")).json() == {"ok": True}
    assert intent.current_plan(maya) is None and intent.shown_bays() == []


def test_tools_need_voice(monkeypatch):
    set_features(monkeypatch, voice=False)
    c = TestClient(app)
    for method, path in (("GET", "/api/kiosk/catalog"), ("GET", "/api/kiosk/cart"), ("DELETE", "/api/kiosk/plan")):
        assert c.request(method, k(path)).status_code == 404


# one conversation at a time: the phone waits while the kiosk talks

def test_phone_voice_is_disabled_while_a_kiosk_conversation_is_active(agent_on):
    from backend import voice
    maya = add_member()
    phone = TestClient(app)
    enter_as(phone, maya)
    assert phone.get("/api/voice/session").status_code == 200
    kiosk_client = TestClient(app)
    assert kiosk_client.post(k("/api/kiosk/conversation"), json={"active": True}).json() == {"ok": True,
                                                                                             "active": True}
    r = phone.get("/api/voice/session")
    assert r.status_code == 409
    assert r.json() == {"error": "kiosk_conversation_active", "message": voice.KIOSK_ACTIVE_MESSAGE}
    assert kiosk_client.post(k("/api/kiosk/conversation"), json={"active": False, "reason": "silence"}).status_code \
        == 200
    assert phone.get("/api/voice/session").status_code == 200
    ended = [e for e in events("kiosk_conversation") if e["active"] is False]
    assert ended[-1]["reason"] == "silence"


def test_kiosk_conversation_needs_a_shopper():
    r = TestClient(app).post(k("/api/kiosk/conversation"), json={"active": True})
    assert r.status_code == 409 and r.json()["error"] == "no_shopper"


def test_only_the_shopper_in_the_store_is_blocked():
    maya, sam = add_member(), add_member("Sam")
    enter_as(TestClient(app), maya)
    TestClient(app).post(k("/api/kiosk/conversation"), json={"active": True})
    assert kiosk_agent.conversation_active_for(maya) is True
    assert kiosk_agent.conversation_active_for(sam) is False


def test_a_forgotten_conversation_expires_after_the_cap(monkeypatch):
    kiosk_agent.start_conversation("ses_x", "mem_x", now=1000.0)
    assert kiosk_agent.conversation_active_for("mem_x", now=1000.0 + 300)
    assert not kiosk_agent.conversation_active_for("mem_x", now=1000.0 + 300 + kiosk_agent.CONVERSATION_GRACE_S)


def test_the_visit_ending_or_the_exit_scan_frees_the_phone():
    maya = add_member()
    c = TestClient(app)
    enter_as(c, maya)
    TestClient(app).post(k("/api/kiosk/conversation"), json={"active": True})
    assert kiosk_agent.conversation_active_for(maya)
    c.post("/internal/shelf", json=snap(with_bays(b2=list(FULL[2])[1:]), frame_id=5), headers=HEADERS)
    assert c.post("/api/dev/checkout").status_code == 200  # the exit scan (gates off: the Checkout button)
    assert not kiosk_agent.conversation_active_for(maya)
    assert events("kiosk_conversation")[-1]["reason"] == "exit_pending"


def test_phone_socket_hears_when_the_kiosk_talks():
    maya = add_member()
    with TestClient(app) as client:
        enter_as(client, maya)
        with client.websocket_connect("/ws") as sock:  # the shopper's phone
            kiosk_client = TestClient(app)
            kiosk_client.post(k("/api/kiosk/conversation"), json={"active": True})
            on = read_until(sock, lambda m, out: m["type"] == "kiosk_voice")[-1]
            kiosk_client.post(k("/api/kiosk/conversation"), json={"active": False})
            off = read_until(sock, lambda m, out: m["type"] == "kiosk_voice")[-1]
        TestClient(app).post(k("/api/kiosk/conversation"), json={"active": True})
        with client.websocket_connect("/ws") as sock:  # a phone that connects mid-conversation
            first = read_until(sock, lambda m, out: m["type"] == "kiosk_voice")
    assert on == {"type": "kiosk_voice", "data": {"active": True}}
    assert off == {"type": "kiosk_voice", "data": {"active": False}}
    assert first[-1] == {"type": "kiosk_voice", "data": {"active": True}}


def test_phone_session_carries_the_same_variables_as_the_kiosk(agent_on):
    maya = add_member(budget=10.0)
    finish_visit(maya)
    c = TestClient(app)
    login_as(c, maya)
    body = c.get("/api/voice/session").json()  # not in the store yet
    assert (body["remaining_usd"], body["is_first_visit"], body["mode"]) == (10.0, False, "phone")
    fresh = add_member("Noor", budget=20.0)
    enter_as(c, fresh)
    c.post("/internal/shelf", json=snap(with_bays(b2=list(FULL[2])[1:]), frame_id=9), headers=HEADERS)
    body = c.get("/api/voice/session").json()  # inside, one Chips in the cart
    assert (body["remaining_usd"], body["is_first_visit"]) == (17.3, True)
