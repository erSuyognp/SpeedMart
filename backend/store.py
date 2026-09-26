"""Store lock, store sessions, state machine (7.1)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from backend import cart, db, eventlog, shelf_state
from backend.settings import settings

IN_STORE = "IN_STORE"
CHECKOUT_PENDING = "CHECKOUT_PENDING"
PAID = "PAID"
CLOSED = "CLOSED"
CANCELLED = "CANCELLED"
ACTIVE_STATES = (IN_STORE, CHECKOUT_PENDING)
TIMEOUT_CHECK_SECONDS = 30


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StoreOccupied(StoreError):
    def __init__(self, occupant_first_name: str):
        super().__init__("store_occupied", "Someone is already shopping. Please wait a moment.")
        self.occupant_first_name = occupant_first_name


class InvalidTransition(StoreError):
    def __init__(self, current: str | None, target: str):
        super().__init__("invalid_state", f"Cannot move session from {current or 'none'} to {target}.")


# Guards every state change. The DB check inside it is the second half of the store lock (7.1).
_lock = threading.Lock()
# Admin manual adjustments per SKU for the active session (8.3). In memory; cleared on start/end/reset.
_overrides: dict[str, int] = {}
_last_cart_key: tuple | None = None


def _row_to_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "member_id": row["member_id"],
        "state": row["state"],
        "baseline": json.loads(row["baseline_json"]),
        "final_cart": json.loads(row["final_cart_json"]) if row["final_cart_json"] else None,
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
    }


def _active_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM store_sessions WHERE state IN ({','.join('?' * len(ACTIVE_STATES))}) "
        "ORDER BY started_at DESC LIMIT 1", ACTIVE_STATES).fetchone()


def _member(conn: sqlite3.Connection, member_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if row is None:
        raise StoreError("unknown_member", f"Member {member_id} does not exist.")
    return dict(row)


def _first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "Someone"


def get_session(session_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        return _row_to_session(conn.execute("SELECT * FROM store_sessions WHERE id = ?", (session_id,)).fetchone())
    finally:
        conn.close()


def current_session() -> dict[str, Any] | None:
    """The session in IN_STORE or CHECKOUT_PENDING, if any."""
    conn = db.connect()
    try:
        return _row_to_session(_active_row(conn))
    finally:
        conn.close()


def is_occupied() -> bool:
    return current_session() is not None


def get_overrides() -> dict[str, int]:
    with _lock:
        return dict(_overrides)


def clear_overrides() -> None:
    with _lock:
        _overrides.clear()


def _live_shelf_counts(session: dict[str, Any]) -> dict[str, int]:
    # Right after a backend restart no snapshot has arrived yet; an empty shelf would look like
    # everything was picked. Treat the shelf as unchanged until vision reports again.
    if not shelf_state.has_snapshot():
        return dict(session["baseline"])
    return shelf_state.shelf_counts()


def compute_live_cart(session: dict[str, Any], member: dict[str, Any] | None = None) -> dict[str, Any]:
    if member is None:
        conn = db.connect()
        try:
            member = _member(conn, session["member_id"])
        finally:
            conn.close()
    return cart.compute_cart(session, _live_shelf_counts(session), get_overrides(), member,
                             misplaced=shelf_state.misplaced())


def cart_for(session: dict[str, Any]) -> dict[str, Any]:
    """Frozen cart once checkout started (7.1 cart freezing), live cart otherwise."""
    if session["final_cart"] is not None:
        return {**session["final_cart"], "state": session["state"]}
    return compute_live_cart(session)


def _cart_key(snapshot: dict[str, Any]) -> tuple:
    return (snapshot["session_id"], snapshot["state"],
            tuple((i["sku"], i["qty"]) for i in snapshot["items"]),
            tuple((w["tag_id"], w["bay"]) for w in snapshot["warnings"]))


def _log_cart_if_changed(snapshot: dict[str, Any], source: str) -> bool:
    global _last_cart_key
    key = _cart_key(snapshot)
    if key == _last_cart_key:
        return False
    _last_cart_key = key
    eventlog.log("cart_changed", source=source, session_id=snapshot["session_id"], state=snapshot["state"],
                 items={i["sku"]: i["qty"] for i in snapshot["items"]}, total_usd=snapshot["total_usd"],
                 warnings=[w["message"] for w in snapshot["warnings"]])
    return True


def current_cart(source: str = "recompute") -> dict[str, Any] | None:
    """Recompute the active session's cart and log it if it changed. None when the store is empty."""
    session = current_session()
    if session is None:
        return None
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, source)
    return snapshot


