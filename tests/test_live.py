"""S1.3 tests: WebSocket pushes, admin routes, store/current and the gates-off dev routes."""

from __future__ import annotations

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("INTERNAL_TOKEN", "test-internal-token")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")

from fastapi.testclient import TestClient  # noqa: E402

from backend import store  # noqa: E402
from backend.main import app  # noqa: E402
from test_cart import FULL, TOKEN, qty, simulate_restart, snap, tmp_data, with_bays  # noqa: E402,F401

HEADERS = {"X-Internal-Token": TOKEN}


def receive_until(sock, msg_type: str, limit: int = 20) -> dict:
    for _ in range(limit):
        msg = sock.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no {msg_type} message in {limit} messages")


def admin_login(client: TestClient) -> None:
    assert client.post("/admin/login", json={"password": "test-admin-password"}).status_code == 200


def enter_store(client: TestClient) -> dict:
    """Admin login, demo-login, full shelf, start via the dev route."""
    admin_login(client)
    assert client.post("/admin/demo-login").status_code == 200
    assert client.post("/internal/shelf", json=snap(FULL), headers=HEADERS).status_code == 200
    r = client.post("/api/dev/start")
    assert r.status_code == 200, r.text
    return r.json()


def test_admin_routes_need_login(tmp_data):
    with TestClient(app) as client:
        for path in ("/admin/reset", "/admin/force-exit", "/admin/demo-login"):
            r = client.post(path)
            assert r.status_code == 401 and r.json()["error"] == "admin_required"
        assert client.get("/admin/state").status_code == 401
        r = client.post("/admin/login", json={"password": "nope"})
        assert r.status_code == 401 and r.json()["error"] == "bad_password"
        r = client.post("/admin/login", json={})
        assert r.status_code == 422 and r.json()["error"] == "bad_request"
        admin_login(client)
        state = client.get("/admin/state").json()
        assert state["lock"] == {"occupied": False} and state["session"] is None
        assert list(state["shelf"]["bays"]) == ["0", "1", "2"]
        assert state["events"][-1]["type"] == "admin_login"


def test_dev_start_needs_member_and_fresh_vision(tmp_data):
    with TestClient(app) as client:
        r = client.post("/api/dev/start")
        assert r.status_code == 401 and r.json()["error"] == "not_logged_in"
        admin_login(client)
        client.post("/admin/demo-login")
        r = client.post("/api/dev/start")  # no snapshot yet
        assert r.status_code == 503
        assert r.json() == {"error": "vision_unavailable",
                            "message": "The shelf camera is offline. Please wait a moment."}


def test_shelf_change_pushes_cart_to_member_socket(tmp_data):
    with TestClient(app) as client:
        started = enter_store(client)
        assert started["session"]["state"] == "IN_STORE" and started["cart"]["items"] == []

        # The store page socket: the member cookie tags it with the demo member id.
        with client.websocket_connect("/ws") as sock:
            first = receive_until(sock, "cart")["data"]
            assert first["session_id"] == started["session"]["id"] and first["items"] == []

            client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)
            pushed = receive_until(sock, "cart")["data"]
            assert qty(pushed, "elx") == 1 and pushed["total_usd"] == 8.64

            client.post("/internal/shelf", json=snap(FULL, frame_id=3), headers=HEADERS)
            assert receive_until(sock, "cart")["data"]["items"] == []

            assert client.post("/admin/override", json={"sku": "bar", "delta": 1}).status_code == 200
            assert qty(receive_until(sock, "cart")["data"], "bar") == 1

            client.post("/admin/force-exit")
            assert receive_until(sock, "gate")["data"] == {"event": "cancelled"}
            assert receive_until(sock, "cart")["data"]["state"] == "CANCELLED"
            assert receive_until(sock, "store_status")["data"] == {"occupied": False}


def test_admin_socket_gets_shelf_and_log(tmp_data):
    with TestClient(app) as client:
        enter_store(client)
        with client.websocket_connect("/ws?role=admin") as sock:
            initial = [sock.receive_json()["type"] for _ in range(3)]
            assert initial == ["store_status", "cart", "shelf"]
            client.post("/internal/shelf", json=snap(with_bays(b2=[5]), frame_id=2), headers=HEADERS)
            seen = {}
            while "cart" not in seen:  # order: log lines, shelf, cart (the cart is always pushed last)
                msg = sock.receive_json()
                seen.setdefault(msg["type"], []).append(msg["data"])
            shelf = seen["shelf"][0]
            assert shelf["bays"]["2"] == [5] and shelf["bay_status"]["2"] == {"stable": True, "motion": False}
            assert qty(seen["cart"][0], "bar") == 1
            assert "shelf_changed" in [e["type"] for e in seen["log"]]


def test_stranger_sees_no_cart(tmp_data):
    with TestClient(app) as shopper, TestClient(app) as stranger:
        enter_store(shopper)
        assert stranger.get("/api/store/current").json() == {"session": None}
        assert shopper.get("/api/store/current").json()["session"]["state"] == "IN_STORE"
        with stranger.websocket_connect("/ws?role=admin") as sock:  # not an admin: role is ignored
            assert sock.receive_json() == {"type": "store_status", "data": {"occupied": True}}
            shopper.post("/admin/override", json={"sku": "elx", "delta": 1})
            shopper.post("/admin/reset")
            # The override's cart never reaches this socket; the next message is the lock release.
            assert sock.receive_json() == {"type": "store_status", "data": {"occupied": False}}


def test_dev_checkout_freezes_cart(tmp_data):
    with TestClient(app) as client:
        enter_store(client)
        client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)
        r = client.post("/api/dev/checkout")
        assert r.status_code == 200 and r.json()["cart"]["state"] == "CHECKOUT_PENDING"
        assert qty(r.json()["cart"], "elx") == 1
        r = client.post("/admin/override", json={"sku": "elx", "delta": 1})
        assert r.status_code == 409 and r.json()["error"] == "no_active_session"
        assert client.post("/api/dev/checkout").status_code == 409


def test_overrides_survive_backend_restart(tmp_data):
    with TestClient(app) as client:
        enter_store(client)
        client.post("/admin/override", json={"sku": "rec", "delta": 1})
        before = client.get("/api/store/current").json()["cart"]
    assert qty(before, "rec") == 1

    simulate_restart()
    with TestClient(app) as client:
        admin_login(client)
        client.post("/internal/shelf", json=snap(FULL, frame_id=500), headers=HEADERS)
        after = client.get("/api/store/current").json()["cart"]
        assert after["items"] == before["items"] and after["total_usd"] == before["total_usd"]

        client.post("/admin/reset")  # reset clears overrides for good
        assert store.get_overrides(before["session_id"]) == {}
