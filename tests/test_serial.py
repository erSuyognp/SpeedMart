"""S1.4 tests: serial bridge with a fake port. No board needed."""

from __future__ import annotations

import dataclasses
import threading
import time

import serial
from fastapi.testclient import TestClient

from backend import serial_bridge, shelf_state, store
from backend.main import app
from backend.settings import settings
from test_cart import FULL, TOKEN, events, snap, tmp_data, with_bays  # noqa: F401
from test_live import admin_login, enter_store

HEADERS = {"X-Internal-Token": TOKEN}


class FakeSerial:
    """Minimal pyserial stand-in: records writes, feeds queued lines, can be 'unplugged'."""

    def __init__(self):
        self.written: list[str] = []
        self._incoming = b""
        self._lock = threading.Lock()
        self.unplugged = False
        self.closed = False

    def feed(self, line: str) -> None:
        with self._lock:
            self._incoming += (line + "\n").encode()

    @property
    def in_waiting(self) -> int:
        if self.unplugged:
            raise serial.SerialException("device disconnected")
        with self._lock:
            return len(self._incoming)

    def read(self, n: int = 1) -> bytes:
        if self.unplugged:
            raise serial.SerialException("device disconnected")
        with self._lock:
            out, self._incoming = self._incoming[:n], self._incoming[n:]
        if not out:
            time.sleep(0.01)
        return out

    def write(self, data: bytes) -> int:
        if self.unplugged:
            raise serial.SerialException("write failed")
        self.written.extend(data.decode().splitlines())
        return len(data)

    def close(self) -> None:
        self.closed = True


class Board:
    """Opener that hands out a FakeSerial while 'plugged in' and fails otherwise."""

    def __init__(self, plugged: bool = True):
        self.plugged = plugged
        self.ports: list[FakeSerial] = []

    def __call__(self, port, baud, **kwargs):
        if not self.plugged:
            raise serial.SerialException(f"could not open port {port}")
        self.ports.append(FakeSerial())
        return self.ports[-1]

    @property
    def port(self) -> FakeSerial:
        return self.ports[-1]

    def unplug(self) -> None:
        self.plugged = False
        self.port.unplugged = True


