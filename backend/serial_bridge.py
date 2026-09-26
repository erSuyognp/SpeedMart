"""ESP32 serial read/write thread (F12, 9.7).

One background thread owns the port: it opens serial.port, writes queued commands, reads incoming lines,
and on any failure closes the port and retries every 3 s. Nothing here ever raises into the backend, so a
missing or unplugged board only shows up as serial:false in /api/health. Runs only when hardware_leds is on.

The board's LCD is the store's gate display (DISP,<screen>,...) and bay LEDs can blink for the intent feature
(HILITE,<bay>,ON|OFF). The bridge remembers the current screen and highlights and resends them, with the bay
LEDs, on every (re)connect and READY. DisplayDirector maps backend.eventlog events to screens, so no other
module has to call the display; docs/overnight/display.md lists the exact mapping.

    python -m backend.serial_bridge --list-ports     print serial ports, to set config.json serial.port
"""

from __future__ import annotations

import queue
import threading
import unicodedata
from collections.abc import Callable
from typing import Any

import serial
from serial.tools import list_ports

RETRY_S = 3.0
READ_TIMEOUT_S = 0.1

DISP_SCREENS = ("IDLE", "WELCOME", "TOTAL", "PAID", "DECLINED", "OCCUPIED")
DISP_ARG_MAX = 20
IDLE_AFTER_S = 3.0       # CLOSED / CANCELLED -> IDLE after this long
WELCOME_HOLD_S = 3.0     # WELCOME stays up this long before the live TOTAL takes over
OCCUPIED_HOLD_S = 3.0    # "<name> is shopping" overlay, then back to the shopper's screen
DECLINED_HOLD_S = 4.0    # DECLINED overlay, then back to TOTAL (the session stays CHECKOUT_PENDING, 7.1)


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
        self._state_lock = threading.Lock()
        self._display_cmd = "DISP,IDLE"  # the board boots into IDLE
        self._highlights: set[int] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def display_cmd(self) -> str:
        return self._display_cmd

    @property
    def highlights(self) -> set[int]:
        with self._state_lock:
            return set(self._highlights)

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

    def set_display(self, cmd: str) -> None:
        """Show a DISP command. The same screen again is not resent (the board would ignore it anyway)."""
        with self._state_lock:
            if cmd == self._display_cmd:
                return
            self._display_cmd = cmd
        self.send(cmd)

    def set_highlight(self, bay: int, on: bool) -> None:
        with self._state_lock:
            if on:
                self._highlights.add(bay)
            else:
                self._highlights.discard(bay)
        self.send(f"HILITE,{bay},{'ON' if on else 'OFF'}")

    def clear_highlights(self) -> None:
        with self._state_lock:
            self._highlights.clear()
        self.send("HILITE,ALL,OFF")

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
        if line == "READY":  # board rebooted: every bay lit, screen IDLE, no highlights
            self.send_current_state()
        elif line == "BTN,0":
            from backend import admin  # lazy: admin imports this module

            try:
                admin.do_reset(source="button")
            except Exception as e:
                _log("serial_button_error", error=repr(e))

    def send_led_state(self) -> None:
        for cmd in bay_led_commands():
            self.send(cmd)

    def send_current_state(self) -> None:
        """Full resync after connect or READY: bay LEDs, the current screen, then highlights."""
        self.send_led_state()
        with self._state_lock:
            display_cmd, highlights = self._display_cmd, sorted(self._highlights)
        self.send(display_cmd)
        self.send("HILITE,ALL,OFF")
        for bay in highlights:
            self.send(f"HILITE,{bay},ON")


def _drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


# --- module API used by the rest of the backend. All are no-ops while hardware_leds is off. ---

bridge: SerialBridge | None = None


def start() -> None:
    global bridge, _subscribed
    from backend.settings import settings

    if not settings.features.hardware_leds or bridge is not None:
        return
    bridge = SerialBridge(str(settings.serial["port"]), int(settings.serial["baud"]))
    if not _subscribed:
        from backend import eventlog

        eventlog.subscribe(_on_event)
        _subscribed = True
    director.prime()  # backend restarted mid-session: show that session's cart, not IDLE
    bridge.start()


def stop() -> None:
    global bridge
    director.reset()
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
        bridge.send_led_state()


def sanitize_arg(value: Any) -> str:
    """One DISP argument: ASCII only (the board's font), no commas or newlines, at most DISP_ARG_MAX chars."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = text.replace(",", "").replace("\r", " ").replace("\n", " ")
    text = "".join(ch for ch in text if ch.isprintable())
    return " ".join(text.split())[:DISP_ARG_MAX].strip()


def display_command(screen: str, *args: Any) -> str:
    screen = screen.strip().upper()
    if screen not in DISP_SCREENS:
        raise ValueError(f"unknown display screen {screen!r}")
    return ",".join(["DISP", screen, *(sanitize_arg(a) for a in args)])


def display(screen: str, *args: Any) -> bool:
    """Show a screen on the board's LCD, e.g. display("TOTAL", "$12.96", 3). False while hardware_leds is off."""
    if bridge is None:
        return False
    bridge.set_display(display_command(screen, *args))
    return True


def highlight(bay: int, on: bool) -> bool:
    """Blink bay `bay`'s LED at 2 Hz (on), or return it to its LED,<bay> state (off)."""
    if bridge is None:
        return False
    bridge.set_highlight(int(bay), bool(on))
    return True


def clear_highlights() -> bool:
    if bridge is None:
        return False
    bridge.clear_highlights()
    return True