def on_shelf_change() -> dict[str, Any] | None:
    """Called after a snapshot changed the shelf. S1.3 adds the WebSocket broadcast."""
    return current_cart(source="vision")


def apply_override(sku: str, delta: int) -> dict[str, Any]:
    """Admin manual fallback (8.3): shift the cart qty of `sku` by `delta`, clamped to [0, baseline].

    The stored override is clamped too, so pressing +1 past the limit and then -1 moves the cart at once.
    """
    if sku not in settings.skus:
        raise StoreError("unknown_sku", f"Unknown SKU '{sku}'.")
    session = current_session()
    if session is None or session["state"] != IN_STORE:
        raise StoreError("no_active_session", "Overrides need a shopper in the store.")
    base = session["baseline"].get(sku, 0)
    shelf = _live_shelf_counts(session).get(sku, 0)
    with _lock:
        before = _overrides.get(sku, 0)
        unclamped = base - shelf  # cart qty with no override, before clamping
        target = max(0, min(base, unclamped + before + int(delta)))
        _overrides[sku] = target - unclamped
        after = _overrides[sku]
    eventlog.log("override", source="override", session_id=session["id"], sku=sku, delta=int(delta),
                 override_before=before, override_after=after)
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, "override")
    return snapshot


def start_session(member_id: str) -> dict[str, Any]:
    """(none) -> IN_STORE. Baseline = what is on the shelf right now. Raises StoreOccupied."""
    with _lock:
        conn = db.connect()
        try:
            member = _member(conn, member_id)
            occupant = _active_row(conn)
            if occupant is not None:
                other = _member(conn, occupant["member_id"])
                eventlog.log("store_occupied", member_id=member_id, occupant_session_id=occupant["id"])
                raise StoreOccupied(_first_name(other["name"]))
            baseline = shelf_state.shelf_counts()
            session_id = db.new_id("ses")
            conn.execute(
                "INSERT INTO store_sessions (id, member_id, state, baseline_json, started_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, member_id, IN_STORE, json.dumps(baseline), db.now_iso()))
            conn.commit()
            _overrides.clear()
            session = _row_to_session(conn.execute("SELECT * FROM store_sessions WHERE id = ?",
                                                   (session_id,)).fetchone())
        finally:
            conn.close()
    eventlog.log("session_state", session_id=session_id, member_id=member_id, **{"from": None}, to=IN_STORE,
                 baseline=baseline)
    snapshot = compute_live_cart(session, member)
    _log_cart_if_changed(snapshot, "session_start")
    return session


def _transition(allowed_from: tuple[str, ...], target: str, *, session_id: str | None = None,
                final_cart: dict[str, Any] | None | str = "keep", end: bool = False,
                reason: str | None = None) -> dict[str, Any]:
    """Move one session to `target` under the store lock. final_cart: 'keep', None (clear) or a snapshot."""
    with _lock:
        conn = db.connect()
        try:
            row = (conn.execute("SELECT * FROM store_sessions WHERE id = ?", (session_id,)).fetchone()
                   if session_id else _active_row(conn))
            if row is None or row["state"] not in allowed_from:
                raise InvalidTransition(row["state"] if row else None, target)
            sets, args = ["state = ?"], [target]
            if final_cart != "keep":
                sets.append("final_cart_json = ?")
                args.append(json.dumps(final_cart) if final_cart is not None else None)
            if end:
                sets.append("ended_at = ?")
                args.append(db.now_iso())
            conn.execute(f"UPDATE store_sessions SET {', '.join(sets)} WHERE id = ?", (*args, row["id"]))
            conn.commit()
            if end:
                _overrides.clear()
            session = _row_to_session(conn.execute("SELECT * FROM store_sessions WHERE id = ?",
                                                   (row["id"],)).fetchone())
        finally:
            conn.close()
    fields = {"reason": reason} if reason else {}
    eventlog.log("session_state", session_id=session["id"], member_id=session["member_id"],
                 **{"from": row["state"]}, to=target, **fields)
    return session


def freeze_cart() -> dict[str, Any]:
    """IN_STORE -> CHECKOUT_PENDING. Copies the live cart into final_cart_json; the charge uses it."""
    session = current_session()
    if session is None or session["state"] != IN_STORE:
        raise InvalidTransition(session["state"] if session else None, CHECKOUT_PENDING)
    frozen = {**compute_live_cart(session), "state": CHECKOUT_PENDING}
    session = _transition((IN_STORE,), CHECKOUT_PENDING, session_id=session["id"], final_cart=frozen)
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, "freeze")
    return snapshot


