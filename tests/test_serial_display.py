"""Gate display (DISP) and bay highlight (HILITE) in the serial bridge, with a fake port. No board needed."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from backend import eventlog, serial_bridge, store
from backend.main import app
from backend.settings import settings
from test_cart import FULL, make_member, snap, tmp_data, with_bays  # noqa: F401
from test_live import admin_login, enter_store
from test_serial import HEADERS, Board, use_board, wait_for


@pytest.fixture
def fast_timers(monkeypatch):
    for name in ("IDLE_AFTER_S", "WELCOME_HOLD_S", "OCCUPIED_HOLD_S", "DECLINED_HOLD_S"):
        monkeypatch.setattr(serial_bridge, name, 0.15)


@pytest.fixture
def shown(monkeypatch):
    """Director unit tests: record display()/clear_highlights() calls, with a bridge that never connects."""
    calls: list[tuple] = []
    monkeypatch.setattr(serial_bridge, "bridge", serial_bridge.SerialBridge("FAKE", 115200, opener=Board(False)))
    monkeypatch.setattr(serial_bridge, "display", lambda screen, *a: calls.append((screen, *map(str, a))))
    monkeypatch.setattr(serial_bridge, "clear_highlights", lambda: calls.append(("HILITE_CLEAR",)))
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
    with pytest.raises(ValueError):
        serial_bridge.display_command("BOGUS")


def test_helpers_are_noops_without_bridge():
    assert serial_bridge.bridge is None
    assert serial_bridge.display("IDLE") is False
    assert serial_bridge.highlight(0, True) is False
    assert serial_bridge.clear_highlights() is False
    serial_bridge._on_event({"type": "session_state", "to": "IN_STORE", "from": None})  # must not raise


# --- highlight state and resync ---

def test_highlight_state_commands_and_dedup(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app):
        assert wait_for(lambda: serial_bridge.is_connected() and "HILITE,ALL,OFF" in board.port.written)
        time.sleep(0.1)  # let the connect-time sync finish
        assert serial_bridge.highlight(2, True) and serial_bridge.highlight(0, True)
        assert serial_bridge.bridge.highlights == {0, 2}
        serial_bridge.highlight(2, False)
        assert serial_bridge.bridge.highlights == {0}
        assert serial_bridge.clear_highlights()
        assert serial_bridge.bridge.highlights == set()
        assert wait_for(lambda: board.port.written[-4:] == ["HILITE,2,ON", "HILITE,0,ON", "HILITE,2,OFF",
                                                             "HILITE,ALL,OFF"]), board.port.written
        # The same screen twice goes out once.
        serial_bridge.display("TOTAL", "$1.00", 1)
        serial_bridge.display("TOTAL", "$1.00", 1)
        time.sleep(0.1)
        assert disp(board).count("DISP,TOTAL,$1.00,1") == 1


def test_reconnect_and_ready_resend_screen_and_highlights(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(with_bays(b1=[])), headers=HEADERS)
        assert wait_for(lambda: serial_bridge.is_connected())
        serial_bridge.display("TOTAL", "$4.32", 1)
        serial_bridge.highlight(2, True)
        board.unplug()
        assert wait_for(lambda: not serial_bridge.is_connected())
        board.plugged = True
        expected = ["LED,0,ON", "LED,1,OFF", "LED,2,ON", "DISP,TOTAL,$4.32,1", "HILITE,ALL,OFF", "HILITE,2,ON"]
        assert wait_for(lambda: len(board.ports) == 2 and board.port.written[:6] == expected), board.port.written
        board.port.written.clear()
        board.port.feed("READY")
        assert wait_for(lambda: board.port.written == expected), board.port.written


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
        serial_bridge.highlight(1, True)
        assert client.post("/admin/reset").status_code == 200  # -> CANCELLED
        assert wait_for(lambda: "HILITE,ALL,OFF" in board.port.written[-3:])
        assert serial_bridge.bridge.highlights == set()
        assert wait_for(lambda: disp(board)[-1] == "DISP,IDLE"), disp(board)


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
    assert shown[-1] == ("HILITE_CLEAR",)
    assert wait_for(lambda: shown[-1] == ("IDLE",))


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
        assert serial_bridge.display("IDLE") is False and serial_bridge.highlight(0, True) is False
        assert serial_bridge.director.screen == ("IDLE",)
        assert client.post("/admin/reset").status_code == 200