# --- event log -> gate display. Event names below are the real ones emitted by the backend:
# backend/payments.py logs "payment" (status AUTHORIZED / DECLINED / ERROR, amount_usd, auth_code),
# backend/store.py logs "cart_changed", "store_occupied" and "session_state". ---

PAYMENT_EVENTS = ("payment",)


def _query_one(sql: str, arg: Any) -> Any:
    from backend import db

    try:
        conn = db.connect()
        try:
            return conn.execute(sql, (arg,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None


def _first_name(member_id: Any) -> str:
    row = _query_one("SELECT name FROM members WHERE id = ?", member_id)
    return ((row["name"] if row else "") or "").strip().split(" ")[0] or "Shopper"


def _occupant_first_name(session_id: Any) -> str:
    row = _query_one("SELECT member_id FROM store_sessions WHERE id = ?", session_id)
    return _first_name(row["member_id"]) if row else "Someone"


def _money(value: Any) -> str | None:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return None


class DisplayDirector:
    """Turns event-log entries into DISP screens. Timed steps (WELCOME -> TOTAL, overlays, -> IDLE) run on
    daemon timers; any newer screen cancels a pending step, so a stale timer never overwrites it."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._screen: tuple[str, ...] = ("IDLE",)
        self._total = "$0.00"
        self._count = 0
        self._gen = 0
        self._timer: threading.Timer | None = None

    @property
    def screen(self) -> tuple[str, ...]:
        return self._screen

    def reset(self) -> None:
        with self._lock:
            self._cancel()
            self._screen = ("IDLE",)
            self._total, self._count = "$0.00", 0

    def _cancel(self) -> None:
        self._gen += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _show(self, screen: str, *args: Any) -> None:
        self._cancel()
        self._screen = (screen, *(str(a) for a in args))
        display(screen, *args)

    def _later(self, seconds: float, fn: Callable[[], None]) -> None:
        gen = self._gen

        def fire() -> None:
            with self._lock:
                if self._gen == gen:
                    fn()

        self._timer = threading.Timer(seconds, fire)
        self._timer.daemon = True
        self._timer.start()

    def _show_total(self) -> None:
        self._show("TOTAL", self._total, self._count)

    def _set_cart(self, entry: dict[str, Any]) -> None:
        items = entry.get("items") or {}
        if isinstance(items, dict):  # cart_changed event: {sku: qty}
            self._count = sum(int(q) for q in items.values())
        else:  # CartSnapshot: [{"sku":..., "qty":...}]
            self._count = sum(int(i.get("qty", 0)) for i in items)
        self._total = _money(entry.get("total_usd")) or self._total

    def prime(self) -> None:
        """At startup: if a session is already active, show its cart instead of IDLE."""
        try:
            from backend import store

            session = store.current_session()
            snapshot = store.cart_for(session) if session else None
        except Exception:
            return
        if snapshot:
            with self._lock:
                self._set_cart(snapshot)
                self._show_total()

    def handle(self, entry: dict[str, Any]) -> None:
        kind = entry.get("type")
        with self._lock:
            if kind == "session_state":
                self._on_session(entry)
            elif kind == "cart_changed":
                self._on_cart(entry)
            elif kind == "store_occupied":
                self._on_occupied(entry)
            elif kind in PAYMENT_EVENTS:
                self._on_payment(entry)

    def _on_session(self, e: dict[str, Any]) -> None:
        to, frm = e.get("to"), e.get("from")
        if to == "IN_STORE" and not frm:
            self._total, self._count = "$0.00", 0
            self._show("WELCOME", _first_name(e.get("member_id")))
            self._later(WELCOME_HOLD_S, self._show_total)
        elif to == "IN_STORE":  # exit cancelled, back to shopping
            self._show_total()
        elif to == "PAID":
            if self._screen[0] != "PAID":  # no payment event seen (yet): still say approved
                self._show("PAID", self._total, "")
        elif to in ("CLOSED", "CANCELLED"):
            clear_highlights()
            self._cancel()
            self._later(IDLE_AFTER_S, lambda: self._show("IDLE"))

    def _on_cart(self, e: dict[str, Any]) -> None:
        self._set_cart(e)
        if e.get("state") not in ("IN_STORE", "CHECKOUT_PENDING"):
            return
        if self._screen[0] in ("WELCOME", "OCCUPIED", "DECLINED", "PAID"):
            return  # a timed screen is up; it hands over to TOTAL with the latest numbers
        self._show_total()

    def _on_occupied(self, e: dict[str, Any]) -> None:
        name = _occupant_first_name(e.get("occupant_session_id"))
        back = self._screen
        self._show("OCCUPIED", name)
        if back[0] in ("TOTAL", "WELCOME", "OCCUPIED"):
            self._later(OCCUPIED_HOLD_S, self._show_total)
        else:
            self._later(OCCUPIED_HOLD_S, lambda: self._show(*back))

    def _on_payment(self, e: dict[str, Any]) -> None:
        # "payment" carries status, amount_usd and auth_code (payments.py record_payment).
        status = str(e.get("status") or "").upper()
        if status == "AUTHORIZED":
            total = _money(e.get("amount_usd")) or self._total
            self._show("PAID", total, e.get("auth_code") or "")
        elif status == "DECLINED":
            self._show("DECLINED")
            self._later(DECLINED_HOLD_S, self._show_total)


director = DisplayDirector()
_subscribed = False


def _on_event(entry: dict[str, Any]) -> None:
    """eventlog subscriber. Never raises; at most one small SQLite read."""
    if bridge is None:
        return
    try:
        director.handle(entry)
    except Exception:
        pass


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
