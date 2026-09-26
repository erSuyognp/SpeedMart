"""S1.2 tests: shelf state, store sessions, cart engine. Fake snapshots only, temp data dir."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Set before backend.settings is imported; load_dotenv never overrides existing variables.
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["INTERNAL_TOKEN"] = "test-internal-token"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import cart, db, eventlog, routes_api, shelf_state, store  # noqa: E402
from backend.main import app  # noqa: E402

TOKEN = "test-internal-token"
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
BAY_IDS = [int(b["id"]) for b in CONFIG["bays"]]


def full_shelf() -> dict[int, list[int]]:
    """Every unit in its home bay, one entry per bay in config.json."""
    bays: dict[int, list[int]] = {b: [] for b in BAY_IDS}
    for unit in sorted(CATALOG["units"], key=lambda u: u["tag_id"]):
        bays.setdefault(int(unit["home_bay"]), []).append(int(unit["tag_id"]))
    return bays


def full_baseline(**taken: int) -> dict[str, int]:
    """The baseline of a full shelf, minus the units already gone: full_baseline(elx=1) -> one elx left."""
    counts = {s["sku"]: 0 for s in CATALOG["skus"]}
    for unit in CATALOG["units"]:
        counts[unit["sku"]] += 1
    for sku, n in taken.items():
        counts[sku] -= n
    return counts


FULL = full_shelf()


def snap(bays: dict[int, list[int]], unstable: tuple[int, ...] = (), frame_id: int = 1, loose=()) -> dict:
    return {
        "ts": 1727222400123 + frame_id,
        "frame_id": frame_id,
        "bays": [{"bay": b, "stable": b not in unstable, "motion": b in unstable, "units": u, "yolo_counts": {}}
                 for b, u in bays.items()],
        "loose_units": list(loose),
    }


def with_bays(**changes: list[int]) -> dict[int, list[int]]:
    bays = {b: list(u) for b, u in FULL.items()}
    for key, units in changes.items():
        bays[int(key.removeprefix("b"))] = units
    return bays


def qty(snapshot: dict, sku: str) -> int:
    return next((i["qty"] for i in snapshot["items"] if i["sku"] == sku), 0)


def events(event_type: str) -> list[dict]:
    if not eventlog.EVENTS_PATH.exists():
        return []
    lines = eventlog.EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == event_type]


def simulate_restart() -> None:
    """Drop everything held in memory; the DB survives, like a uvicorn restart."""
    shelf_state.reset()
    store._overrides.clear()  # memory only: data/overrides.json survives like the DB
    store._overrides_session = None
    store._last_cart_key = None


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "speedmart.db")
    monkeypatch.setattr(eventlog, "DATA_DIR", tmp_path)
    monkeypatch.setattr(eventlog, "EVENTS_PATH", tmp_path / "events.log.jsonl")
    simulate_restart()
    db.init_db()
    db.seed_demo_member()
    yield tmp_path
    simulate_restart()


@pytest.fixture
def member_id() -> str:
    conn = db.connect()
    try:
        return conn.execute("SELECT id FROM members WHERE is_demo = 1").fetchone()["id"]
    finally:
        conn.close()


def make_member(name: str) -> str:
    conn = db.connect()
    try:
        mid = db.new_id("mem")
        conn.execute("INSERT INTO members (id, name, budget_usd, created_at) VALUES (?, ?, 20, ?)",
                     (mid, name, db.now_iso()))
        conn.commit()
        return mid
    finally:
        conn.close()


@pytest.fixture
def shopping(member_id):
    """Full shelf, demo member in the store."""
    shelf_state.apply_snapshot(snap(FULL))
    return store.start_session(member_id)


def push(bays, **kw) -> dict:
    if shelf_state.apply_snapshot(snap(bays, **kw)):
        store.on_shelf_change()
    return store.current_cart()


# --- cases listed in S1.2 ---

def test_pick_then_put_back(shopping):
    assert shopping["baseline"] == full_baseline()
    assert store.current_cart()["items"] == []

    cart_now = push(with_bays(b0=[1]), frame_id=2)  # tag 0 picked
    assert qty(cart_now, "elx") == 1
    assert len(cart_now["items"]) == 1

    cart_now = push(FULL, frame_id=3)
    assert qty(cart_now, "elx") == 0
    assert cart_now["items"] == []


def test_put_back_into_wrong_bay_is_on_shelf_with_warning(shopping):
    assert qty(push(with_bays(b2=[5]), frame_id=2), "bar") == 1  # tag 4 picked

    cart_now = push(with_bays(b0=[0, 1, 4], b2=[5]), frame_id=3)  # bar returned to the elx bay
    assert cart_now["items"] == []
    assert cart_now["warnings"] == [{"kind": "misplaced", "sku": "bar", "name": "Protein bar", "bay": 0,
                                     "tag_id": 4, "message": "Protein bar is in the wrong bay"}]
    assert shelf_state.misplaced()[0]["home_bay"] == 2

    cart_now = push(FULL, frame_id=4)  # moved home: warning clears
    assert cart_now["warnings"] == []


def test_unstable_bay_keeps_previous_contents(shopping):
    # Hand covers bay 0: vision sees no tags there, but the bay is unstable.
    cart_now = push(with_bays(b0=[]), unstable=(0,), frame_id=2)
    assert cart_now["items"] == []
    assert shelf_state.shelf_counts()["elx"] == 2

    # Several flickering unstable frames in a row: still no change.
    for f, units in enumerate([[0], [], [1], []], start=3):
        assert qty(push(with_bays(b0=units), unstable=(0,), frame_id=f), "elx") == 0

    # Hand leaves with one unit: bay 0 turns stable showing one tag.
    assert qty(push(with_bays(b0=[1]), frame_id=10), "elx") == 1


def test_items_missing_before_entry_are_not_charged(member_id):
    shelf_state.apply_snapshot(snap(with_bays(b0=[1], b1=[])))  # tag 0 and both recovery drinks already gone
    session = store.start_session(member_id)
    assert session["baseline"] == full_baseline(elx=1, rec=2)
    assert store.current_cart()["items"] == []

    cart_now = push(with_bays(b0=[], b1=[]), frame_id=2)  # shopper takes the last elx
    assert qty(cart_now, "elx") == 1
    assert qty(cart_now, "rec") == 0
    assert cart_now["total_usd"] == 8.64


def test_repeated_identical_snapshots_do_not_change_cart(shopping):
    picked = with_bays(b0=[1])
    first = push(picked, frame_id=2)
    changes_before = len(events("cart_changed"))
    for f in range(3, 23):
        assert shelf_state.apply_snapshot(snap(picked, frame_id=f)) is False
        again = push(picked, frame_id=f)
        assert again["items"] == first["items"]
        assert again["total_usd"] == first["total_usd"]
    assert len(events("cart_changed")) == changes_before
    assert qty(store.current_cart(), "elx") == 1


def test_override_clamps_to_zero_and_baseline(shopping):
    assert qty(store.apply_override("elx", +1), "elx") == 1
    assert qty(store.apply_override("elx", +1), "elx") == 2
    assert qty(store.apply_override("elx", +1), "elx") == 2  # baseline is 2
    assert qty(store.apply_override("elx", -1), "elx") == 1  # moves at once, no hidden excess
    assert qty(store.apply_override("elx", -1), "elx") == 0
    assert qty(store.apply_override("elx", -1), "elx") == 0  # never below 0
    assert qty(store.apply_override("rec", +1), "elx") == 0
    assert len(events("override")) == 7
    assert all(e["source"] == "override" for e in events("override"))

    # compute_cart clamps raw overrides too.
    member = {"budget_usd": 20}
    session = {"id": "ses_x", "state": "IN_STORE", "baseline": {"elx": 2, "rec": 2, "bar": 2}}
    shelf = {"elx": 2, "rec": 0, "bar": 2}
    c = cart.compute_cart(session, shelf, {"elx": 99, "rec": -99}, member)
    assert qty(c, "elx") == 2 and qty(c, "rec") == 0


def test_second_start_session_while_occupied_raises(shopping):
    other = make_member("Maya Lin")
    with pytest.raises(store.StoreOccupied) as err:
        store.start_session(other)
    assert err.value.code == "store_occupied"
    assert err.value.occupant_first_name == "Demo"

    store.freeze_cart()  # CHECKOUT_PENDING still holds the lock
    with pytest.raises(store.StoreOccupied):
        store.start_session(other)

    store.cancel(reason="admin")
    assert store.start_session(other)["state"] == store.IN_STORE


def test_totals_one_electrolyte_at_8_percent(shopping):
    cart_now = push(with_bays(b0=[1]), frame_id=2)
    assert cart_now["items"] == [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1,
                                  "unit_price_usd": 8.00, "line_total_usd": 8.00}]
    assert cart_now["subtotal_usd"] == 8.00
    assert cart_now["tax_usd"] == 0.64
    assert cart_now["total_usd"] == 8.64
    assert cart_now["over_budget"] is False


# --- extra cases ---

def test_totals_multi_item_integer_cents(shopping):
    cart_now = push(with_bays(b0=[1], b1=[3], b2=[]), frame_id=2)  # 1 elx, 1 rec, 2 bar
    assert cart_now["subtotal_usd"] == 19.00
    assert cart_now["tax_usd"] == 1.52
    assert cart_now["total_usd"] == 20.52
    assert cart_now["over_budget"] is True  # budget 20


def test_backend_restart_recomputes_same_cart(member_id):
    headers = {"X-Internal-Token": TOKEN}
    with TestClient(app) as client:
        assert client.post("/internal/shelf", json=snap(FULL), headers=headers).status_code == 200
        session = store.start_session(member_id)
        client.post("/internal/shelf", json=snap(with_bays(b0=[1], b1=[2, 3, 4], b2=[]), frame_id=2), headers=headers)
        before = store.current_cart()
    assert qty(before, "elx") == 1 and qty(before, "bar") == 1 and before["warnings"]

    simulate_restart()
    with TestClient(app) as client:
        # Before vision reports, the shelf is treated as unchanged rather than empty.
        assert store.current_cart()["items"] == []
        client.post("/internal/shelf", json=snap(with_bays(b0=[1], b1=[2, 3, 4], b2=[]), frame_id=900), headers=headers)
        after = store.current_cart()
    assert after["session_id"] == session["id"]
    for key in ("items", "subtotal_usd", "tax_usd", "total_usd", "warnings", "state"):
        assert after[key] == before[key]


def test_internal_shelf_rejects_wrong_token(member_id):
    with TestClient(app) as client:
        r = client.post("/internal/shelf", json=snap(FULL), headers={"X-Internal-Token": "wrong"})
        assert r.status_code == 401
        assert r.json()["error"] == "bad_internal_token"
        assert client.post("/internal/shelf", json=snap(FULL)).status_code == 401
        assert shelf_state.has_snapshot() is False

        assert client.get("/api/health").json()["vision_age_ms"] == -1
        r = client.post("/internal/shelf", json=snap(FULL), headers={"X-Internal-Token": TOKEN})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert 0 <= client.get("/api/health").json()["vision_age_ms"] < 5000


def test_freeze_unfreeze_and_state_log(shopping):
    push(with_bays(b0=[1]), frame_id=2)
    frozen = store.freeze_cart()
    assert frozen["state"] == store.CHECKOUT_PENDING and qty(frozen, "elx") == 1

    # Shelf changes during checkout do not touch the frozen cart.
    assert qty(push(FULL, frame_id=3), "elx") == 1

    live = store.unfreeze_cart()
    assert live["state"] == store.IN_STORE and live["items"] == []

    push(with_bays(b0=[1]), frame_id=4)
    store.freeze_cart()
    store.mark_paid(shopping["id"])
    assert store.current_session() is None
    assert store.close(shopping["id"])["state"] == store.CLOSED
    assert [e["to"] for e in events("session_state")] == [
        "IN_STORE", "CHECKOUT_PENDING", "IN_STORE", "CHECKOUT_PENDING", "PAID", "CLOSED"]


def test_timeout_cancels_stale_session(shopping):
    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    assert store.expire_stale_sessions(now=later) == [shopping["id"]]
    assert store.get_session(shopping["id"])["state"] == store.CANCELLED
    assert store.current_session() is None


def test_entry_refused_without_any_snapshot(member_id):
    assert shelf_state.has_snapshot() is False
    with pytest.raises(store.VisionUnavailable) as err:
        store.start_session(member_id)
    assert err.value.code == "vision_unavailable"
    assert err.value.message == "The shelf camera is offline. Please wait a moment."
    assert store.current_session() is None
    assert events("vision_unavailable")[-1]["vision_age_ms"] == -1

    r = routes_api.store_error_response(err.value)
    assert r.status_code == 503
    assert json.loads(r.body) == {"error": "vision_unavailable",
                                  "message": "The shelf camera is offline. Please wait a moment."}


def test_entry_refused_with_stale_snapshot(member_id, monkeypatch):
    shelf_state.apply_snapshot(snap(FULL))
    monkeypatch.setattr(shelf_state, "_last_snapshot_at", time.monotonic() - 2.5)
    assert shelf_state.last_snapshot_age_ms() > store.VISION_MAX_AGE_MS
    with pytest.raises(store.VisionUnavailable):
        store.start_session(member_id)
    assert store.current_session() is None
    assert events("vision_unavailable")[-1]["vision_age_ms"] >= 2500

    shelf_state.apply_snapshot(snap(FULL, frame_id=2))  # camera back: entry works again
    assert store.start_session(member_id)["state"] == store.IN_STORE