def wait_for(cond, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def use_board(monkeypatch, board: Board, retry_s: float = 0.05) -> None:
    monkeypatch.setattr(serial_bridge, "open_port", board)
    monkeypatch.setattr(serial_bridge, "RETRY_S", retry_s)


def test_missing_board_never_crashes_and_reports_false(tmp_data):
    with TestClient(app) as client:
        assert wait_for(lambda: events("serial_unavailable"))
        r = client.get("/api/health")
        assert r.status_code == 200 and r.json()["serial"] is False
        admin_login(client)
        assert client.get("/admin/state").json()["health"]["serial"] is False
        # /admin/led still answers: queued, board just is not there.
        r = client.post("/admin/led", json={"cmd": "LED,0,OFF"})
        assert r.status_code == 200 and r.json() == {"ok": True, "connected": False}


def test_connect_health_and_admin_led(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        assert client.get("/api/health").json()["serial"] is True
        admin_login(client)
        assert client.get("/admin/state").json()["health"]["serial"] is True
        # On connect the bridge syncs every bay LED (shelf empty so far).
        assert wait_for(lambda: board.port.written[:3] == ["LED,0,OFF", "LED,1,OFF", "LED,2,OFF"])
        assert client.post("/admin/led", json={"cmd": "SHELF,GREEN"}).json()["connected"] is True
        assert wait_for(lambda: "SHELF,GREEN" in board.port.written)
        r = client.post("/admin/led", json={"cmd": "LED,0,OFF\nPING"})
        assert r.status_code == 400 and r.json()["error"] == "bad_command"


def test_unplug_flips_serial_false_then_replug_recovers(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        board.unplug()
        assert wait_for(lambda: client.get("/api/health").json()["serial"] is False)
        assert events("serial_disconnected")
        board.plugged = True
        assert wait_for(lambda: client.get("/api/health").json()["serial"] is True)
        assert len(board.ports) == 2


def test_shelf_change_drives_bay_leds(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
        assert wait_for(lambda: board.port.written[-3:] == ["LED,0,ON", "LED,1,ON", "LED,2,ON"])
        client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)
        assert wait_for(lambda: board.port.written[-3:] == ["LED,0,ON", "LED,1,ON", "LED,2,ON"])
        client.post("/internal/shelf", json=snap(with_bays(b0=[]), frame_id=3), headers=HEADERS)
        assert wait_for(lambda: board.port.written[-3:] == ["LED,0,OFF", "LED,1,ON", "LED,2,ON"])
        client.post("/internal/shelf", json=snap(FULL, frame_id=4), headers=HEADERS)
        assert wait_for(lambda: board.port.written[-3:] == ["LED,0,ON", "LED,1,ON", "LED,2,ON"])
        # An unstable empty bay keeps its last stable contents, so its LED stays on.
        n = len(board.port.written)
        client.post("/internal/shelf", json=snap(with_bays(b0=[]), unstable=(0,), frame_id=5), headers=HEADERS)
        time.sleep(0.1)
        assert len(board.port.written) == n
        assert shelf_state.bay_occupancy() == {0: True, 1: True, 2: True}


def test_incoming_lines_logged_and_button_resets(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        started = enter_store(client)
        assert wait_for(lambda: serial_bridge.is_connected())
        board.port.feed("PONG")
        assert wait_for(lambda: any(e["line"] == "PONG" for e in events("serial_in")))
        board.port.feed("BTN,0")
        assert wait_for(lambda: events("admin_reset"))
        assert store.current_session() is None
        reset = events("admin_reset")[-1]
        assert reset["source"] == "button" and reset["cancelled_session_id"] == started["session"]["id"]
        assert wait_for(lambda: "SHELF,IDLE" in board.port.written and "GATE,IDLE" in board.port.written)


def test_ready_resyncs_bay_leds(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(with_bays(b2=[])), headers=HEADERS)
        expected = ["LED,0,ON", "LED,1,ON", "LED,2,OFF"]
        assert wait_for(lambda: serial_bridge.is_connected() and board.port.written[-3:] == expected)
        time.sleep(0.1)  # let the connect-time sync finish
        board.port.written.clear()
        board.port.feed("READY")
        assert wait_for(lambda: board.port.written == expected), board.port.written


def test_timed_effect_does_not_block_and_latest_wins(monkeypatch):
    bridge = serial_bridge.SerialBridge("FAKE", 115200, opener=Board())
    sent: list[str] = []
    monkeypatch.setattr(bridge, "send", sent.append)
    t0 = time.monotonic()
    bridge.send_timed("SHELF,GREEN", "SHELF,IDLE", 0.2)
    assert time.monotonic() - t0 < 0.05 and sent == ["SHELF,GREEN"]
    bridge.send_timed("SHELF,RED", "SHELF,IDLE", 0.4)  # replaces the pending reset
    time.sleep(0.3)
    assert sent == ["SHELF,GREEN", "SHELF,RED"]
    assert wait_for(lambda: sent == ["SHELF,GREEN", "SHELF,RED", "SHELF,IDLE"])
    time.sleep(0.1)
    assert sent.count("SHELF,IDLE") == 1


def test_hardware_leds_off_means_no_bridge(tmp_data, monkeypatch):
    off = dataclasses.replace(settings, features=dataclasses.replace(settings.features, hardware_leds=False))
    monkeypatch.setattr("backend.settings.settings", off)
    opened: list[str] = []
    monkeypatch.setattr(serial_bridge, "open_port", lambda *a, **k: opened.append(a[0]))
    with TestClient(app) as client:
        time.sleep(0.1)
        assert serial_bridge.bridge is None and opened == []
        assert client.get("/api/health").json()["serial"] is False
        admin_login(client)
        r = client.post("/admin/led", json={"cmd": "LED,0,OFF"})
        assert r.status_code == 409 and r.json()["error"] == "hardware_leds_off"
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)  # shelf changes still fine
        assert client.post("/admin/reset").status_code == 200


def test_list_ports_runs():
    assert isinstance(serial_bridge.list_serial_ports(), list)
