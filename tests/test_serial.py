"""S1.4 tests: serial bridge with a fake port. No board needed. The build has no bay or status LEDs: the
bridge only ever drives the gate screen (DISP), so no test here expects LED, SHELF, GATE or HILITE."""

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
from identity_helpers import set_features
from test_live import admin_login, enter_store

LEGACY = ("LED", "SHELF", "GATE", "HILITE")


def legacy(written: list[str]) -> list[str]:
    """Commands for the LEDs that no longer exist. The backend must never send any."""
    return [c for c in written if c.split(",", 1)[0] in LEGACY]

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
        r = client.post("/admin/led", json={"cmd": "DISP,IDLE"})
        assert r.status_code == 200 and r.json() == {"ok": True, "connected": False}


def test_connect_health_and_admin_led(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        assert client.get("/api/health").json()["serial"] is True
        admin_login(client)
        assert client.get("/admin/state").json()["health"]["serial"] is True
        # On connect the bridge syncs the gate screen, and nothing else.
        assert wait_for(lambda: board.port.written[:1] == ["DISP,IDLE"])
        assert client.post("/admin/led", json={"cmd": "DISP,FIND,2 and 4"}).json()["connected"] is True
        assert wait_for(lambda: "DISP,FIND,2 and 4" in board.port.written)
        assert legacy(board.port.written) == []
        r = client.post("/admin/led", json={"cmd": "DISP,IDLE\nPING"})
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


def test_shopping_sends_no_led_commands(tmp_data, monkeypatch):
    """Shelf changes, entry, a pick, checkout, payment and reset: only DISP lines reach the board."""
    set_features(monkeypatch, gates=False, passkeys=False, stripe=False)  # approve without Face ID
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        assert wait_for(lambda: serial_bridge.is_connected())
        enter_store(client)
        client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)  # pick elx
        client.post("/internal/shelf", json=snap(with_bays(b0=[]), frame_id=3), headers=HEADERS)
        assert client.post("/api/dev/checkout").status_code == 200
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "AUTHORIZED"
        assert wait_for(lambda: any(c.startswith("DISP,PAID") for c in board.port.written)), board.port.written
        assert client.post("/admin/reset").status_code == 200
        time.sleep(0.2)
        assert legacy(board.port.written) == [], board.port.written
        assert all(c.startswith("DISP,") for c in board.port.written), board.port.written


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
        time.sleep(0.1)
        assert legacy(board.port.written) == []


def test_ready_resyncs_the_screen(tmp_data, monkeypatch):
    board = Board()
    use_board(monkeypatch, board)
    with TestClient(app) as client:
        client.post("/internal/shelf", json=snap(with_bays(b2=[])), headers=HEADERS)
        assert wait_for(lambda: serial_bridge.is_connected() and "DISP,IDLE" in board.port.written)
        time.sleep(0.1)  # let the connect-time sync finish
        board.port.written.clear()
        board.port.feed("READY")
        # Resync is the current screen only.
        assert wait_for(lambda: board.port.written == ["DISP,IDLE"]), board.port.written


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
        r = client.post("/admin/led", json={"cmd": "DISP,IDLE"})
        assert r.status_code == 409 and r.json()["error"] == "hardware_leds_off"
        client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)  # shelf changes still fine
        assert client.post("/admin/reset").status_code == 200


def test_list_ports_runs():
    assert isinstance(serial_bridge.list_serial_ports(), list)
