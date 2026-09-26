"""Signup, /me, logout, demo account (F6, 8.1)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend import bank, db, eventlog, store
from backend.payments import DEFAULT_CARD_LABEL as TEST_CARD_LABEL
from backend.routes_api import ApiError
from backend.settings import settings

MIN_BUDGET_USD = 10
MAX_BUDGET_USD = 50
MAX_NAME_LEN = 40
# value sent by the dietary select -> stored tag (read by the agent)
DIETARY = {"": None, "none": None, "vegetarian": "vegetarian", "vegan": "vegan", "gluten_free": "gluten free"}
# Session cookie keys that belong to one signed-in member; dropped whenever the member changes.
MEMBER_SESSION_KEYS = ("member_id", "verified_at", "verified_member", "verified_purpose")


class SignupBody(BaseModel):
    name: str
    budget_usd: float | None = None
    dietary: str | None = None


router = APIRouter()


def get_member(member_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0]


def public_member(m: dict[str, Any]) -> dict[str, Any]:
    """What the phone may see: no Stripe ids."""
    return {
        "id": m["id"],
        "name": m["name"],
        "first_name": first_name(m["name"]),
        "budget_usd": m["budget_usd"],
        "dietary": m["dietary"],
        "card_label": m["card_label"],
        "points": m["points"],
        "is_demo": bool(m["is_demo"]),
        "created_at": m["created_at"],
    }


def log_in(request: Request, member_id: str) -> None:
    """Sign this browser in as member_id. A different member never inherits a fresh verification."""
    if request.session.get("member_id") != member_id:
        for key in MEMBER_SESSION_KEYS:
            request.session.pop(key, None)
    request.session["member_id"] = member_id


def current_member(request: Request) -> dict[str, Any]:
    """The signed-in member, or 401. A cookie for a member that no longer exists (DB reset) is cleared."""
    member_id = request.session.get("member_id")
    member = get_member(member_id) if member_id else None
    if member is None:
        if member_id:
            for key in MEMBER_SESSION_KEYS:
                request.session.pop(key, None)
        raise ApiError(401, "not_logged_in", "Please sign in first.")
    return member


def _link_card(member_id: str, name: str) -> None:
    """Stripe test card at signup when F10 is on (S4.2). Never fails signup: the mock provider covers."""
    from backend import payments

    link = getattr(payments, "link_test_card", None)
    if link is not None:
        link(member_id, name)


@router.post("/api/members/signup")
def signup(body: SignupBody, request: Request):
    if not settings.features.signup:
        raise ApiError(404, "signup_off", "Joining is closed for this demo. Ask the team for the Demo Shopper.")
    name = " ".join(body.name.split())
    if not name:
        raise ApiError(422, "bad_name", "Please enter your first name.")
    if len(name) > MAX_NAME_LEN:
        raise ApiError(422, "bad_name", f"Please keep your name under {MAX_NAME_LEN} characters.")
    budget = body.budget_usd if body.budget_usd is not None else float(settings.store.get("default_budget_usd", 20))
    if not MIN_BUDGET_USD <= budget <= MAX_BUDGET_USD:
        raise ApiError(422, "bad_budget", f"Budget must be between ${MIN_BUDGET_USD} and ${MAX_BUDGET_USD}.")
    dietary_key = (body.dietary or "").strip().lower().replace(" ", "_")
    if dietary_key not in DIETARY:
        raise ApiError(422, "bad_dietary", "Pick one of: none, vegetarian, vegan, gluten free.")

    member_id = db.new_id("mem")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO members (id, name, budget_usd, dietary, card_label, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (member_id, name, round(float(budget), 2), DIETARY[dietary_key], TEST_CARD_LABEL, db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    eventlog.log("member_signup", member_id=member_id, budget_usd=budget, dietary=DIETARY[dietary_key])
    bank.open_account(member_id)  # demo bank (8.15): the opening balance behind the demo card
    if settings.features.stripe:
        _link_card(member_id, name)
    log_in(request, member_id)
    return {"member": public_member(get_member(member_id))}


def has_passkey(member_id: str) -> bool:
    conn = db.connect()
    try:
        return conn.execute("SELECT 1 FROM passkeys WHERE member_id = ? LIMIT 1", (member_id,)).fetchone() is not None
    finally:
        conn.close()


@router.get("/api/me")
def me(request: Request):
    member = current_member(request)
    session = store.current_session()
    return {
        "member": public_member(member),
        "has_passkey": has_passkey(member["id"]),
        "active_session_id": session["id"] if session and session["member_id"] == member["id"] else None,
    }


@router.post("/api/logout")
def logout(request: Request):
    member_id = request.session.get("member_id")
    for key in MEMBER_SESSION_KEYS:
        request.session.pop(key, None)
    if member_id:
        eventlog.log("member_logout", member_id=member_id)
    return {"ok": True}
