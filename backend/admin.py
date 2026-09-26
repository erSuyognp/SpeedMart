"""Admin routes (8.3): login, state, reset, force-exit, overrides, demo-login, LED, force-decline stub."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend import db, eventlog, serial_bridge, shelf_state, store, ws
from backend.routes_api import ApiError, public_session
from backend.settings import settings

# Next charge returns DECLINED while on (9.9). Read by payments in S4.1/S4.2.
force_decline = False


class LoginBody(BaseModel):
    password: str


class OverrideBody(BaseModel):
    sku: str
    delta: int


class LedBody(BaseModel):
    cmd: str


class ForceDeclineBody(BaseModel):
    on: bool


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise ApiError(401, "admin_required", "Admin login required.")


public = APIRouter()
router = APIRouter(dependencies=[Depends(require_admin)])


@public.post("/admin/login")
def login(body: LoginBody, request: Request):
    expected = settings.env.admin_password
    if not expected:
        raise ApiError(503, "admin_disabled", "ADMIN_PASSWORD is not set in .env.")
    if not secrets.compare_digest(body.password.encode(), expected.encode()):
        eventlog.log("admin_login_failed")
        raise ApiError(401, "bad_password", "Wrong password.")
    request.session["admin"] = True
    eventlog.log("admin_login")
    return {"ok": True}


def _health() -> dict:
    stripe_key = settings.env.stripe_secret_key
    return {
        "vision_age_ms": shelf_state.last_snapshot_age_ms(),
        "serial": serial_bridge.is_connected(),
        "stripe": ("test" if stripe_key.startswith("sk_test_") else "no_key") if settings.features.stripe else "off",
        "llm": settings.features.llm and bool(settings.env.anthropic_api_key or settings.env.openai_api_key),
    }


@router.get("/admin/state")
def state():
    session = store.current_session()
    member = None
    if session is not None:
        conn = db.connect()
        try:
            row = conn.execute("SELECT id, name, budget_usd, is_demo FROM members WHERE id = ?",
                               (session["member_id"],)).fetchone()
            member = dict(row) if row else None
        finally:
            conn.close()
    return {
        "lock": {"occupied": session is not None},
        "session": public_session(session) if session else None,
        "member": member,
        "baseline": session["baseline"] if session else None,
        "overrides": store.get_overrides(session["id"]) if session else {},
        "shelf": shelf_state.state(),
        "cart": store.cart_for(session) if session else None,
        "health": _health(),
        "force_decline": force_decline,
        "features": settings.features.as_dict(),
        "skus": [{"sku": s.sku, "name": s.name} for s in settings.skus.values()],
        "sockets": ws.manager.count(),
        "events": eventlog.tail(50),
    }


def do_reset(source: str = "admin") -> dict:
    """Cancel the active session, clear overrides, free the lock, LEDs idle. Also the board's BTN,0."""
    cancelled = store.cancel(reason="admin_reset")
    store.clear_overrides()
    eventlog.log("admin_reset", source=source, cancelled_session_id=cancelled["id"] if cancelled else None)
    serial_bridge.send("SHELF,IDLE")
    serial_bridge.send("GATE,IDLE")
    return {"ok": True, "cancelled_session_id": cancelled["id"] if cancelled else None}


@router.post("/admin/reset")
def reset():
    return do_reset()


@router.post("/admin/force-exit")
def force_exit():
    cancelled = store.cancel(reason="admin_force_exit")
    eventlog.log("admin_force_exit", cancelled_session_id=cancelled["id"] if cancelled else None)
    return {"ok": True, "cancelled_session_id": cancelled["id"] if cancelled else None}


@router.post("/admin/override")
def override(body: OverrideBody):
    return {"cart": store.apply_override(body.sku, body.delta)}


@router.post("/admin/demo-login")
def demo_login(request: Request):
    conn = db.connect()
    try:
        row = conn.execute("SELECT id, name FROM members WHERE is_demo = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise ApiError(404, "no_demo_member", "Demo member is missing.")
    request.session["member_id"] = row["id"]
    eventlog.log("demo_login", member_id=row["id"])
    return {"ok": True, "member": {"id": row["id"], "name": row["name"]}}


@router.post("/admin/led")
def led(body: LedBody):
    cmd = body.cmd.strip()
    if not cmd or not cmd.isascii() or not cmd.isprintable():  # one line, no control chars
        raise ApiError(400, "bad_command", "Command must be one line of ASCII, e.g. LED,0,OFF.")
    if not serial_bridge.send(cmd):
        raise ApiError(409, "hardware_leds_off", "hardware_leds is off in config.json.")
    eventlog.log("admin_led", cmd=cmd, connected=serial_bridge.is_connected())
    return {"ok": True, "connected": serial_bridge.is_connected()}


@router.post("/admin/force-decline")
def set_force_decline(body: ForceDeclineBody):
    global force_decline
    force_decline = body.on
    eventlog.log("force_decline", on=body.on)  # payments honour it from S4.1
    return {"ok": True, "on": force_decline}
