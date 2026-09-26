"""S3.2: signup, /api/me, logout, demo account."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import db, members, shelf_state, store
from backend.main import app
from identity_helpers import login_as, set_features
from test_cart import FULL, snap, tmp_data  # noqa: F401  (tmp_data: temp DB + event log per test)

pytestmark = pytest.mark.usefixtures("tmp_data")


def member_row(member_id: str) -> dict:
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone())
    finally:
        conn.close()


def test_signup_creates_member_and_logs_in():
    with TestClient(app) as client:
        r = client.post("/api/members/signup", json={"name": "  Maya  Lopez ", "budget_usd": 25, "dietary": "vegan"})
        assert r.status_code == 200, r.text
        m = r.json()["member"]
        assert m["name"] == "Maya Lopez" and m["first_name"] == "Maya"
        assert m["budget_usd"] == 25 and m["dietary"] == "vegan" and m["points"] == 0 and m["is_demo"] is False
        assert m["card_label"] == "Visa •••• 4242 (test)"
        assert "stripe_customer_id" not in m and "stripe_pm_id" not in m

        row = member_row(m["id"])
        assert row["name"] == "Maya Lopez" and row["is_demo"] == 0

        me = client.get("/api/me").json()
        assert me == {"member": m, "has_passkey": False, "active_session_id": None}
        assert client.get("/api/me").json()["member"]["id"] == m["id"]  # reload keeps login


def test_signup_defaults_and_dietary_mapping():
    with TestClient(app) as client:
        m = client.post("/api/members/signup", json={"name": "Sam"}).json()["member"]
        assert m["budget_usd"] == 20 and m["dietary"] is None
        m = client.post("/api/members/signup", json={"name": "Ana", "dietary": "gluten_free"}).json()["member"]
        assert m["dietary"] == "gluten free"
        assert client.get("/api/me").json()["member"]["id"] == m["id"]  # newest signup wins the cookie


@pytest.mark.parametrize("body, code", [
    ({"name": "   "}, "bad_name"),
    ({"name": "x" * 41}, "bad_name"),
    ({"name": "Maya", "budget_usd": 9}, "bad_budget"),
    ({"name": "Maya", "budget_usd": 51}, "bad_budget"),
    ({"name": "Maya", "dietary": "keto"}, "bad_dietary"),
    ({}, "bad_request"),
])
def test_signup_validation(body, code):
    with TestClient(app) as client:
        r = client.post("/api/members/signup", json=body)
        assert r.status_code == 422 and r.json()["error"] == code
        assert client.get("/api/me").status_code == 401


def test_signup_off(monkeypatch):
    set_features(monkeypatch, signup=False)
    with TestClient(app) as client:
        r = client.post("/api/members/signup", json={"name": "Maya"})
        assert r.status_code == 404 and r.json()["error"] == "signup_off"


def test_me_requires_login_and_logout_clears_it():
    with TestClient(app) as client:
        r = client.get("/api/me")
        assert r.status_code == 401 and r.json()["error"] == "not_logged_in"
        client.post("/api/members/signup", json={"name": "Maya"})
        assert client.post("/api/logout").json() == {"ok": True}
        assert client.get("/api/me").status_code == 401
        assert client.post("/api/logout").json() == {"ok": True}  # idempotent


def test_cookie_for_deleted_member_is_401():
    with TestClient(app) as client:
        login_as(client, "mem_gone")
        assert client.get("/api/me").status_code == 401


def test_new_login_drops_previous_members_fresh_verification():
    req = Request({"type": "http", "session": {"member_id": "a", "verified_at": 1, "verified_member": "a",
                                               "verified_purpose": "enter", "admin": True}})
    members.log_in(req, "b")
    assert req.session == {"member_id": "b", "admin": True}  # admin rights are not member state
    req.session["verified_member"] = "b"
    members.log_in(req, "b")  # same member: keeps its own verification
    assert req.session == {"member_id": "b", "admin": True, "verified_member": "b"}


def test_me_reports_active_session_and_demo_member():
    with TestClient(app) as client:
        client.post("/admin/login", json={"password": "test-admin-password"})
        demo = client.post("/admin/demo-login").json()["member"]
        me = client.get("/api/me").json()
        assert me["member"]["is_demo"] is True and me["member"]["name"] == "Demo Shopper"
        shelf_state.apply_snapshot(snap(FULL))
        session = store.start_session(demo["id"])
        assert client.get("/api/me").json()["active_session_id"] == session["id"]
