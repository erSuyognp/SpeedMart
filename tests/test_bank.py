"""Demo bank (8.15): the simulated account behind the demo card.

Ledger math (current vs available with a pending hold), charge and refund rows through the real checkout and
return flows, the insufficient funds decline, the top up limit, the admin reset, the WebSocket push, the
startup backfill, the receipt line and the config defaults. No Stripe, no LLM, no serial port."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import admin, bank, db, eventlog, settings as settings_mod, ws
from backend.main import app
from identity_helpers import login_as, set_features
from test_cart import FULL, TOKEN, events, make_member, member_id, snap, tmp_data, with_bays  # noqa: F401

pytestmark = pytest.mark.usefixtures("tmp_data")

HEADERS = {"X-Internal-Token": TOKEN}
ENTRY = {"gate_token": "test-entry-token"}
EXIT = {"gate_token": "test-exit-token"}
ONE_ELX = with_bays(b0=[1])  # 1 x $3.50 -> $3.78 with tax
ELX_AND_BAR = with_bays(b0=[1], b2=[5])  # $3.50 + $2.50 = $6.00 -> $6.48 with tax


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True)
    monkeypatch.setattr(admin, "force_decline", False)


@pytest.fixture
def pushed(monkeypatch):
    """Every socket publish, so a test can assert the {"type":"bank"} pushes (8.4)."""
    seen: list[tuple[dict, str | None, bool, bool]] = []

    def publish(msg, *, member_id=None, admin=False, everyone=False):
        seen.append((msg, member_id, admin, everyone))

    monkeypatch.setattr(ws.manager, "publish", publish)
    return seen


def face_id(client: TestClient, mid: str) -> None:
    login_as(client, mid, verified_at=time.time() - 1, verified_member=mid, verified_purpose="exit")


def shelf(client: TestClient, bays, frame_id: int) -> None:
    assert client.post("/internal/shelf", json=snap(bays, frame_id=frame_id), headers=HEADERS).status_code == 200


def quote(client: TestClient, mid: str, taken=ONE_ELX) -> dict:
    """Enter with Face ID, pick, scan the exit. Returns the quote; a second Face ID is ready for approve."""
    shelf(client, FULL, 1)
    face_id(client, mid)
    assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
    shelf(client, taken, 2)
    r = client.post("/api/gate/exit/quote", json=EXIT)
    assert r.status_code == 200, r.text
    face_id(client, mid)
    return r.json()


def buy(client: TestClient, mid: str, taken=ONE_ELX) -> dict:
    quote(client, mid, taken)
    r = client.post("/api/gate/exit/approve")
    assert r.status_code == 200 and r.json()["payment"]["status"] == "AUTHORIZED", r.text
    return r.json()["payment"]


def ledger(mid: str) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM bank_ledger WHERE member_id = ? ORDER BY id", (mid,))]
    finally:
        conn.close()


def add_row(mid: str, kind: str, cents: int, status: str = "posted", related: str | None = None) -> None:
    conn = db.connect()
    try:
        bank._insert(conn, mid, kind, cents, f"test {kind}", related, status)
        conn.commit()
    finally:
        conn.close()


# --- ledger math ---

def test_opening_balance_comes_from_config(member_id):
    assert settings_mod.settings.demo_bank == {"opening_balance_usd": 50.0, "top_up_usd": 20.0, "max_top_ups": 3}
    assert settings_mod.DEFAULT_DEMO_BANK == {"opening_balance_usd": 50.0, "top_up_usd": 20.0, "max_top_ups": 3}
    s = bank.summary(member_id)
    assert s["balance_usd"] == 50.0 and s["available_usd"] == 50.0 and s["opening_balance_usd"] == 50.0
    assert s["card_label"] == "Demo Visa •••• 4242" and s["note"] == "Demo balance · not a real account"
    rows = ledger(member_id)
    assert len(rows) == 1 and rows[0]["type"] == "opening" and rows[0]["amount_cents"] == 5000
    assert rows[0]["status"] == "posted" and rows[0]["description"] == "Opening demo balance"


def test_current_is_posted_and_available_subtracts_pending_holds(member_id):
    bank.open_account(member_id)
    add_row(member_id, "charge", -378, related="pay_a")
    add_row(member_id, "refund", 162, related="ref_a")
    add_row(member_id, "hold", -1000, status="pending", related="pay_hold")  # a pre-authorization, not captured
    add_row(member_id, "top_up", 2000)
    current, available = bank.balances_cents(member_id)
    assert current == 5000 - 378 + 162 + 2000 == 6784
    assert available == 6784 - 1000 == 5784
    s = bank.summary(member_id)
    assert (s["balance_usd"], s["available_usd"], s["pending_usd"]) == (67.84, 57.84, 10.0)
    assert [t["type"] for t in s["transactions"]] == ["top_up", "hold", "refund", "charge", "opening"]  # newest first
    assert s["transactions"][1]["status"] == "pending" and s["transactions"][1]["amount_usd"] == -10.0


def test_pending_hold_turns_into_the_charge_plus_a_release(member_id):
    bank.open_account(member_id)
    add_row(member_id, "hold", -378, status="pending", related="pay_1")
    assert bank.balances_cents(member_id) == (5000, 4622)
    bank.post_charge(member_id, "pay_1", 378, [{"sku": "elx", "qty": 1}])
    rows = ledger(member_id)
    assert [(r["type"], r["amount_cents"], r["status"]) for r in rows] == [
        ("opening", 5000, "posted"), ("hold", -378, "posted"), ("hold_release", 378, "posted"), ("charge", -378, "posted")]
    assert bank.balances_cents(member_id) == (4622, 4622)


def test_summary_lists_the_last_20_transactions(member_id):
    bank.open_account(member_id)
    for n in range(25):
        add_row(member_id, "top_up", 100, related=f"t{n}")
    s = bank.summary(member_id)
    assert len(s["transactions"]) == 20 and s["transactions"][0]["related_id"] == "t24"
    assert s["balance_usd"] == 50.0 + 25.0


# --- charges and refunds through the real flows ---

def test_charge_posts_a_negative_entry_with_the_item_summary(member_id, pushed):
    with TestClient(app) as client:
        payment = buy(client, member_id, ELX_AND_BAR)
    rows = ledger(member_id)
    charge = rows[-1]
    assert charge["type"] == "charge" and charge["amount_cents"] == -648 and charge["status"] == "posted"
    assert charge["description"] == "SpeedMart #01 · 2 items" and charge["related_id"] == payment["payment_id"]
    assert bank.balances_cents(member_id) == (5000 - 648, 5000 - 648)
    e = events("bank_charge")
    assert e and e[-1]["payment_id"] == payment["payment_id"] and e[-1]["available_usd"] == 43.52
    banks = [m for m, mid, adm, _ in pushed if m["type"] == "bank"]
    assert banks and banks[-1]["data"]["available_usd"] == 43.52 and banks[-1]["data"]["balance_usd"] == 43.52
    assert all(mid == member_id and adm for m, mid, adm, _ in pushed if m["type"] == "bank")


def test_one_item_charge_says_1_item(member_id):
    with TestClient(app) as client:
        buy(client, member_id)
    assert ledger(member_id)[-1]["description"] == "SpeedMart #01 · 1 item"


def test_refund_posts_a_positive_entry(member_id, pushed):
    with TestClient(app) as client:
        payment = buy(client, member_id, ELX_AND_BAR)
        sid = client.get("/api/store/current").json()  # store is free now
        assert sid == {"session": None}
        conn = db.connect()
        try:
            session_id = conn.execute("SELECT store_session_id FROM payments WHERE id = ?",
                                      (payment["payment_id"],)).fetchone()[0]
        finally:
            conn.close()
        shelf(client, ELX_AND_BAR, 3)
        face_id(client, member_id)
        assert client.post("/api/returns/start", json={"session_id": session_id}).status_code == 200
        shelf(client, with_bays(b2=[5]), 4)  # the elx comes back
        r = client.post("/api/returns/confirm")
        assert r.status_code == 200 and r.json()["refund"]["status"] == "SUCCEEDED", r.text
        refund = r.json()["refund"]
    rows = ledger(member_id)
    assert [r["type"] for r in rows] == ["opening", "charge", "refund"]
    assert rows[-1]["amount_cents"] == 378 and rows[-1]["related_id"] == refund["refund_id"]
    assert rows[-1]["description"] == "Refund · 1 Hydration drink"
    assert bank.balances_cents(member_id) == (5000 - 648 + 378, 5000 - 648 + 378)
    banks = [m["data"] for m, *_ in pushed if m["type"] == "bank"]
    assert banks[-1]["available_usd"] == 47.3 and banks[-1]["transactions"][0]["type"] == "refund"
    assert events("bank_refund")[-1]["refund_id"] == refund["refund_id"]


def test_declined_charge_posts_nothing(member_id, monkeypatch):
    monkeypatch.setattr(admin, "force_decline", True)
    with TestClient(app) as client:
        quote(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 200 and r.json()["payment"]["status"] == "DECLINED"
    assert [r["type"] for r in ledger(member_id)] == ["opening"]


# --- insufficient funds ---

def test_insufficient_funds_declines_before_face_id_is_used(member_id, pushed):
    add_row(member_id, "charge", -4700, related="pay_earlier")  # $3.00 left, the cart is $3.78
    with TestClient(app) as client:
        quote(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 402, r.text
        body = r.json()
        assert body["error"] == "insufficient_funds"
        assert body["message"].startswith("Insufficient funds on your demo card (demo balance $3.00).")
        assert "Put something back" in body["message"]
        # No charge was made, the checkout is still pending, and the Face ID is still fresh for a retry.
        conn = db.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
            assert conn.execute("SELECT state FROM store_sessions").fetchone()[0] == "CHECKOUT_PENDING"
        finally:
            conn.close()
        e = events("bank_insufficient_funds")
        assert e and e[-1]["total_usd"] == 3.78 and e[-1]["available_usd"] == 3.0 and e[-1]["member_id"] == member_id
        assert ("gate", "declined") in [(m["type"], m["data"].get("event")) for m, *_ in pushed if m["type"] == "gate"]
        # Add demo funds, then the same approval goes through with the still fresh Face ID.
        assert client.post("/api/bank/topup").status_code == 200
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 200 and r.json()["payment"]["status"] == "AUTHORIZED", r.text
    assert bank.balances_cents(member_id) == (300 + 2000 - 378, 300 + 2000 - 378)


def test_a_pending_hold_counts_against_the_available_balance(member_id):
    add_row(member_id, "hold", -4800, status="pending", related="pay_hold")  # current $50, available $2
    with TestClient(app) as client:
        quote(client, member_id)
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 402 and "demo balance $2.00" in r.json()["message"]


def test_exact_balance_is_enough(member_id):
    add_row(member_id, "charge", -(5000 - 378), related="pay_earlier")
    with TestClient(app) as client:
        assert buy(client, member_id)["status"] == "AUTHORIZED"
    assert bank.balances_cents(member_id) == (0, 0)


# --- top ups ---

def test_top_up_adds_20_at_most_3_times(member_id, pushed):
    with TestClient(app) as client:
        login_as(client, member_id)
        for n in (1, 2, 3):
            r = client.post("/api/bank/topup")
            assert r.status_code == 200, r.text
            assert r.json()["available_usd"] == 50 + 20 * n and r.json()["top_ups_used"] == n
            assert r.json()["top_ups_left"] == 3 - n
        r = client.post("/api/bank/topup")
        assert r.status_code == 409 and r.json()["error"] == "topup_limit"
        assert "all 3 demo top ups" in r.json()["message"]
    rows = [r for r in ledger(member_id) if r["type"] == "top_up"]
    assert len(rows) == 3 and all(r["amount_cents"] == 2000 and r["description"] == "Demo top up" for r in rows)
    assert len(events("bank_topup")) == 3 and events("bank_topup_limit")
    assert len([m for m, *_ in pushed if m["type"] == "bank"]) == 3  # a refused top up pushes nothing


def test_bank_routes_need_a_signed_in_member():
    with TestClient(app) as client:
        assert client.get("/api/bank").status_code == 401
        assert client.post("/api/bank/topup").status_code == 401


def test_get_bank_returns_the_summary(member_id):
    with TestClient(app) as client:
        login_as(client, member_id)
        s = client.get("/api/bank").json()
    assert set(s) == {"card_label", "note", "balance_usd", "available_usd", "pending_usd", "opening_balance_usd",
                      "top_up_usd", "top_ups_used", "top_ups_left", "transactions"}
    assert s["top_up_usd"] == 20.0 and s["top_ups_left"] == 3 and len(s["transactions"]) == 1
    assert set(s["transactions"][0]) == {"id", "type", "amount_usd", "status", "description", "related_id", "created_at"}


# --- admin reset ---

def test_admin_reset_restores_every_member_to_the_opening_balance(member_id, pushed):
    other = make_member("Maya")
    with TestClient(app) as client:
        buy(client, member_id, ELX_AND_BAR)
        login_as(client, other)
        client.post("/api/bank/topup")
        client.post("/api/bank/topup")
        assert bank.summary(other)["available_usd"] == 90.0 and bank.summary(member_id)["available_usd"] == 43.52
        r = client.post("/admin/bank/reset")
        assert r.status_code == 401  # team only
        client.cookies.clear()
        assert client.post("/admin/login", json={"password": "test-admin-password"}).status_code == 200
        pushed.clear()
        r = client.post("/admin/bank/reset")
        assert r.status_code == 200 and r.json() == {"ok": True, "members": 2, "opening_balance_usd": 50.0}
    for mid in (member_id, other):
        s = bank.summary(mid)
        assert s["available_usd"] == s["balance_usd"] == 50.0 and s["top_ups_used"] == 0 and s["top_ups_left"] == 3
        assert [t["type"] for t in s["transactions"]] == ["opening"]
    assert sorted(mid for m, mid, *_ in pushed if m["type"] == "bank") == sorted([member_id, other])
    assert events("bank_reset")[-1]["members"] == 2


# --- backfill ---

def test_backfill_gives_existing_members_and_the_demo_member_an_opening_balance(member_id):
    old = make_member("Old")  # a member from before the demo bank existed: no ledger row at all
    conn = db.connect()
    try:
        conn.execute("DELETE FROM bank_ledger")
        conn.commit()
    finally:
        conn.close()
    assert ledger(member_id) == [] and ledger(old) == []
    done = bank.backfill()
    assert sorted(done) == sorted([member_id, old])
    for mid in (member_id, old):
        rows = ledger(mid)
        assert len(rows) == 1 and rows[0]["type"] == "opening" and rows[0]["amount_cents"] == 5000
    assert bank.backfill() == []  # idempotent
    assert events("bank_backfill")[-1]["members"] and len(ledger(member_id)) == 1


def test_startup_backfills_through_the_app_lifespan(member_id):
    old = make_member("Old")
    with TestClient(app):
        pass  # lifespan: init_db, seed_demo_member, bank.backfill
    assert ledger(old)[0]["type"] == "opening" and ledger(member_id)[0]["type"] == "opening"


def test_signup_opens_the_account():
    with TestClient(app) as client:
        m = client.post("/api/members/signup", json={"name": "Sam"}).json()["member"]
        s = client.get("/api/bank").json()
    assert s["available_usd"] == 50.0 and ledger(m["id"])[0]["type"] == "opening"
    assert events("bank_opened")[-1]["member_id"] == m["id"]


def test_a_cookie_for_a_deleted_member_opens_nothing():
    assert bank.balances_cents("mem_gone") == (0, 0)
    assert bank.summary("mem_gone")["transactions"] == []


# --- receipt and WebSocket ---

def test_receipt_shows_the_balance_after_this_purchase(member_id):
    with TestClient(app) as client:
        payment = buy(client, member_id, ELX_AND_BAR)
        client.post("/api/bank/topup")  # later movements do not change the line
        conn = db.connect()
        try:
            session_id = conn.execute("SELECT store_session_id FROM payments WHERE id = ?",
                                      (payment["payment_id"],)).fetchone()[0]
        finally:
            conn.close()
        r = client.get(f"/api/receipt/{session_id}").json()
    assert r["bank"] == {"balance_after_usd": 43.52, "card_label": "Demo Visa •••• 4242",
                         "note": "Demo balance · not a real account"}


def test_receipt_before_the_demo_bank_has_no_balance_line(member_id):
    with TestClient(app) as client:
        payment = buy(client, member_id)
        conn = db.connect()
        try:
            session_id = conn.execute("SELECT store_session_id FROM payments WHERE id = ?",
                                      (payment["payment_id"],)).fetchone()[0]
            conn.execute("DELETE FROM bank_ledger WHERE type = 'charge'")  # as if paid before the ledger existed
            conn.commit()
        finally:
            conn.close()
        assert client.get(f"/api/receipt/{session_id}").json()["bank"]["balance_after_usd"] is None


def test_websocket_pushes_bank_on_connect_and_on_every_ledger_change(member_id):
    with TestClient(app) as client:
        login_as(client, member_id)
        with client.websocket_connect("/ws") as sock:
            first = [sock.receive_json() for _ in range(2)]
            assert first[0]["type"] == "store_status"
            assert first[1]["type"] == "bank" and first[1]["data"]["available_usd"] == 50.0
            client.post("/api/bank/topup")
            msg = sock.receive_json()
            assert msg["type"] == "bank" and msg["data"]["available_usd"] == 70.0
            assert msg["data"]["transactions"][0]["type"] == "top_up"


def test_admin_socket_gets_bank_messages_and_state_carries_the_shoppers_balance(member_id):
    with TestClient(app) as client:
        assert client.post("/admin/login", json={"password": "test-admin-password"}).status_code == 200
        assert client.post("/admin/demo-login").status_code == 200
        with client.websocket_connect("/ws?role=admin") as sock:
            while sock.receive_json()["type"] != "shelf":
                pass
            client.post("/api/bank/topup")
            msg = sock.receive_json()
            while msg["type"] != "bank":
                msg = sock.receive_json()
            assert msg["data"]["available_usd"] == 70.0
        assert client.get("/admin/state").json()["bank"] is None  # nobody in the store
        shelf(client, FULL, 1)
        assert client.post("/api/dev/start").status_code == 200
        assert client.get("/admin/state").json()["bank"]["available_usd"] == 70.0


def test_disabled_features_do_not_break_the_bank(member_id, monkeypatch):
    set_features(monkeypatch, gates=False, passkeys=False, stripe=False, loyalty=False, disputes=False, llm=False)
    with TestClient(app) as client:
        login_as(client, member_id)
        shelf(client, FULL, 1)
        assert client.post("/api/dev/start").status_code == 200
        shelf(client, ONE_ELX, 2)
        assert client.post("/api/dev/checkout").status_code == 200
        r = client.post("/api/gate/exit/approve")
        assert r.status_code == 200 and r.json()["payment"]["status"] == "AUTHORIZED"
    assert bank.balances_cents(member_id) == (5000 - 378, 5000 - 378)


def test_bad_demo_bank_config_is_refused():
    from backend.settings import SettingsError, _build
    import json
    from backend.settings import CATALOG_PATH, CONFIG_PATH

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    env = settings_mod.settings.env
    config["demo_bank"] = {"opening_balance_usd": -1}
    with pytest.raises(SettingsError, match="demo_bank.opening_balance_usd"):
        _build(env, config, catalog)
    config["demo_bank"] = {"max_top_ups": 2.5}
    with pytest.raises(SettingsError, match="demo_bank.max_top_ups"):
        _build(env, config, catalog)
    del config["demo_bank"]  # optional section: the defaults apply
    assert _build(env, config, catalog).demo_bank == settings_mod.DEFAULT_DEMO_BANK
