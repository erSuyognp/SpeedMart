"""Kiosk QR endpoint and URL builder (backend/kiosk.py).

The router is mounted on a test app of its own, so these tests pass whether or not backend/main.py
includes it yet.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import kiosk
from backend.settings import settings

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(kiosk.router)
    return TestClient(app)


@pytest.fixture
def fake_env(monkeypatch):
    """Known origin and gate tokens, so the URL assertions do not depend on the machine's .env."""
    env = dataclasses.replace(settings.env, public_origin="https://kiosk-test.example",
                              entry_gate_token="entry-7d2f", exit_gate_token="exit-91ac")
    monkeypatch.setattr(kiosk, "settings", dataclasses.replace(settings, env=env))
    kiosk.clear_cache()
    yield
    kiosk.clear_cache()


# --- url_for (8.1 / 13.2 targets) ---

def test_url_for_join_is_the_origin_root(fake_env):
    assert kiosk.url_for("join") == "https://kiosk-test.example/"


def test_url_for_enter_carries_the_entry_gate_token(fake_env):
    assert kiosk.url_for("enter") == "https://kiosk-test.example/enter.html?g=entry-7d2f"


def test_url_for_exit_carries_the_exit_gate_token(fake_env):
    assert kiosk.url_for("exit") == "https://kiosk-test.example/exit.html?g=exit-91ac"


def test_url_for_strips_a_trailing_slash_from_the_origin(monkeypatch):
    env = dataclasses.replace(settings.env, public_origin="https://kiosk-test.example/",
                              entry_gate_token="entry-7d2f")
    monkeypatch.setattr(kiosk, "settings", dataclasses.replace(settings, env=env))
    assert kiosk.url_for("join") == "https://kiosk-test.example/"
    assert kiosk.url_for("enter") == "https://kiosk-test.example/enter.html?g=entry-7d2f"


def test_url_for_percent_encodes_a_token_with_url_characters(monkeypatch):
    env = dataclasses.replace(settings.env, public_origin="https://kiosk-test.example",
                              entry_gate_token="a b&c=d")
    monkeypatch.setattr(kiosk, "settings", dataclasses.replace(settings, env=env))
    assert kiosk.url_for("enter") == "https://kiosk-test.example/enter.html?g=a%20b%26c%3Dd"


@pytest.mark.parametrize("which", ["", "Join", "signup", "enter.html", "../enter", "admin"])
def test_url_for_rejects_anything_else(which):
    with pytest.raises(kiosk.UnknownKioskQr):
        kiosk.url_for(which)


# --- GET /api/kiosk/qr/{which} ---

@pytest.mark.parametrize("which", list(kiosk.KINDS))
def test_valid_names_return_a_png(client, fake_env, which):
    r = client.get(f"/api/kiosk/qr/{which}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(PNG_MAGIC)
    assert len(r.content) > 200


@pytest.mark.parametrize("which", ["Join", "signup", "enter.html", "join%20", "admin"])
def test_invalid_names_return_404(client, fake_env, which):
    r = client.get(f"/api/kiosk/qr/{which}")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_qr"


def test_404_body_never_leaks_or_reflects_anything(client, fake_env):
    r = client.get("/api/kiosk/qr/entry-7d2f")
    assert r.status_code == 404
    assert "entry-7d2f" not in r.text  # no reflection of the requested name
    assert "exit-91ac" not in r.text
    assert "kiosk-test.example" not in r.text


def test_pngs_are_cached_in_memory(client, fake_env):
    first = client.get("/api/kiosk/qr/enter")
    assert kiosk.url_for("enter") in kiosk._cache
    second = client.get("/api/kiosk/qr/enter")
    assert first.content == second.content


def test_each_kind_gets_a_different_png(client, fake_env):
    pngs = {w: client.get(f"/api/kiosk/qr/{w}").content for w in kiosk.KINDS}
    assert len(set(pngs.values())) == len(kiosk.KINDS)


def test_cache_follows_the_url_not_the_name(fake_env, monkeypatch):
    before = kiosk.png_for_url(kiosk.url_for("enter"))
    env = dataclasses.replace(settings.env, public_origin="https://moved.example",
                              entry_gate_token="entry-7d2f")
    monkeypatch.setattr(kiosk, "settings", dataclasses.replace(settings, env=env))
    assert kiosk.png_for_url(kiosk.url_for("enter")) != before
