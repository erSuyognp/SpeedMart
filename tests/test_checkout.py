"""S4.1: instruction (8.6), mock payment (9.9), approve, receipt (11.5), loyalty (9.10), LEDs, force-decline."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import admin, db, payments, serial_bridge, store
from backend.main import app
from identity_helpers import login_as, set_features
from test_cart import FULL, TOKEN, make_member, member_id, snap, tmp_data, with_bays  # noqa: F401

pytestmark = pytest.mark.usefixtures("tmp_data")

HEADERS = {"X-Internal-Token": TOKEN}
ENTRY = {"gate_token": "test-entry-token"}
EXIT = {"gate_token": "test-exit-token"}
ONE_ELX = with_bays(b0=[1])  # 1 x $8.00 -> $8.64 with tax


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True)
    monkeypatch.setattr(admin, "force_decline", False)


@pytest.fixture
def leds(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(serial_bridge, "send_timed", lambda *a: sent.append(a) or True)
    return sent


@pytest.fixture
def gate_events(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(store.ws, "broadcast_gate", lambda mid, ev: seen.append((mid, ev)))
    return seen


def face_id(client: TestClient, mid: str) -> None:
    login_as(client, mid, verified_at=time.time() - 1, verified_member=mid, verified_purpose="exit")


def shop(client: TestClient, mid: str, bays=ONE_ELX) -> dict:
    """Enter with Face ID, pick, scan the exit. Returns the quote."""
    client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
    face_id(client, mid)
    assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
    client.post("/internal/shelf", json=snap(bays, frame_id=2), headers=HEADERS)
    r = client.post("/api/gate/exit/quote", json=EXIT)
    assert r.status_code == 200, r.text
    return r.json()


def payment_rows() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY rowid")]
    finally:
        conn.close()


def points_of(mid: str) -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT points FROM members WHERE id = ?", (mid,)).fetchone()[0]
    finally:
        conn.close()


def test_quote_carries_instruction(member_id):
    with TestClient(app) as client:
        q = shop(client, member_id)
        instr, cart = q["instruction"], q["cart"]
        assert set(instr) == {"instruction_id", "label", "agent", "agent_token", "user_intent", "items",
                              "amount_usd", "cardholder_confirmation", "created_at"}
        assert instr["instruction_id"].startswith("instr_")
        assert instr["label"] == "SANDBOX: structure modeled on Visa Intelligent Commerce concepts. Not a Visa API call."
        assert instr["agent"] == {"id": "speedmart-shelf-agent-01", "name": "SpeedMart Store Agent"}
        token = instr["agent_token"]
        assert token["token_ref"] == f"tok_speedmart_{cart['session_id']}"
        scope = token["scope"]
        assert scope["merchant"] == "SpeedMart #01" and scope["max_amount_usd"] == 20.0
        assert scope["currency"] == "USD" and scope["single_use"] is True
        created = datetime.fromisoformat(instr["created_at"].replace("Z", "+00:00"))
        assert datetime.fromisoformat(scope["expires_at"].replace("Z", "+00:00")) - created == timedelta(minutes=15)
        assert instr["user_intent"] == "Pay $8.64 to SpeedMart #01 for 1 item"
        assert instr["items"] == [{"sku": "elx", "qty": 1, "unit_price_usd": 8.0}]
        assert instr["amount_usd"] == 8.64 == cart["total_usd"]
        assert instr["cardholder_confirmation"] == {"method": "passkey", "verified_at": None}
        # Scanning the exit again shows the same instruction.
        assert client.post("/api/gate/exit/quote", json=EXIT).json()["instruction"] == instr


def test_approve_needs_fresh_face_id(member_id):
    with TestClient(app) as client:
        shop(client, member_id)  # the entry used up the first Face ID
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 401 and r.json()["error"] == "not_verified"
        assert store.current_session()["state"] == "CHECKOUT_PENDING" and payment_rows() == []


def test_approve_charges_mock_and_pays(member_id, leds, gate_events):
    with TestClient(app) as client:
        q = shop(client, member_id)
        face_id(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 200, r.text
        p = r.json()["payment"]
        assert set(p) == {"payment_id", "instruction_id", "provider", "provider_ref", "amount_usd", "currency",
                          "status", "auth_code", "card_label", "points_earned", "created_at"}
        assert p["status"] == "AUTHORIZED" and p["provider"] == "mock" and p["provider_ref"] is None
        assert re.fullmatch(r"MOCK\d{4}", p["auth_code"])
        assert p["amount_usd"] == 8.64 and p["currency"] == "USD" and p["points_earned"] == 8
        assert p["instruction_id"] == q["instruction"]["instruction_id"]
        assert p["card_label"] == "Visa •••• 4242 (test)"

        session = store.get_session(q["cart"]["session_id"])
        assert session["state"] == "PAID" and store.current_session() is None  # lock free
        assert ("SHELF,GREEN", "SHELF,IDLE", 3) in leds
        assert (member_id, "paid") in gate_events
        assert points_of(member_id) == 8

        [row] = payment_rows()
        assert row["amount_cents"] == 864 and row["status"] == "AUTHORIZED" and row["provider"] == "mock"
        confirmation = json.loads(row["instruction_json"])["cardholder_confirmation"]
        assert confirmation["method"] == "passkey" and confirmation["verified_at"].endswith("Z")

        [logged] = [json.loads(line) for line in payments.payments_log_path().read_text(encoding="utf-8").splitlines()]
        assert logged["payment_id"] == p["payment_id"] and logged["status"] == "AUTHORIZED"
        assert logged["instruction"]["instruction_id"] == p["instruction_id"]

        face_id(client, member_id)  # a second approve cannot charge again
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 409 and len(payment_rows()) == 1


def test_over_scope_is_refused_without_using_face_id(member_id):
    with TestClient(app) as client:
        everything_taken = {0: [], 1: [], 2: []}  # $31.00 + tax, budget $20
        q = shop(client, member_id, everything_taken)
        assert q["cart"]["total_usd"] == 33.48 and q["instruction"]["agent_token"]["scope"]["max_amount_usd"] == 20
        face_id(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 409 and r.json() == {
            "error": "over_scope", "message": "This is over your $20 limit. Put something back to continue."}
        assert payment_rows() == []

        # Put something back: cancel, return items, re-quote, approve with the same Face ID.
        client.post("/api/gate/exit/cancel")
        client.post("/internal/shelf", json=snap(ONE_ELX, frame_id=5), headers=HEADERS)
        assert client.post("/api/gate/exit/quote", json=EXIT).json()["cart"]["total_usd"] == 8.64
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "AUTHORIZED"


def test_force_decline_then_retry(member_id, leds, gate_events, monkeypatch):
    with TestClient(app) as client:
        q = shop(client, member_id)
        monkeypatch.setattr(admin, "force_decline", True)
        face_id(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 200
        body = r.json()
        assert body["payment"]["status"] == "DECLINED" and body["payment"]["auth_code"] is None
        assert body["payment"]["points_earned"] == 0
        assert body["message"] == "Card declined (sandbox: forced by staff)"
        assert ("SHELF,RED", "SHELF,IDLE", 2) in leds and (member_id, "declined") in gate_events
        assert store.current_session()["state"] == "CHECKOUT_PENDING" and points_of(member_id) == 0

        # Retry needs a new Face ID; with force-decline off it goes through.
        assert client.post("/api/gate/exit/approve").json()["error"] == "not_verified"
        monkeypatch.setattr(admin, "force_decline", False)
        face_id(client, member_id)
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "AUTHORIZED"
        assert [r["status"] for r in payment_rows()] == ["DECLINED", "AUTHORIZED"]
        assert store.get_session(q["cart"]["session_id"])["state"] == "PAID"


def test_force_decline_via_admin_route(member_id):
    with TestClient(app) as client:
        shop(client, member_id)
        client.post("/admin/login", json={"password": "test-admin-password"})
        assert client.post("/admin/force-decline", json={"on": True}).json()["on"] is True
        face_id(client, member_id)
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "DECLINED"


def test_approve_before_quote_is_409(member_id):
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
        face_id(client, member_id)
        client.post("/api/gate/enter", json=ENTRY)
        face_id(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 409 and r.json()["error"] == "invalid_state"
        assert TestClient(app).post("/api/gate/exit/approve").status_code == 401


def test_receipt_shows_payment_points_and_closes(member_id):
    with TestClient(app) as client, TestClient(app) as stranger:
        q = shop(client, member_id)
        face_id(client, member_id)
        p = client.post("/api/gate/exit/approve").json()["payment"]
        sid = q["cart"]["session_id"]

        login_as(stranger, make_member("Sam"))
        assert stranger.get(f"/api/receipt/{sid}").status_code == 404
        assert client.get("/api/receipt/ses_nope").status_code == 404

        r = client.get(f"/api/receipt/{sid}").json()
        assert r["paid"] is True and r["payment"] == p
        assert r["items"] == q["cart"]["items"] and r["total_usd"] == 8.64 and r["tax_usd"] == 0.64
        assert r["points_earned"] == 8 and r["points_total"] == 8 and r["loyalty"] is True
        assert r["state"] == "CLOSED" and store.get_session(sid)["state"] == "CLOSED"  # viewed -> CLOSED
        assert client.get(f"/api/receipt/{sid}").json()["payment"] == p  # still readable afterwards


def test_loyalty_off_earns_nothing(member_id, monkeypatch):
    set_features(monkeypatch, loyalty=False)
    with TestClient(app) as client:
        shop(client, member_id)
        face_id(client, member_id)
        assert client.post("/api/gate/exit/approve").json()["payment"]["points_earned"] == 0
        assert points_of(member_id) == 0


def test_passkeys_off_confirm_button(member_id, monkeypatch):
    set_features(monkeypatch, passkeys=False)
    with TestClient(app) as client:
        q = shop(client, member_id)
        assert q["instruction"]["cardholder_confirmation"]["method"] == "confirm_button"
        r = client.post("/api/gate/exit/approve")  # signed in is enough
        assert r.json()["payment"]["status"] == "AUTHORIZED"
        assert json.loads(payment_rows()[0]["instruction_json"])["cardholder_confirmation"]["method"] == "confirm_button"


def test_gates_off_checkout_button_path(member_id, monkeypatch):
    set_features(monkeypatch, gates=False, passkeys=False)
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
        login_as(client, member_id)
        assert client.post("/api/dev/start").status_code == 200
        client.post("/internal/shelf", json=snap(ONE_ELX, frame_id=2), headers=HEADERS)
        q = client.post("/api/dev/checkout").json()
        assert q["instruction"]["amount_usd"] == 8.64
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "AUTHORIZED"


def test_paid_session_closes_after_60_s(member_id):
    with TestClient(app) as client:
        q = shop(client, member_id)
        face_id(client, member_id)
        client.post("/api/gate/exit/approve")
    sid = q["cart"]["session_id"]
    assert store.close_paid_sessions() == []
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    assert store.close_paid_sessions(later) == [sid] and store.get_session(sid)["state"] == "CLOSED"


def test_instruction_renewed_when_expired(member_id, monkeypatch):
    with TestClient(app) as client:
        q = shop(client, member_id)
        old = q["instruction"]
        with payments._lock:
            payments._pending[q["cart"]["session_id"]]["agent_token"]["scope"]["expires_at"] = "2000-01-01T00:00:00Z"
        again = client.post("/api/gate/exit/quote", json=EXIT).json()["instruction"]
        assert again["instruction_id"] != old["instruction_id"]


# --- F18: the exit quote carries plan_check (intent.compare_cart_to_plan) -------------------------

def _plan(**over):
    plan = {"plan_id": "pln_x", "goal_summary": "post run recovery", "budget_usd": 15.0,
            "items": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1}],
            "est_total_usd": 8.64, "fits_budget": True, "bays": [0], "source": "rules"}
    plan.update(over)
    return plan


def test_quote_has_no_plan_check_without_a_plan(member_id):
    with TestClient(app) as client:
        assert shop(client, member_id)["plan_check"] is None


def test_quote_plan_check_matches_the_cart(monkeypatch, member_id):
    from backend import intent

    monkeypatch.setattr(intent, "current_plan", lambda mid: _plan() if mid == member_id else None)
    with TestClient(app) as client:
        check = shop(client, member_id)["plan_check"]
    assert check["matches"] is True
    assert check["missing"] == [] and check["extra"] == []
    assert "post run recovery" in check["summary"] and "$8.64" in check["summary"]


def test_quote_plan_check_reports_a_missing_item(monkeypatch, member_id):
    from backend import intent

    plan = _plan(items=[{"sku": "elx", "name": "Electrolyte tabs", "qty": 1},
                        {"sku": "rec", "name": "Recovery drink", "qty": 1}])
    monkeypatch.setattr(intent, "current_plan", lambda mid: plan if mid == member_id else None)
    with TestClient(app) as client:
        check = shop(client, member_id)["plan_check"]  # only the electrolytes were picked up
    assert check["matches"] is False
    assert check["missing"] == [{"sku": "rec", "name": "Recovery drink", "qty": 1}]
    assert check["extra"] == []


def test_empty_cart_quote_has_a_plan_check_key(member_id):
    """The "nothing to pay" branch returns early; the key must still be there for exit.js."""
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
        face_id(client, member_id)
        assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
        q = client.post("/api/gate/exit/quote", json=EXIT).json()
        assert q["cart"]["state"] == "CLOSED"
        assert q["instruction"] is None and q["plan_check"] is None
