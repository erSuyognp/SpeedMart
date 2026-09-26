"""Shared test setup: no test ever touches a real serial port."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Same values as test_cart.py; must be set before backend.settings is imported.
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["INTERNAL_TOKEN"] = "test-internal-token"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"

import pytest  # noqa: E402
import serial  # noqa: E402

from backend import serial_bridge  # noqa: E402


def _no_board(port, baud, **kwargs):
    raise serial.SerialException(f"could not open port {port}: no board in tests")


@pytest.fixture(autouse=True)
def no_serial_board(monkeypatch):
    """The app's lifespan starts the bridge when hardware_leds is on; make every open fail like a missing board."""
    monkeypatch.setattr(serial_bridge, "open_port", _no_board)
    yield
    serial_bridge.stop()
