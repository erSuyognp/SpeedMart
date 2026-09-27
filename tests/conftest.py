"""Shared test setup: a fixed test config, no real serial port, and no wait without a timeout."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# The suite always runs against tests/fixtures/config.json (a copy of the committed defaults), never the local
# config.json: a YOLO-only local file makes tag snapshots move nothing, so tests would wait for pushes that never
# come. backend.settings reads SPEEDMART_CONFIG once at import, so this must come before any backend import.
TEST_CONFIG = Path(__file__).resolve().parent / "fixtures" / "config.json"
os.environ["SPEEDMART_CONFIG"] = str(TEST_CONFIG)
# Same values as test_cart.py; must be set before backend.settings is imported.
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["INTERNAL_TOKEN"] = "test-internal-token"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
# Never pick up the real .env: TestClient talks plain http to "testserver" (secure cookies would be dropped),
# and tests must never reach Stripe or an LLM with real keys.
os.environ["PUBLIC_ORIGIN"] = "http://testserver"
os.environ["RP_ID"] = "testserver"
os.environ["RP_NAME"] = "SpeedMart"
os.environ["ENTRY_GATE_TOKEN"] = "test-entry-token"
os.environ["EXIT_GATE_TOKEN"] = "test-exit-token"
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["ELEVENLABS_API_KEY"] = ""
os.environ["ELEVENLABS_AGENT_ID"] = ""
os.environ["ELEVENLABS_VOICE_ID"] = ""
os.environ["KIOSK_TOKEN"] = ""

import anyio  # noqa: E402
import pytest  # noqa: E402
import serial  # noqa: E402
from starlette.testclient import WebSocketTestSession  # noqa: E402

from backend import serial_bridge  # noqa: E402
from backend.settings import CONFIG_PATH  # noqa: E402

assert CONFIG_PATH == TEST_CONFIG, f"backend.settings loaded {CONFIG_PATH} instead of the test config {TEST_CONFIG}"


# --- no wait without a timeout ---
# Starlette's WebSocketTestSession.receive() blocks until the app sends something, so a test expecting a push that
# never comes (a cart that did not change) would hang the whole run. Every receive in the suite gets this limit
# instead; pytest-timeout (pytest.ini, 30 s per test) is the backstop for everything else.
WS_RECEIVE_TIMEOUT_S = 10.0


async def _receive_or_timeout(rx, seconds: float):
    with anyio.fail_after(seconds):
        return await rx.receive()


def _timed_receive(self: WebSocketTestSession):
    try:  # self._send_rx / self.portal: Starlette 1.7 internals behind receive()
        return self.portal.call(_receive_or_timeout, self._send_rx, WS_RECEIVE_TIMEOUT_S)
    except TimeoutError:
        raise AssertionError(f"no WebSocket message from the backend within {WS_RECEIVE_TIMEOUT_S:.0f} s "
                             "(the test waited for a push that never came)") from None


WebSocketTestSession.receive = _timed_receive  # covers the connect handshake as well as receive_json()


def _no_board(port, baud, **kwargs):
    raise serial.SerialException(f"could not open port {port}: no board in tests")


@pytest.fixture(autouse=True)
def quiet_kiosk_narrator():
    """The kiosk narrator's thread would keep working (and logging) after a test's temp data dir is gone. Tests
    that want its output drive it with kiosk_agent.narrator.run_due(...)."""
    from backend import kiosk_agent

    kiosk_agent.narrator.threaded = False
    kiosk_agent.narrator.visit_ended()
    yield
    kiosk_agent.narrator.visit_ended()


@pytest.fixture(autouse=True)
def no_serial_board(monkeypatch):
    """The app's lifespan starts the bridge when hardware_leds is on; make every open fail like a missing board."""
    monkeypatch.setattr(serial_bridge, "open_port", _no_board)
    yield
    serial_bridge.stop()
