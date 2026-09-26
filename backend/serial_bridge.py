"""ESP32 serial read/write thread (F12, 9.7).

One background thread owns the port: it opens serial.port, writes queued commands, reads incoming lines,
and on any failure closes the port and retries every 3 s. Nothing here ever raises into the backend, so a
missing or unplugged board only shows up as serial:false in /api/health. Runs only when hardware_leds is on.

    python -m backend.serial_bridge --list-ports     print serial ports, to set config.json serial.port
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

import serial
from serial.tools import list_ports

RETRY_S = 3.0
READ_TIMEOUT_S = 0.1


def open_port(port: str, baud: int, **kwargs: Any) -> Any:
    """Real port. Tests replace this so no board is needed."""
    return serial.Serial(port, baud, **kwargs)


def _log(event_type: str, **fields: Any) -> None:
    from backend import eventlog  # lazy: --list-ports must work without .env

    eventlog.log(event_type, **fields)


class SerialBridge:
    def __init__(self, port: str, baud: int, opener: Callable[..., Any] | None = None):
        self.port = port
        self.baud = baud
        self.opener = opener  # None: module-level open_port, looked up on every attempt
        self._out: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._timers: dict[str, threading.Timer] = {}
        self._timers_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="serial-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._timers_lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._connected = False

    def send(self, cmd: str) -> None:
        """Queue one command (no newline). Never blocks; dropped on reconnect if the board was away."""
        self._out.put(cmd.strip())

    def send_timed(self, cmd: str, reset_cmd: str, seconds: float) -> None:
        """Send cmd now and reset_cmd after `seconds`, without blocking. A newer timed effect on the same
        target (the part before the first comma, e.g. SHELF) replaces a pending reset."""
        key = cmd.split(",", 1)[0]
        timer = threading.Timer(seconds, self._fire_reset, (key, reset_cmd))
        timer.daemon = True
        with self._timers_lock:
            old = self._timers.pop(key, None)
            if old is not None:
                old.cancel()
            self._timers[key] = timer
        self.send(cmd)
        timer.start()

    def _fire_reset(self, key: str, reset_cmd: str) -> None:
        with self._timers_lock:
            if self._timers.get(key) is not threading.current_thread():
                return  # replaced by a newer effect
            del self._timers[key]
        self.send(reset_cmd)

    # --- thread ---

    def _run(self) -> None:
        announced_down = False
        while not self._stop.is_set():
            try:
                conn = (self.opener or open_port)(self.port, self.baud, timeout=READ_TIMEOUT_S, write_timeout=1)
            except Exception as e:  # missing board, busy port, bad name: retry later
                if not announced_down:
                    _log("serial_unavailable", port=self.port, error=str(e))
                    announced_down = True
                self._stop.wait(RETRY_S)
                continue
            announced_down = False
            self._serve(conn)

    def _serve(self, conn: Any) -> None:
        _drain(self._out)  # commands queued while unplugged are stale; resync instead
        self._connected = True
        _log("serial_connected", port=self.port)
        self.send_current_state()
        buf = b""
        try:
            while not self._stop.is_set():
                while True:
                    try:
                        cmd = self._out.get_nowait()
                    except queue.Empty:
                        break
                    conn.write((cmd + "\n").encode("ascii", errors="replace"))
                    _log("serial_out", cmd=cmd)
                buf += conn.read(conn.in_waiting or 1)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("ascii", errors="replace").strip()
                    if line:
                        self._handle_line(line)
        except Exception as e:  # unplugged mid-use: SerialException / OSError
            _log("serial_disconnected", port=self.port, error=str(e))
        finally:
            self._connected = False
            try:
                conn.close()
            except Exception:
                pass
        if not self._stop.is_set():
            self._stop.wait(RETRY_S)

    def _handle_line(self, line: str) -> None:
        _log("serial_in", line=line)
        if line == "READY":  # board rebooted: it starts with every bay lit
            self.send_current_state()
        elif line == "BTN,0":
            from backend import admin  # lazy: admin imports this module

            try:
                admin.do_reset(source="button")
            except Exception as e:
                _log("serial_button_error", error=repr(e))

    def send_current_state(self) -> None:
        for cmd in bay_led_commands():
            self.send(cmd)


def _drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


# --- module API used by the rest of the backend. All are no-ops while hardware_leds is off. ---

bridge: SerialBridge | None = None


def start() -> None:
    global bridge
    from backend.settings import settings

    if not settings.features.hardware_leds or bridge is not None:
        return
    bridge = SerialBridge(str(settings.serial["port"]), int(settings.serial["baud"]))
    bridge.start()


def stop() -> None:
    global bridge
    if bridge is not None:
        bridge.stop()
        bridge = None


def is_connected() -> bool:
    return bridge is not None and bridge.connected


def send(cmd: str) -> bool:
    """Queue a raw command. Returns False when the bridge is not running (hardware_leds off)."""
    if bridge is None:
        return False
    bridge.send(cmd)
    return True


def send_timed(cmd: str, reset_cmd: str, seconds: float) -> bool:
    """E.g. send_timed("SHELF,GREEN", "SHELF,IDLE", 3)."""
    if bridge is None:
        return False
    bridge.send_timed(cmd, reset_cmd, seconds)
    return True


def bay_led_commands() -> list[str]:
    """LED,<bay>,ON for every bay holding at least one unit, else LED,<bay>,OFF."""
    from backend import shelf_state

    return [f"LED,{bay_id},{'ON' if occupied else 'OFF'}"
            for bay_id, occupied in sorted(shelf_state.bay_occupancy().items())]


def send_bay_leds() -> None:
    """Called after every shelf change."""
    if bridge is not None:
        bridge.send_current_state()


def list_serial_ports() -> list[str]:
    return [f"{p.device}  {p.description}" for p in sorted(list_ports.comports(), key=lambda p: p.device)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SpeedMart serial bridge helpers.")
    parser.add_argument("--list-ports", action="store_true", help="print available serial ports")
    args = parser.parse_args()
    if args.list_ports:
        ports = list_serial_ports()
        print("\n".join(ports) if ports else "No serial ports found. Is the board plugged in with a data cable?")
    else:
        parser.print_help()
