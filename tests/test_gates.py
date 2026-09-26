"""S3.4: entry + exit gates, store lock, fresh verification at the gate, QR generation."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import db, serial_bridge, store
from backend.main import app
from identity_helpers import login_as, set_features
from test_cart import FULL, TOKEN, make_member, member_id, snap, tmp_data, with_bays  # noqa: F401

pytestmark = pytest.mark.usefixtures("tmp_data")

HEADERS = {"X-Internal-Token": TOKEN}
ENTRY = {"gate_token": "test-entry-token"}
EXIT = {"gate_token": "test-exit-token"}


@pytest.fixture(autouse=True)
def gates_on(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True)


@pytest.fixture
def leds(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(serial_bridge, "send_timed", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(serial_bridge, "send", lambda cmd: sent.append((cmd,)) or True)
    return sent


def verified(client: TestClient, mid: str, age_s: float = 1) -> None:
    """Signed in as mid with a passkey login age_s seconds ago (as /api/passkey/login/verify leaves it)."""
    login_as(client, mid, verified_at=time.time() - age_s, verified_member=mid, verified_purpose="enter")


def shelf(client: TestClient, bays=FULL, frame_id: int = 1) -> None:
    assert client.post("/internal/shelf", json=snap(bays, frame_id=frame_id), headers=HEADERS).status_code == 200


# --- entry ---

def test_gate_routes_off_when_gates_off(monkeypatch, member_id):
    set_features(monkeypatch, gates=False)
    with TestClient(app) as client:
        verified(client, member_id)
        for path, body in (("/api/gate/enter", ENTRY), ("/api/gate/exit/quote", EXIT)):
            r = client.post(path, json=body)
            assert r.status_code == 404 and r.json()["error"] == "gates_off"


@pytest.mark.parametrize("body", [{"gate_token": "wrong"}, {}, EXIT])
def test_enter_bad_token_is_403(member_id, body):
    with TestClient(app) as client:
        shelf(client)
        verified(client, member_id)
        r = client.post("/api/gate/enter", json=body)
        assert r.status_code == 403 and r.json()["error"] == "bad_gate_token"
        assert store.current_session() is None


def test_enter_needs_login_then_fresh_verification(member_id):
    with TestClient(app) as client:
        shelf(client)
        r = client.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 401 and r.json()["error"] == "not_logged_in"
        login_as(client, member_id)
        r = client.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 401 and r.json()["error"] == "not_verified"
        verified(client, member_id, age_s=91)
        assert client.post("/api/gate/enter", json=ENTRY).json()["error"] == "not_verified"
        assert store.current_session() is None


def test_enter_with_face_id_opens_gate(member_id, leds):
    with TestClient(app) as client:
        shelf(client)
        verified(client, member_id)
        r = client.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session"]["state"] == "IN_STORE" and body["session"]["member_id"] == member_id
        assert body["cart"]["items"] == [] and body["session"]["baseline"] == {"elx": 2, "rec": 2, "bar": 2}
        assert ("GATE,OPEN", "GATE,IDLE", 3) in leds

        # Scanning again while inside is harmless and needs no new Face ID.
        again = client.post("/api/gate/enter", json=ENTRY)
        assert again.status_code == 200 and again.json()["session"]["id"] == body["session"]["id"]

        # The verification was used up: after leaving, entering again needs a new Face ID.
        client.post("/admin/login", json={"password": "test-admin-password"})
        client.post("/admin/reset")
        r = client.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 401 and r.json()["error"] == "not_verified"


def test_second_phone_sees_occupied_and_keeps_its_verification(member_id):
    maya = make_member("Maya Lopez")
    with TestClient(app) as first, TestClient(app) as second:
        shelf(first)
        verified(first, maya)
        assert first.post("/api/gate/enter", json=ENTRY).status_code == 200

        verified(second, member_id)
        r = second.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 409
        assert r.json() == {"error": "store_occupied", "message": "Someone is already shopping. Please wait a moment.",
                            "occupant_first_name": "Maya"}

        # Admin reset frees the store; the second phone's Face ID (still < 90 s) was not used up by the 409.
        first.post("/admin/login", json={"password": "test-admin-password"})
        assert first.post("/admin/reset").status_code == 200
        r = second.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 200 and r.json()["session"]["member_id"] == member_id


def test_enter_refused_without_vision_keeps_verification(member_id):
    with TestClient(app) as client:
        verified(client, member_id)
        r = client.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 503 and r.json()["error"] == "vision_unavailable"
        shelf(client)
        assert client.post("/api/gate/enter", json=ENTRY).status_code == 200


def test_enter_with_passkeys_off_needs_only_login(monkeypatch, member_id):
    set_features(monkeypatch, passkeys=False)
    with TestClient(app) as client:
        shelf(client)
        login_as(client, member_id)
        assert client.post("/api/gate/enter", json=ENTRY).status_code == 200


# --- exit quote + cancel ---

def enter(client: TestClient, mid: str) -> str:
    shelf(client)
    verified(client, mid)
    r = client.post("/api/gate/enter", json=ENTRY)
    assert r.status_code == 200, r.text
    return r.json()["session"]["id"]


def test_quote_freezes_cart_and_cancel_unfreezes(member_id):
    with TestClient(app) as client:
        session_id = enter(client, member_id)
        shelf(client, with_bays(b0=[1]), frame_id=2)  # one electrolyte picked

        r = client.post("/api/gate/exit/quote", json={"gate_token": "nope"})
        assert r.status_code == 403
        r = client.post("/api/gate/exit/quote", json=EXIT)
        assert r.status_code == 200, r.text
        cart = r.json()["cart"]
        assert cart["state"] == "CHECKOUT_PENDING" and cart["session_id"] == session_id and cart["total_usd"] == 8.64

        shelf(client, FULL, frame_id=3)  # put back after the quote: the frozen cart does not change
        again = client.post("/api/gate/exit/quote", json=EXIT).json()["cart"]
        assert again["total_usd"] == 8.64 and again["state"] == "CHECKOUT_PENDING"

        assert client.post("/api/gate/exit/cancel").json() == {"ok": True}
        current = client.get("/api/store/current").json()
        assert current["session"]["state"] == "IN_STORE" and current["cart"]["items"] == []
        assert client.post("/api/gate/exit/cancel").json() == {"ok": True}  # already shopping: no-op


def test_quote_with_empty_cart_closes_session(member_id):
    with TestClient(app) as client:
        enter(client, member_id)
        r = client.post("/api/gate/exit/quote", json=EXIT)
        assert r.status_code == 200
        assert r.json()["cart"]["state"] == "CLOSED" and r.json()["instruction"] is None
        assert store.current_session() is None  # lock free, no payment row
        conn = db.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
        finally:
            conn.close()


def test_quote_and_cancel_need_own_session(member_id):
    other = make_member("Sam")
    with TestClient(app) as shopper, TestClient(app) as stranger:
        enter(shopper, member_id)
        login_as(stranger, other)
        r = stranger.post("/api/gate/exit/quote", json=EXIT)
        assert r.status_code == 409 and r.json()["error"] == "no_active_session"
        r = stranger.post("/api/gate/exit/cancel")
        assert r.status_code == 409 and r.json()["error"] == "no_active_session"
        assert stranger.post("/api/gate/exit/quote", json=EXIT).status_code == 409
        assert TestClient(app).post("/api/gate/exit/quote", json=EXIT).status_code == 401


def test_gate_tokens_are_not_public():
    with TestClient(app) as client:
        text = client.get("/api/config/public").text
        assert "test-entry-token" not in text and "test-exit-token" not in text


# --- QR codes (13.2) ---

def test_gen_qr_writes_pngs_and_pdf(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import gen_qr

    env = tmp_path / ".env"
    env.write_text("PUBLIC_ORIGIN=https://speedmart-demo.ngrok-free.app/\r\nENTRY_GATE_TOKEN=e 1\r\n"
                   "EXIT_GATE_TOKEN=x2\r\n", encoding="utf-8")
    out = tmp_path / "qr"
    assert gen_qr.main(["--env", str(env), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["1_join.png", "2_enter.png", "3_exit.png", "all.pdf"]
    assert gen_qr.codes("https://d", "e 1", "x2") == [
        ("1_join", "1 · JOIN", "https://d/"),
        ("2_enter", "2 · ENTER", "https://d/enter.html?g=e%201"),
        ("3_exit", "3 · EXIT", "https://d/exit.html?g=x2"),
    ]
    assert (out / "all.pdf").read_bytes().startswith(b"%PDF")
    assert gen_qr.main(["--env", str(tmp_path / "missing.env"), "--out", str(out)]) == 1
