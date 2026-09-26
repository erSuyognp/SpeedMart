"""Continue stage: returns with camera-verified refunds, the permissions card and measured results.

Refund math in cents with tax, never more than purchased, never twice, the 30 minute window, Face ID, Stripe
refunds (faked: no test reaches Stripe), mock fallback, the store lock, points, the gate screen, the receipt,
/api/guardrails and the admin metrics."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import admin, db, payments, returns, serial_bridge, store
from backend.main import app
from identity_helpers import login_as, set_env, set_features
from test_cart import FULL, TOKEN, make_member, member_id, snap, tmp_data, with_bays  # noqa: F401
from test_serial_display import shown  # noqa: F401
from test_stripe import TEST_KEY, FakeStripe

pytestmark = pytest.mark.usefixtures("tmp_data")

HEADERS = {"X-Internal-Token": TOKEN}
ENTRY = {"gate_token": "test-entry-token"}
EXIT = {"gate_token": "test-exit-token"}
ELX_TAKEN = with_bays(b0=[1])              # 1 elx off the shelf: $8.00 + $0.64 tax
ELX_AND_BAR_TAKEN = with_bays(b0=[1], b2=[5])  # 1 elx + 1 bar: $11.50 + $0.92 = $12.42


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True)
    monkeypatch.setattr(admin, "force_decline", False)


def face_id(client: TestClient, mid: str) -> None:
    login_as(client, mid, verified_at=time.time() - 1, verified_member=mid, verified_purpose="exit")


def shelf(client: TestClient, bays, frame_id: int) -> None:
    assert client.post("/internal/shelf", json=snap(bays, frame_id=frame_id), headers=HEADERS).status_code == 200


def buy(client: TestClient, mid: str, taken=ELX_TAKEN) -> str:
    """Enter, pick, exit, approve with Face ID. Returns the paid session id; the items stay off the shelf."""
    shelf(client, FULL, 1)
    face_id(client, mid)
    assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
    shelf(client, taken, 2)
    assert client.post("/api/gate/exit/quote", json=EXIT).status_code == 200
    face_id(client, mid)
    r = client.post("/api/gate/exit/approve")
    assert r.status_code == 200 and r.json()["payment"]["status"] == "AUTHORIZED", r.text
    return paid_session_id(r.json()["payment"]["payment_id"])


def paid_session_id(payment_id: str) -> str:
    conn = db.connect()
    try:
        return conn.execute("SELECT store_session_id FROM payments WHERE id = ?", (payment_id,)).fetchone()[0]
    finally:
        conn.close()


def start_return(client: TestClient, mid: str, sid: str):
    face_id(client, mid)
    return client.post("/api/returns/start", json={"session_id": sid})


def points_of(mid: str) -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT points FROM members WHERE id = ?", (mid,)).fetchone()[0]
    finally:
        conn.close()


def refund_rows() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM refunds ORDER BY rowid")]
    finally:
        conn.close()


def log_entries() -> list[dict]:
    return [json.loads(line) for line in payments.payments_log_path().read_text(encoding="utf-8").splitlines()]


def age_payment(minutes: float) -> None:
    """Pretend every payment happened `minutes` ago."""
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = db.connect()
    try:
        conn.execute("UPDATE payments SET created_at = ?", (when,))
        conn.commit()
    finally:
        conn.close()


# --- refund amount math (pure, in cents) ---

def test_refund_amount_is_items_plus_tax_in_cents():
    units = {"elx": 800, "bar": 350}
    # one elx of one: exactly what was paid for it
    assert returns.refund_amount({"elx": 1}, {"elx": 1}, units, 864, 0, 0.08) == (800, 64, 864)
    # one bar of a two-item visit ($12.42 paid): 350 + round(28.0) = 378
    assert returns.refund_amount({"bar": 1}, {"elx": 1, "bar": 1}, units, 1242, 0, 0.08) == (350, 28, 378)
    # nothing back, nothing refunded
    assert returns.refund_amount({}, {"elx": 1}, units, 864, 0, 0.08) == (0, 0, 0)


def test_refund_rounding_never_exceeds_the_payment():
    # 3 x $0.05 at 10 %: paid 15 + round(1.5) = 17. One at a time: 5 + round(0.5) = 6 each, 6 + 6 = 12,
    # and the last return refunds exactly what is left (5), so the three refunds add up to the 17 charged.
    units = {"x": 5}
    first = returns.refund_amount({"x": 1}, {"x": 3}, units, 17, 0, 0.10)
    second = returns.refund_amount({"x": 1}, {"x": 2}, units, 17, first[2], 0.10)
    last = returns.refund_amount({"x": 1}, {"x": 1}, units, 17, first[2] + second[2], 0.10)
    assert first[2] == second[2] == 6 and last[2] == 5
    assert first[2] + second[2] + last[2] == 17


def test_detect_caps_at_purchase_and_ignores_the_rest():
    baseline = {"elx": 1, "rec": 2, "bar": 2}
    shelf_now = {"elx": 2, "rec": 3, "bar": 2}  # elx back, plus a recovery drink that was never bought
    assert returns.detect(baseline, shelf_now, {"elx": 1}) == ({"elx": 1}, {"rec": 1})
    # two elx appear but only one was bought: one refunded, one ignored
    assert returns.detect({"elx": 0}, {"elx": 2}, {"elx": 1}) == ({"elx": 1}, {"elx": 1})
    # a count that drops (someone picks something up) never becomes a negative return
    assert returns.detect({"elx": 2}, {"elx": 1}, {"elx": 1}) == ({}, {})


# --- the whole flow, mock provider ---

def test_return_refunds_exactly_what_the_camera_sees(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        assert points_of(member_id) == 8
        r = start_return(client, member_id, sid)
        assert r.status_code == 200, r.text
        ret = r.json()["return"]
        assert ret["state"] == "RETURNING" and ret["original_session_id"] == sid
        assert ret["items"] == [] and ret["total_usd"] == 0 and ret["returnable"][0]["qty"] == 1
        session = store.current_session()
        assert session["state"] == "RETURNING" and session["return_of"] == sid
        assert session["baseline"]["elx"] == 1  # baseline = the shelf when the return started

        # Nothing seen yet: confirm refuses.
        assert client.post("/api/returns/confirm").json()["error"] == "nothing_returned"

        shelf(client, FULL, 3)  # the elx goes back
        live = client.get("/api/returns/current").json()["return"]
        assert [(i["sku"], i["qty"]) for i in live["items"]] == [("elx", 1)]
        assert (live["subtotal_usd"], live["tax_usd"], live["total_usd"]) == (8.0, 0.64, 8.64)

        r = client.post("/api/returns/confirm")
        assert r.status_code == 200, r.text
        refund = r.json()["refund"]
        assert refund["status"] == "SUCCEEDED" and refund["provider"] == "mock"
        assert refund["provider_ref"].startswith("re_mock_") and refund["refund_id"].startswith("ref_")
        assert refund["amount_usd"] == 8.64 and refund["items_text"] == "1 Electrolyte tabs"
        assert r.json()["agent_line"] == "Refund of $8.64 is on its way to your Visa ending 4242."

        assert store.get_session(ret["session_id"])["state"] == "CLOSED" and store.current_session() is None
        assert points_of(member_id) == 0 and refund["points_removed"] == 8
        [row] = refund_rows()
        assert row["amount_cents"] == 864 and row["payment_id"] == refund["payment_id"]
        logged = [e for e in log_entries() if e.get("type") == "REFUND"]
        assert len(logged) == 1 and logged[0]["refund_id"] == refund["refund_id"] and logged[0]["amount_usd"] == 8.64

        receipt = client.get(f"/api/receipt/{sid}").json()
        assert [f["refund_id"] for f in receipt["refunds"]] == [refund["refund_id"]]
        assert receipt["refunded_usd"] == 8.64
        assert receipt["return"]["eligible"] is False and receipt["return"]["reason"] == "nothing_to_return"


def test_other_items_are_not_from_this_purchase(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        shelf(client, with_bays(b0=[1], b1=[2]), 3)  # a recovery drink is off the shelf when the return starts
        assert start_return(client, member_id, sid).status_code == 200
        shelf(client, with_bays(b0=[1]), 4)  # it lands back on the shelf, but it was never bought
        live = client.get("/api/returns/current").json()["return"]
        assert live["items"] == [] and live["total_usd"] == 0
        assert live["ignored"] == [{"sku": "rec", "name": "Recovery drink", "qty": 1, "message": "not from this purchase"}]
        assert client.post("/api/returns/confirm").json()["error"] == "nothing_returned"
        shelf(client, FULL, 5)  # now the elx too: only the elx is refunded
        live = client.get("/api/returns/current").json()["return"]
        assert [(i["sku"], i["qty"]) for i in live["items"]] == [("elx", 1)] and live["total_usd"] == 8.64
        assert [i["sku"] for i in live["ignored"]] == ["rec"]


def test_cannot_refund_more_than_purchased(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        # The other elx is off the shelf too when the return starts, so two come back but one was bought.
        shelf(client, with_bays(b0=[]), 3)
        assert start_return(client, member_id, sid).status_code == 200
        shelf(client, FULL, 4)  # both elx back: a rise of 2
        live = client.get("/api/returns/current").json()["return"]
        assert [(i["sku"], i["qty"]) for i in live["items"]] == [("elx", 1)]
        assert live["ignored"] == [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1, "message": "not from this purchase"}]
        refund = client.post("/api/returns/confirm").json()["refund"]
        assert refund["amount_usd"] == 8.64  # one unit, never two


def test_cannot_refund_twice(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        assert start_return(client, member_id, sid).status_code == 200
        shelf(client, FULL, 3)
        assert client.post("/api/returns/confirm").status_code == 200
        # The same return again: it is closed.
        assert client.post("/api/returns/confirm").json()["error"] == "no_active_return"
        # A new return for the same visit: nothing left to refund, and the Face ID is not used up.
        r = start_return(client, member_id, sid)
        assert r.status_code == 409 and r.json()["error"] == "nothing_to_return"
        assert len(refund_rows()) == 1


def test_partial_returns_add_up_to_the_payment(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id, taken=ELX_AND_BAR_TAKEN)  # $12.42
        assert start_return(client, member_id, sid).status_code == 200
        shelf(client, with_bays(b0=[1]), 3)  # the bar comes back
        first = client.post("/api/returns/confirm").json()["refund"]
        assert first["amount_usd"] == 3.78  # 3.50 + 0.28
        r = start_return(client, member_id, sid)
        assert r.status_code == 200 and r.json()["return"]["returnable"] == [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1}]
        shelf(client, FULL, 4)
        second = client.post("/api/returns/confirm").json()["refund"]
        assert second["amount_usd"] == 8.64
        assert sum(r["amount_cents"] for r in refund_rows()) == 1242
        assert points_of(member_id) == 12 - 3 - 8


# --- window, Face ID, lock ---

def test_thirty_minute_window(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        age_payment(29)
        assert client.get(f"/api/receipt/{sid}").json()["return"]["eligible"] is True
        age_payment(31)
        assert client.get(f"/api/receipt/{sid}").json()["return"]["reason"] == "return_window_closed"
        r = start_return(client, member_id, sid)
        assert r.status_code == 409 and r.json()["error"] == "return_window_closed"
        assert store.current_session() is None


def test_face_id_required(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        login_as(client, member_id)  # signed in, no fresh Face ID
        r = client.post("/api/returns/start", json={"session_id": sid})
        assert r.status_code == 401 and r.json()["error"] == "not_verified"
        # stale verification (older than 90 s) does not count either
        login_as(client, member_id, verified_at=time.time() - 120, verified_member=member_id)
        assert client.post("/api/returns/start", json={"session_id": sid}).status_code == 401
        assert store.current_session() is None
        assert start_return(client, member_id, sid).status_code == 200


def test_only_your_own_paid_visits(member_id):
    other = make_member("Ana")
    with TestClient(app) as client:
        sid = buy(client, member_id)
        face_id(client, other)
        assert client.post("/api/returns/start", json={"session_id": sid}).status_code == 404
        # an unpaid (cancelled) visit cannot be returned
        shelf(client, FULL, 5)
        face_id(client, other)
        assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
        cancelled = store.cancel(reason="admin")
        face_id(client, other)
        r = client.post("/api/returns/start", json={"session_id": cancelled["id"]})
        assert r.status_code == 409 and r.json()["error"] == "not_returnable"


def test_store_lock_respected(member_id):
    other = make_member("Ana")
    with TestClient(app) as client:
        sid = buy(client, member_id)
        # Someone else is shopping: the return is refused with the usual occupied message.
        face_id(client, other)
        assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
        r = start_return(client, member_id, sid)
        assert r.status_code == 409 and r.json() == {
            "error": "store_occupied", "message": "Someone is already shopping. Please wait a moment.",
            "occupant_first_name": "Ana"}
        store.cancel(reason="admin")

        # Returning holds the lock: nobody can enter, and the returner cannot shop meanwhile.
        assert start_return(client, member_id, sid).status_code == 200
        face_id(client, other)
        r = client.post("/api/gate/enter", json=ENTRY)
        assert r.status_code == 409 and r.json()["occupant_first_name"] == "Demo"
        face_id(client, member_id)
        assert client.post("/api/gate/enter", json=ENTRY).json()["error"] == "returning"
        assert client.post("/api/gate/exit/quote", json=EXIT).json()["error"] == "no_active_session"
        assert client.get("/api/store/current").json()["cart"]["items"] == []


def test_cancel_closes_without_refund(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        ret = start_return(client, member_id, sid).json()["return"]
        shelf(client, FULL, 3)
        assert client.post("/api/returns/cancel").json() == {"ok": True}
        assert store.get_session(ret["session_id"])["state"] == "CLOSED" and store.current_session() is None
        assert refund_rows() == [] and points_of(member_id) == 8
        assert client.get("/api/returns/current").json() == {"return": None}


def test_return_times_out_after_three_minutes(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        ret = start_return(client, member_id, sid).json()["return"]
        started = datetime.fromisoformat(ret["expires_at"].replace("Z", "+00:00")) - timedelta(minutes=3)
        assert store.expire_stale_sessions(now=started + timedelta(minutes=2)) == []
        assert store.expire_stale_sessions(now=started + timedelta(minutes=3, seconds=5)) == [ret["session_id"]]
        assert store.get_session(ret["session_id"])["state"] == "CANCELLED" and refund_rows() == []


# --- Stripe ---

class FakeStripeWithRefunds(FakeStripe):
    def __init__(self):
        super().__init__()
        self.refund_error: Exception | None = None
        fake = self

        class Refund:
            @staticmethod
            def create(**kw):
                fake.calls.append(("Refund.create", kw))
                if fake.refund_error:
                    raise fake.refund_error
                return type("R", (), {"id": "re_3QtestREFUND01", "status": "succeeded"})()

        self.Refund = Refund


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripeWithRefunds()
    monkeypatch.setattr(payments, "stripe", fake)
    monkeypatch.setattr(payments, "_stripe_ready", False)
    set_features(monkeypatch, stripe=True, gates=True, passkeys=True, loyalty=True)
    set_env(monkeypatch, stripe_secret_key=TEST_KEY)
    return fake


def test_stripe_partial_refund_on_the_original_payment_intent(member_id, fake_stripe):
    with TestClient(app) as client:
        sid = buy(client, member_id, taken=ELX_AND_BAR_TAKEN)
        ret = start_return(client, member_id, sid).json()["return"]
        shelf(client, with_bays(b0=[1]), 3)
        refund = client.post("/api/returns/confirm").json()["refund"]
        [kw] = fake_stripe.named("Refund.create")
        assert kw["payment_intent"] == "pi_3QabcdEFGH12xyz9" and kw["amount"] == 378
        assert kw["idempotency_key"] == f"speedmart-refund-{ret['session_id']}-1"
        assert kw["metadata"]["session_id"] == sid
        assert refund["provider"] == "stripe_test" and refund["provider_ref"] == "re_3QtestREFUND01"
        assert refund["status"] == "SUCCEEDED" and refund["amount_usd"] == 3.78


def test_stripe_error_falls_back_to_mock(member_id, fake_stripe):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        fake_stripe.refund_error = FakeStripe.APIConnectionError("network down")
        start_return(client, member_id, sid)
        shelf(client, FULL, 3)
        refund = client.post("/api/returns/confirm").json()["refund"]
        assert len(fake_stripe.named("Refund.create")) == 1
        assert refund["provider"] == "mock" and refund["status"] == "SUCCEEDED" and refund["amount_usd"] == 8.64


def test_stripe_card_error_fails_and_keeps_the_return_open(member_id, fake_stripe):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        fake_stripe.refund_error = FakeStripe.CardError("refund refused", user_message="Refund refused.")
        ret = start_return(client, member_id, sid).json()["return"]
        shelf(client, FULL, 3)
        body = client.post("/api/returns/confirm").json()
        assert body["refund"]["status"] == "FAILED" and body["message"] == "Refund refused."
        assert store.current_session()["state"] == "RETURNING" and points_of(member_id) == 8
        fake_stripe.refund_error = None  # retry uses a new idempotency key
        assert client.post("/api/returns/confirm").json()["refund"]["status"] == "SUCCEEDED"
        keys = [kw["idempotency_key"] for kw in fake_stripe.named("Refund.create")]
        assert keys == [f"speedmart-refund-{ret['session_id']}-1", f"speedmart-refund-{ret['session_id']}-2"]


def test_mock_payment_refunds_through_mock_even_with_stripe_on(member_id, monkeypatch):
    with TestClient(app) as client:
        sid = buy(client, member_id)  # stripe off: a mock payment
        fake = FakeStripeWithRefunds()
        monkeypatch.setattr(payments, "stripe", fake)
        monkeypatch.setattr(payments, "_stripe_ready", False)
        set_features(monkeypatch, stripe=True)
        set_env(monkeypatch, stripe_secret_key=TEST_KEY)
        start_return(client, member_id, sid)
        shelf(client, FULL, 3)
        assert client.post("/api/returns/confirm").json()["refund"]["provider"] == "mock"
        assert fake.named("Refund.create") == []


# --- gate screen ---

def test_refund_event_shows_refund_screen_then_idle(shown, monkeypatch):
    monkeypatch.setattr(serial_bridge, "REFUND_HOLD_S", 0.1)
    d = serial_bridge.director
    d.handle({"type": "refund", "status": "SUCCEEDED", "amount_usd": 8.64})
    assert shown[-1] == ("REFUND", "$8.64")
    d.handle({"type": "session_state", "from": "RETURNING", "to": "CLOSED"})
    assert d.screen == ("REFUND", "$8.64")  # held, not cut to IDLE at once
    time.sleep(0.3)
    assert shown[-1] == ("IDLE",)
    d.handle({"type": "refund", "status": "FAILED", "amount_usd": 8.64})
    assert shown[-1] == ("IDLE",)
    assert serial_bridge.display_command("REFUND", "$8.64") == "DISP,REFUND,$8.64"


# --- measured results ---

def test_receipt_and_metrics_measure_the_visit(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        session = store.get_session(sid)
        assert session["first_pick_at"] and session["quoted_at"] and session["approved_at"]
        receipt = client.get(f"/api/receipt/{sid}").json()
        assert receipt["approvals"] == 1 and receipt["in_and_out_s"] == store.seconds_between(
            session["started_at"], session["approved_at"])
        start_return(client, member_id, sid)
        shelf(client, FULL, 3)
        client.post("/api/returns/confirm")

        client.post("/admin/login", json={"password": "test-admin-password"})
        m = client.get("/admin/state").json()["metrics"]
        assert m["sessions_today"] == 1 and m["paid_today"] == 1  # the return is not a shopping session
        assert m["avg_in_store_s"] is not None and m["avg_exit_to_approval_s"] is not None
        assert m["refunds_today"] == 1 and m["refunded_usd_today"] == 8.64
        events = [json.loads(line)["type"] for line in
                  (db.DATA_DIR / "events.log.jsonl").read_text(encoding="utf-8").splitlines()]
        for kind in ("first_pick", "session_metrics", "return_started", "return_detected", "refund"):
            assert kind in events


def test_declined_attempt_counts_as_a_tap(member_id, monkeypatch):
    with TestClient(app) as client:
        shelf(client, FULL, 1)
        face_id(client, member_id)
        client.post("/api/gate/enter", json=ENTRY)
        shelf(client, ELX_TAKEN, 2)
        client.post("/api/gate/exit/quote", json=EXIT)
        monkeypatch.setattr(admin, "force_decline", True)
        face_id(client, member_id)
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "DECLINED"
        monkeypatch.setattr(admin, "force_decline", False)
        face_id(client, member_id)
        sid = paid_session_id(client.post("/api/gate/exit/approve").json()["payment"]["payment_id"])
        assert client.get(f"/api/receipt/{sid}").json()["approvals"] == 2


# --- permissions card ---

def test_guardrails_come_from_the_member_and_scope(member_id, monkeypatch):
    with TestClient(app) as client:
        assert client.get("/api/guardrails").status_code == 401
        mid = make_member("Maya")
        conn = db.connect()
        conn.execute("UPDATE members SET budget_usd = 12.5 WHERE id = ?", (mid,))
        conn.commit()
        conn.close()
        login_as(client, mid)
        g = client.get("/api/guardrails").json()
        assert g["title"] == "Your agent's permissions"
        assert g["agent_can"] == ["See the shelf", "Suggest items", "Build your cart"]
        assert g["only_you"] == ["Approve a payment with Face ID", "Spend more than your $12.50 limit",
                                 "Request a refund"]
        assert g["never"] == ["Your face data leaving your phone", "A reusable payment token",
                              "A charge without your approval"]
        assert g["scope"]["max_amount_usd"] == 12.5 and g["scope"]["single_use"] is True
        assert g["source"] == "member"

        set_features(monkeypatch, passkeys=False)
        assert client.get("/api/guardrails").json()["only_you"][0] == "Approve a payment with a tap on Confirm"


def test_guardrails_use_the_pending_instruction(member_id):
    with TestClient(app) as client:
        shelf(client, FULL, 1)
        face_id(client, member_id)
        client.post("/api/gate/enter", json=ENTRY)
        shelf(client, ELX_TAKEN, 2)
        instr = client.post("/api/gate/exit/quote", json=EXIT).json()["instruction"]
        g = client.get("/api/guardrails").json()
        assert g["source"] == "instruction"
        assert g["scope"]["expires_at"] == instr["agent_token"]["scope"]["expires_at"]
        assert "Spend more than your $20 limit" in g["only_you"]


def test_old_database_gets_the_new_columns(tmp_data, monkeypatch):
    """A data/speedmart.db from before this change: no refunds table, no timestamp columns."""
    import sqlite3

    old = tmp_data / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE store_sessions (id TEXT PRIMARY KEY, member_id TEXT NOT NULL, state TEXT NOT NULL,
          baseline_json TEXT NOT NULL, final_cart_json TEXT, started_at TEXT NOT NULL, ended_at TEXT);
        INSERT INTO store_sessions VALUES ('ses_old', 'mem_x', 'CLOSED', '{}', NULL, '2026-09-25T00:00:00Z', NULL);
    """)
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()
    conn = db.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(store_sessions)")}
        assert {"return_of", "first_pick_at", "quoted_at", "approved_at"} <= cols
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'refunds'").fetchone()
        assert store.get_session("ses_old")["return_of"] is None
    finally:
        conn.close()
