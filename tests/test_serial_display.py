"""Gate display (DISP) in the serial bridge, with a fake port. No board needed. There are no bay LEDs: a new
plan shows DISP,FIND ("Find bay 2 and 4") instead of HILITE, and nothing here expects LED/SHELF/GATE/HILITE."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from backend import eventlog, intent, serial_bridge, store
from backend.main import app
from backend.settings import settings
from test_cart import FULL, make_member, snap, tmp_data, with_bays  # noqa: F401
from test_live import admin_login, enter_store
from identity_helpers import set_features
from test_serial import HEADERS, Board, legacy, use_board, wait_for


@pytest.fixture
def fast_timers(monkeypatch):
    for name in ("IDLE_AFTER_S", "WELCOME_HOLD_S", "OCCUPIED_HOLD_S", "DECLINED_HOLD_S", "FIND_HOLD_S"):
        monkeypatch.setattr(serial_bridge, name, 0.15)


@pytest.fixture
def shown(monkeypatch):
    """Director unit tests: record display() calls, with a bridge that never connects."""
    calls: list[tuple] = []
    monkeypatch.setattr(serial_bridge, "bridge", serial_bridge.SerialBridge("FAKE", 115200, opener=Board(False)))
    monkeypatch.setattr(serial_bridge, "display", lambda screen, *a: calls.append((screen, *map(str, a))))
    serial_bridge.director.reset()
    return calls


def disp(board: Board) -> list[str]:
    return [c for c in board.port.written if c.startswith("DISP,")]


# --- sanitizing ---

def test_sanitize_strips_commas_newlines_and_caps_length():
    assert serial_bridge.sanitize_arg("Maya, Jr.") == "Maya Jr."
    assert serial_bridge.sanitize_arg("a\nb\r\nc") == "a b c"
    assert serial_bridge.sanitize_arg("x" * 50) == "x" * 20
    assert serial_bridge.sanitize_arg("José") == "Jose"  # the board's font is ASCII
    assert serial_bridge.sanitize_arg("  spaced   out ") == "spaced out"
    assert serial_bridge.sanitize_arg(3) == "3"


def test_display_command_shapes():
    assert serial_bridge.display_command("IDLE") == "DISP,IDLE"
    assert serial_bridge.display_command("total", "$12.96", 3) == "DISP,TOTAL,$12.96,3"
    assert serial_bridge.display_command("WELCOME", "Ann,\nDROP") == "DISP,WELCOME,Ann DROP"
    assert serial_bridge.display_command("FIND", "2 and 4") == "DISP,FIND,2 and 4"
    with pytest.raises(ValueError):
        serial_bridge.display_command("BOGUS")


def test_helpers_are_noops_without_bridge():
    assert serial_bridge.bridge is None
    assert serial_bridge.display("IDLE") is False
    assert serial_bridge.show_find([1, 3]) is False
    serial_bridge._on_event({"type": "session_state", "to": "IN_STORE", "from": None})  # must not raise


def test_bay_cards_are_printed_numbers():
    assert serial_bridge.bay_cards([1, 3]) == "2 and 4"  # bay id 0 is card "1"
    assert serial_bridge.bay_cards([2]) == "3"
    assert serial_bridge.bay_cards([4, 0, 2, 0]) == "1 3 and 5"  # sorted, deduped, no commas
    assert serial_bridge.bay_cards([]) == ""


# --- screen state and resync ---

def test_same_screen_twice_goes_out_once(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app):
        assert wait_for(lambda: serial_bridge.is_connected() and "DISP,IDLE" in board.port.written)
        time.sleep(0.1)  # let the connect-time sync finish
        serial_bridge.display("TOTAL", "$1.00", 1)
        serial_bridge.display("TOTAL", "$1.00", 1)
        time.sleep(0.1)
        assert disp(board).count("DISP,TOTAL,$1.00,1") == 1
        assert legacy(board.port.written) == []


def test_reconnect_and_ready_resend_the_screen(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(with_bays(b1=[])), headers=HEADERS)
        assert wait_for(lambda: serial_bridge.is_connected())
        serial_bridge.display("TOTAL", "$4.32", 1)
        board.unplug()
        assert wait_for(lambda: not serial_bridge.is_connected())
        board.plugged = True
        assert wait_for(lambda: len(board.ports) == 2
                        and board.port.written[:1] == ["DISP,TOTAL,$4.32,1"]), board.port.written
        board.port.written.clear()
        board.port.feed("READY")
        assert wait_for(lambda: board.port.written == ["DISP,TOTAL,$4.32,1"]), board.port.written


# --- event mapping, end to end through the event log ---

def test_session_flow_drives_the_display(tmp_data, monkeypatch, fast_timers):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        enter_store(client)
        assert wait_for(lambda: "DISP,WELCOME,Demo" in disp(board)), disp(board)
        # After the welcome hold, the live total takes over.
        assert wait_for(lambda: disp(board)[-1] == "DISP,TOTAL,$0.00,0"), disp(board)
        client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)  # pick elx
        assert wait_for(lambda: disp(board)[-1] == "DISP,TOTAL,$8.64,1"), disp(board)
        assert client.post("/admin/reset").status_code == 200  # -> CANCELLED
        assert wait_for(lambda: disp(board)[-1] == "DISP,IDLE"), disp(board)
        assert legacy(board.port.written) == []


def test_new_plan_shows_find_then_back_to_total(tmp_data, monkeypatch, fast_timers):
    monkeypatch.setattr(serial_bridge, "FIND_HOLD_S", 0.4)
    set_features(monkeypatch, llm=False)  # the keyword planner: recovery -> bays 0 and 1
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        enter_store(client)  # also signs this client in as the demo shopper
        assert wait_for(lambda: disp(board)[-1] == "DISP,TOTAL,$0.00,0"), disp(board)
        plan = client.post("/api/intent", json={"text": "Post run recovery under $15"}).json()
        cards = serial_bridge.bay_cards(plan["bays"])
        assert plan["bays"] and cards
        assert wait_for(lambda: disp(board)[-1] == f"DISP,FIND,{cards}"), disp(board)
        # A pick during the FIND screen does not cut it short; TOTAL comes back with the latest numbers.
        client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)
        time.sleep(0.1)
        assert disp(board)[-1] == f"DISP,FIND,{cards}"
        assert wait_for(lambda: disp(board)[-1] == "DISP,TOTAL,$8.64,1"), disp(board)
        assert intent.shown_bays() == plan["bays"]
        assert legacy(board.port.written) == []


def test_welcome_hold_shows_latest_cart(tmp_data, monkeypatch, fast_timers):
    monkeypatch.setattr(serial_bridge, "WELCOME_HOLD_S", 0.5)
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        enter_store(client)
        client.post("/internal/shelf", json=snap(with_bays(b2=[5]), frame_id=2), headers=HEADERS)  # pick a bar
        time.sleep(0.1)
        assert disp(board)[-1] == "DISP,WELCOME,Demo"  # the welcome is not cut short
        assert wait_for(lambda: disp(board)[-1] == "DISP,TOTAL,$3.78,1"), disp(board)


def test_occupied_overlay_then_back(tmp_data, monkeypatch, fast_timers):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        enter_store(client)
        assert wait_for(lambda: disp(board)[-1].startswith("DISP,TOTAL"))
        with pytest.raises(store.StoreOccupied):
            store.start_session(make_member("Zed Other"))
        assert wait_for(lambda: disp(board)[-1] == "DISP,OCCUPIED,Demo"), disp(board)
        assert wait_for(lambda: disp(board)[-1] == "DISP,TOTAL,$0.00,0"), disp(board)


# --- event mapping, unit level (payment events are guessed names; see docs/overnight/display.md) ---

def test_payment_authorized_then_closed(shown, fast_timers):
    d = serial_bridge.director
    d.handle({"type": "cart_changed", "state": "CHECKOUT_PENDING", "items": {"elx": 1, "bar": 2}, "total_usd": 16.2})
    assert shown[-1] == ("TOTAL", "$16.20", "3")
    d.handle({"type": "payment", "status": "AUTHORIZED", "amount_usd": 16.2, "auth_code": "A1B2C3"})
    assert shown[-1] == ("PAID", "$16.20", "A1B2C3")
    d.handle({"type": "session_state", "from": "CHECKOUT_PENDING", "to": "PAID"})
    assert shown[-1] == ("PAID", "$16.20", "A1B2C3")  # already showing PAID: not replaced
    d.handle({"type": "session_state", "from": "PAID", "to": "CLOSED"})
    assert wait_for(lambda: shown[-1] == ("IDLE",))


def test_find_overlay_returns_to_total(shown, fast_timers):
    d = serial_bridge.director
    d.handle({"type": "cart_changed", "state": "IN_STORE", "items": {"elx": 1}, "total_usd": 8.64})
    d.find([3, 1])
    assert shown[-1] == ("FIND", "2 and 4")
    d.handle({"type": "cart_changed", "state": "IN_STORE", "items": {"elx": 1, "rec": 1}, "total_usd": 12.96})
    assert shown[-1] == ("FIND", "2 and 4")  # not interrupted
    assert wait_for(lambda: shown[-1] == ("TOTAL", "$12.96", "2"))


def test_find_with_nobody_inside_returns_to_idle(shown, fast_timers):
    d = serial_bridge.director
    d.find([0])
    assert shown[-1] == ("FIND", "1")
    assert wait_for(lambda: shown[-1] == ("IDLE",))
    n = len(shown)
    d.find([])  # an empty plan points nowhere: no screen
    assert len(shown) == n


def test_payment_declined_then_back_to_total(shown, fast_timers):
    d = serial_bridge.director
    d.handle({"type": "cart_changed", "state": "CHECKOUT_PENDING", "items": {"rec": 1}, "total_usd": 4.32})
    d.handle({"type": "payment", "status": "DECLINED", "amount_usd": 4.32, "auth_code": None})
    assert shown[-1] == ("DECLINED",)
    assert wait_for(lambda: shown[-1] == ("TOTAL", "$4.32", "1"))


def test_payment_alternatives_and_fallbacks(shown):
    d = serial_bridge.director
    # A mock-provider approval: the real "payment" event, amount_usd and a MOCK auth code.
    d.handle({"type": "cart_changed", "state": "IN_STORE", "items": {"elx": 1}, "total_usd": 8.64})
    d.handle({"type": "payment", "status": "AUTHORIZED", "amount_usd": 8.64, "auth_code": "MOCK1234"})
    assert shown[-1] == ("PAID", "$8.64", "MOCK1234")
    d.reset()
    d.handle({"type": "cart_changed", "state": "CHECKOUT_PENDING", "items": {"elx": 1}, "total_usd": 8.64})
    d.handle({"type": "session_state", "from": "CHECKOUT_PENDING", "to": "PAID"})  # no payment event at all
    assert shown[-1] == ("PAID", "$8.64", "")
    n = len(shown)
    d.handle({"type": "payment", "status": "ERROR"})
    d.handle({"type": "serial_out", "cmd": "DISP,IDLE"})
    # Names that are not the real payment event are ignored outright.
    d.handle({"type": "payment_authorized", "amount_usd": 8.64, "auth_code": "NOPE12"})
    d.handle({"type": "payment_declined", "amount_usd": 8.64})
    assert len(shown) == n


def test_payment_without_amount_falls_back_to_last_cart_total(shown):
    d = serial_bridge.director
    d.handle({"type": "cart_changed", "state": "CHECKOUT_PENDING", "items": {"elx": 1}, "total_usd": 8.64})
    d.handle({"type": "payment", "status": "AUTHORIZED", "auth_code": "A1B2C3"})
    assert shown[-1] == ("PAID", "$8.64", "A1B2C3")


def test_cancelled_exit_returns_to_total_and_new_screen_cancels_idle_timer(shown, fast_timers):
    d = serial_bridge.director
    d.handle({"type": "cart_changed", "state": "CHECKOUT_PENDING", "items": {"bar": 1}, "total_usd": 3.78})
    d.handle({"type": "session_state", "from": "CHECKOUT_PENDING", "to": "IN_STORE"})
    assert shown[-1] == ("TOTAL", "$3.78", "1")
    d.handle({"type": "session_state", "from": "IN_STORE", "to": "CANCELLED"})
    d.handle({"type": "cart_changed", "state": "IN_STORE", "items": {}, "total_usd": 0})  # a new shopper
    time.sleep(0.4)
    assert shown[-1] == ("TOTAL", "$0.00", "0")  # the stale IDLE timer never fired


def test_hardware_leds_off_display_is_inert(tmp_data, monkeypatch):
    off = dataclasses.replace(settings, features=dataclasses.replace(settings.features, hardware_leds=False))
    monkeypatch.setattr("backend.settings.settings", off)
    with TestClient(app) as client:
        assert serial_bridge.bridge is None
        enter_store(client)
        eventlog.log("payment", status="AUTHORIZED", amount_usd=1, auth_code="X")
        assert serial_bridge.display("IDLE") is False and serial_bridge.show_find([0]) is False
        assert serial_bridge.director.screen == ("IDLE",)
        assert client.post("/admin/reset").status_code == 200