def unfreeze_cart() -> dict[str, Any]:
    """CHECKOUT_PENDING -> IN_STORE (shopper cancelled exit). Cart goes live again."""
    session = _transition((CHECKOUT_PENDING,), IN_STORE, final_cart=None)
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, "unfreeze")
    return snapshot


def mark_paid(session_id: str) -> dict[str, Any]:
    """CHECKOUT_PENDING -> PAID after an AUTHORIZED charge. Frees the store lock."""
    return _transition((CHECKOUT_PENDING,), PAID, session_id=session_id, end=True)


def close(session_id: str, reason: str = "done") -> dict[str, Any]:
    """PAID -> CLOSED (receipt viewed / 60 s), or an active session with an empty cart -> CLOSED."""
    session = get_session(session_id)
    if session is not None and session["state"] in ACTIVE_STATES and cart_for(session)["items"]:
        raise StoreError("cart_not_empty", "Only an empty cart can leave without paying.")
    ended = session is not None and session["ended_at"] is not None
    return _transition((PAID, *ACTIVE_STATES), CLOSED, session_id=session_id, end=not ended, reason=reason)


def cancel(session_id: str | None = None, reason: str = "admin") -> dict[str, Any] | None:
    """IN_STORE / CHECKOUT_PENDING -> CANCELLED (admin reset / force-exit / timeout). None if nothing to cancel."""
    try:
        return _transition(ACTIVE_STATES, CANCELLED, session_id=session_id, end=True, reason=reason)
    except InvalidTransition:
        return None


def expire_stale_sessions(now: datetime | None = None) -> list[str]:
    """Cancel IN_STORE sessions older than store.max_session_minutes. Returns cancelled ids."""
    now = now or datetime.now(timezone.utc)
    limit = timedelta(minutes=float(settings.store.get("max_session_minutes", 15)))
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, started_at FROM store_sessions WHERE state = ?", (IN_STORE,)).fetchall()
    finally:
        conn.close()
    expired = []
    for row in rows:
        if now - datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")) > limit:
            if cancel(row["id"], reason="timeout"):
                expired.append(row["id"])
    return expired


async def timeout_task() -> None:
    """Background loop started by main.py's lifespan (7.1 timeout)."""
    while True:
        await asyncio.sleep(TIMEOUT_CHECK_SECONDS)
        try:
            await asyncio.to_thread(expire_stale_sessions)
        except Exception as e:  # never let the checker die
            eventlog.log("timeout_task_error", error=repr(e))
