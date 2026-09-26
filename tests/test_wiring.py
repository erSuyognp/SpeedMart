"""Integration wiring: the routers the overnight agents built are mounted on the real app, ahead of the
static mount (StaticFiles matches every path, so anything registered after it never sees a request)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from backend.main import app
from test_cart import tmp_data  # noqa: F401

pytestmark = pytest.mark.usefixtures("tmp_data")


def route_paths() -> set[str]:
    """Every path the app actually serves. Read from the OpenAPI schema: this FastAPI keeps included
    routers in opaque _IncludedRouter entries, so walking app.routes only finds the inline ones."""
    return set(app.openapi()["paths"])


def test_intent_routes_are_registered():
    assert {"/api/intent", "/api/intent/current"} <= route_paths()


def test_kiosk_route_is_registered():
    assert "/api/kiosk/qr/{which}" in route_paths()


def test_static_mount_is_last():
    routes = app.routes
    mounts = [i for i, r in enumerate(routes) if isinstance(r, Mount)]
    assert mounts, "the web/ StaticFiles mount is missing"
    assert mounts[-1] == len(routes) - 1, "something is registered after the static mount and can never be reached"


def test_intent_and_kiosk_answer_through_the_app():
    with TestClient(app) as client:
        # Not signed in: the router ran (its own 401), rather than StaticFiles' 404.
        r = client.get("/api/intent/current")
        assert r.status_code == 401 and r.json()["error"] == "not_logged_in"
        assert client.get("/api/kiosk/qr/join").headers["content-type"] == "image/png"
        assert client.get("/api/kiosk/qr/nope").status_code == 404
