"""Store lock, store sessions, state machine (7.1).

Two kinds of session hold the store lock: shopping (IN_STORE, CHECKOUT_PENDING) and returning (RETURNING, the
Continue stage: a paid shopper puts items back and backend/returns.py refunds what the camera sees return).
A returning session has return_of = the paid session it refunds; its cart is always empty.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from backend import agent, cart, db, eventlog, shelf_state, ws
from backend.settings import settings

IN_STORE = "IN_STORE"
CHECKOUT_PENDING = "CHECKOUT_PENDING"
PAID = "PAID"
CLOSED = "CLOSED"
CANCELLED = "CANCELLED"
RETURNING = "RETURNING"
SHOPPING_STATES = (IN_STORE, CHECKOUT_PENDING)
ACTIVE_STATES = (IN_STORE, CHECKOUT_PENDING, RETURNING)  # every state that holds the store lock
TIMEOUT_CHECK_SECONDS = 30
RETURN_TIMEOUT_MINUTES = 3  # a RETURNING session older than this is CANCELLED, no refund
PAID_CLOSE_SECONDS = 60  # 7.1: PAID -> CLOSED when the receipt is viewed or after this long
VISION_MAX_AGE_MS = 2000  # entry is refused when the last shelf snapshot is older than this


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StoreOccupied(StoreError):
    def __init__(self, occupant_first_name: str):
        super().__init__("store_occupied", "Someone is already shopping. Please wait a moment.")
        self.occupant_first_name = occupant_first_name


class VisionUnavailable(StoreError):
    def __init__(self):
        super().__init__("vision_unavailable", "The shelf camera is offline. Please wait a moment.")


class InvalidTransition(StoreError):
    def __init__(self, current: str | None, target: str):
        super().__init__("invalid_state", f"Cannot move session from {current or 'none'} to {target}.")


# Guards every state change. The DB check inside it is the second half of the store lock (7.1).
_lock = threading.Lock()
# Admin manual adjustments per SKU for one session (8.3). Mirrored to data/overrides.json so a backend
# restart mid-session recomputes the same cart (7.3). Cleared on start/end/reset.
_overrides: dict[str, int] = {}
_overrides_session: str | None = None  # session id _overrides belongs to
_last_cart_key: tuple | None = None
# gate event (8.4) announced when a session moves into each state
GATE_EVENTS = {IN_STORE: "entered", CHECKOUT_PENDING: "exit_pending", PAID: "paid", CANCELLED: "cancelled",
               RETURNING: "return_started"}
_first_pick_logged: set[str] = set()  # sessions whose first_pick_at is already saved


def _row_to_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    keys = row.keys()
    return {
        "id": row["id"],
        "member_id": row["member_id"],
        "state": row["state"],
        "baseline": json.loads(row["baseline_json"]),
        "final_cart": json.loads(row["final_cart_json"]) if row["final_cart_json"] else None,
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        # measured results (Section 6): entered = started_at, then first pick, exit quote, approval
        **{k: row[k] if k in keys else None for k in ("return_of", "first_pick_at", "quoted_at", "approved_at")},
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
    """The session holding the store lock (IN_STORE, CHECKOUT_PENDING or RETURNING), if any."""
    conn = db.connect()
    try:
        return _row_to_session(_active_row(conn))
    finally:
        conn.close()


def is_occupied() -> bool:
    return current_session() is not None


def _overrides_path():
    return db.DATA_DIR / "overrides.json"


def _load_overrides_locked(session_id: str) -> None:
    global _overrides_session
    if _overrides_session == session_id:
        return
    _overrides.clear()
    try:
        saved = json.loads(_overrides_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved.get("session_id") == session_id:
        _overrides.update({k: int(v) for k, v in saved.get("overrides", {}).items() if k in settings.skus})
    _overrides_session = session_id


def _save_overrides_locked() -> None:
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"session_id": _overrides_session, "overrides": _overrides}), encoding="utf-8")
    tmp.replace(path)


def _reset_overrides_locked(session_id: str | None = None) -> None:
    global _overrides_session
    _overrides.clear()
    _overrides_session = session_id
    _overrides_path().unlink(missing_ok=True)


def get_overrides(session_id: str) -> dict[str, int]:
    with _lock:
        _load_overrides_locked(session_id)
        return dict(_overrides)


def clear_overrides() -> None:
    with _lock:
        _reset_overrides_locked()


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
    return agent.on_cart(cart.compute_cart(session, _live_shelf_counts(session), get_overrides(session["id"]),
                                           member, misplaced=shelf_state.misplaced()), member)


def empty_cart(session: dict[str, Any]) -> dict[str, Any]:
    """CartSnapshot with nothing in it: a returning session never buys anything."""
    return {"session_id": session["id"], "state": session["state"], "items": [], "subtotal_usd": 0.0,
            "tax_usd": 0.0, "total_usd": 0.0, "budget_usd": settings.store.get("default_budget_usd", 20),
            "over_budget": False, "warnings": [], "agent_line": "", "updated_at": db.now_iso()}


def cart_for(session: dict[str, Any]) -> dict[str, Any]:
    """Frozen cart once checkout started (7.1 cart freezing), live cart otherwise. Empty for a return."""
    if session.get("return_of"):
        return empty_cart(session)
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
    if snapshot["items"] and snapshot["state"] == IN_STORE:
        _record_first_pick(snapshot["session_id"])
    return True


def _record_first_pick(session_id: str) -> None:
    """Measured results: the first moment this session's cart has something in it."""
    if session_id in _first_pick_logged:
        return
    _first_pick_logged.add(session_id)
    at = db.now_iso()
    conn = db.connect()
    try:
        updated = conn.execute("UPDATE store_sessions SET first_pick_at = ? WHERE id = ? AND first_pick_at IS NULL",
                               (at, session_id)).rowcount
        conn.commit()
    finally:
        conn.close()
    if updated:
        eventlog.log("first_pick", session_id=session_id, at=at)


def current_cart(source: str = "recompute") -> dict[str, Any] | None:
    """Recompute the active session's cart and log it if it changed. None when the store is empty."""
    session = current_session()
    if session is None:
        return None
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, source)
    return snapshot


def on_shelf_change() -> dict[str, Any] | None:
    """Called after a snapshot changed the shelf: recompute, then push to the shopper and admins."""
    ws.broadcast_admin({"type": "shelf", "data": shelf_state.state()})
    session = current_session()
    if session is None:
        return None
    if session.get("return_of"):
        from backend import returns  # local: returns imports this module

        returns.on_shelf_change(session)
        return None
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, "vision")
    ws.broadcast_cart(session["member_id"], snapshot)
    return snapshot


def _announce(session: dict[str, Any], gate_event: str | None) -> None:
    """Push a state change: gate event, the session's cart (with its new state), store lock status."""
    if gate_event:
        ws.broadcast_gate(session["member_id"], gate_event)
    if not session.get("return_of"):  # a return has no cart; its phone gets {"type":"return"} instead
        ws.broadcast_cart(session["member_id"], cart_for(session))
    ws.broadcast_store_status(session["state"] in ACTIVE_STATES)


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
        _load_overrides_locked(session["id"])
        before = _overrides.get(sku, 0)
        unclamped = base - shelf  # cart qty with no override, before clamping
        target = max(0, min(base, unclamped + before + int(delta)))
        _overrides[sku] = target - unclamped
        after = _overrides[sku]
        _save_overrides_locked()
    eventlog.log("override", source="override", session_id=session["id"], sku=sku, delta=int(delta),
                 override_before=before, override_after=after)
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, "override")
    ws.broadcast_cart(session["member_id"], snapshot)
    return snapshot


def camera_quantities(session: dict[str, Any]) -> dict[str, int]:
    """What the camera alone puts in the cart right now: clamp(baseline - shelf_now, 0, baseline), no overrides.
    A cart dispute (8.13) compares the cart the shopper sees against this."""
    return cart.cart_quantities(session["baseline"], _live_shelf_counts(session), {})


def remove_one(sku: str, source: str = "dispute") -> dict[str, Any]:
    """Take one unit of `sku` out of the active shopping cart (cart disputes, 8.13) and return the cart.

    IN_STORE: a signed override of -1, clamped like apply_override. CHECKOUT_PENDING: the same override, so the
    live cart agrees if the shopper keeps shopping, and one unit fewer in the frozen cart the charge uses.
    """
    if sku not in settings.skus:
        raise StoreError("unknown_sku", f"Unknown SKU '{sku}'.")
    session = current_session()
    if session is None or session["state"] not in SHOPPING_STATES:
        raise StoreError("no_active_session", "You are not in the store.")
    base = session["baseline"].get(sku, 0)
    shelf = _live_shelf_counts(session).get(sku, 0)
    with _lock:
        _load_overrides_locked(session["id"])
        before = _overrides.get(sku, 0)
        unclamped = base - shelf
        target = max(0, min(base, unclamped + before - 1))
        _overrides[sku] = target - unclamped
        after = _overrides[sku]
        _save_overrides_locked()
        if session["state"] == CHECKOUT_PENDING and session["final_cart"] is not None:
            frozen = session["final_cart"]
            qty = {i["sku"]: int(i["qty"]) for i in frozen["items"]}
            qty[sku] = max(0, qty.get(sku, 0) - 1)
            refrozen = {**cart.priced_cart(session, qty, {"budget_usd": frozen["budget_usd"]},
                                           agent_line=frozen.get("agent_line", "")),
                        "warnings": frozen.get("warnings", []), "state": CHECKOUT_PENDING}
            conn = db.connect()
            try:
                conn.execute("UPDATE store_sessions SET final_cart_json = ? WHERE id = ? AND state = ?",
                             (json.dumps(refrozen), session["id"], CHECKOUT_PENDING))
                conn.commit()
            finally:
                conn.close()
    eventlog.log("override", source=source, session_id=session["id"], sku=sku, delta=-1,
                 override_before=before, override_after=after, state=session["state"])
    session = get_session(session["id"])
    snapshot = cart_for(session)
    _log_cart_if_changed(snapshot, source)
    ws.broadcast_cart(session["member_id"], snapshot)
    return snapshot


def _open_session_locked(conn: sqlite3.Connection, member_id: str, state: str,
                         return_of: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Insert a new lock-holding session (call with _lock held). Baseline = the shelf right now.
    Raises VisionUnavailable (no fresh snapshot) or StoreOccupied. Returns (session, member)."""
    member = _member(conn, member_id)
    age_ms = shelf_state.last_snapshot_age_ms()
    if not shelf_state.has_snapshot() or age_ms > VISION_MAX_AGE_MS:
        eventlog.log("vision_unavailable", member_id=member_id, vision_age_ms=age_ms)
        raise VisionUnavailable()
    occupant = _active_row(conn)
    if occupant is not None:
        other = _member(conn, occupant["member_id"])
        eventlog.log("store_occupied", member_id=member_id, occupant_session_id=occupant["id"])
        raise StoreOccupied(_first_name(other["name"]))
    baseline = shelf_state.shelf_counts()
    session_id = db.new_id("ses")
    conn.execute(
        "INSERT INTO store_sessions (id, member_id, state, baseline_json, started_at, return_of) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, member_id, state, json.dumps(baseline), db.now_iso(), return_of))
    conn.commit()
    _reset_overrides_locked(session_id)
    session = _row_to_session(conn.execute("SELECT * FROM store_sessions WHERE id = ?", (session_id,)).fetchone())
    return session, member


def start_session(member_id: str) -> dict[str, Any]:
    """(none) -> IN_STORE. Baseline = what is on the shelf right now.

    Raises StoreOccupied, or VisionUnavailable when there is no fresh snapshot to take the baseline from.
    """
    with _lock:
        conn = db.connect()
        try:
            session, member = _open_session_locked(conn, member_id, IN_STORE)
        finally:
            conn.close()
    eventlog.log("session_state", session_id=session["id"], member_id=member_id, **{"from": None}, to=IN_STORE,
                 baseline=session["baseline"])
    snapshot = compute_live_cart(session, member)
    _log_cart_if_changed(snapshot, "session_start")
    _announce(session, GATE_EVENTS[IN_STORE])
    return session


def start_return(member_id: str, original_session_id: str) -> dict[str, Any]:
    """(none) -> RETURNING for a paid visit. Same store lock and baseline rule as shopping: units the camera
    sees come back after this moment are candidate returns (backend/returns.py decides which ones count)."""
    with _lock:
        conn = db.connect()
        try:
            session, _ = _open_session_locked(conn, member_id, RETURNING, return_of=original_session_id)
        finally:
            conn.close()
    eventlog.log("session_state", session_id=session["id"], member_id=member_id, **{"from": None}, to=RETURNING,
                 baseline=session["baseline"], return_of=original_session_id)
    _announce(session, GATE_EVENTS[RETURNING])
    return session


def _transition(allowed_from: tuple[str, ...], target: str, *, session_id: str | None = None,
                final_cart: dict[str, Any] | None | str = "keep", end: bool = False,
                reason: str | None = None, stamp: str | None = None) -> dict[str, Any]:
    """Move one session to `target` under the store lock. final_cart: 'keep', None (clear) or a snapshot.
    stamp: a timestamp column ("quoted_at" or "approved_at") set to now in the same update."""
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
            if stamp in ("quoted_at", "approved_at"):
                sets.append(f"{stamp} = ?")
                args.append(db.now_iso())
            conn.execute(f"UPDATE store_sessions SET {', '.join(sets)} WHERE id = ?", (*args, row["id"]))
            conn.commit()
            if end:
                _reset_overrides_locked()
            session = _row_to_session(conn.execute("SELECT * FROM store_sessions WHERE id = ?",
                                                   (row["id"],)).fetchone())
        finally:
            conn.close()
    fields = {"reason": reason} if reason else {}
    eventlog.log("session_state", session_id=session["id"], member_id=session["member_id"],
                 **{"from": row["state"]}, to=target, **fields)
    # Back to IN_STORE from checkout is a cancelled exit, not a new entry: no gate event for it.
    _announce(session, GATE_EVENTS.get(target) if target != IN_STORE else None)
    return session


def freeze_cart() -> dict[str, Any]:
    """IN_STORE -> CHECKOUT_PENDING. Copies the live cart into final_cart_json; the charge uses it."""
    session = current_session()
    if session is None or session["state"] != IN_STORE:
        raise InvalidTransition(session["state"] if session else None, CHECKOUT_PENDING)
    frozen = {**compute_live_cart(session), "state": CHECKOUT_PENDING}
    session = _transition((IN_STORE,), CHECKOUT_PENDING, session_id=session["id"], final_cart=frozen,
                          stamp="quoted_at")
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
    session = _transition((CHECKOUT_PENDING,), PAID, session_id=session_id, end=True, stamp="approved_at")
    log_session_metrics(session)
    return session


def seconds_between(start_iso: str | None, end_iso: str | None) -> int | None:
    if not start_iso or not end_iso:
        return None
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return max(0, round((end - start).total_seconds()))


def log_session_metrics(session: dict[str, Any]) -> None:
    """Measured results: the four timestamps of a paid visit and the times between them."""
    eventlog.log("session_metrics", session_id=session["id"], member_id=session["member_id"],
                 entered_at=session["started_at"], first_pick_at=session["first_pick_at"],
                 quoted_at=session["quoted_at"], approved_at=session["approved_at"],
                 in_store_s=seconds_between(session["started_at"], session["approved_at"]),
                 exit_to_approval_s=seconds_between(session["quoted_at"], session["approved_at"]))


def end_return(session_id: str, reason: str) -> dict[str, Any]:
    """RETURNING -> CLOSED: refunded, or cancelled by the shopper (no refund)."""
    return _transition((RETURNING,), CLOSED, session_id=session_id, end=True, reason=reason)


def close(session_id: str, reason: str = "done") -> dict[str, Any]:
    """PAID -> CLOSED (receipt viewed / 60 s), or a shopping session with an empty cart -> CLOSED."""
    session = get_session(session_id)
    if session is not None and session["state"] in SHOPPING_STATES and cart_for(session)["items"]:
        raise StoreError("cart_not_empty", "Only an empty cart can leave without paying.")
    ended = session is not None and session["ended_at"] is not None
    return _transition((PAID, *SHOPPING_STATES), CLOSED, session_id=session_id, end=not ended, reason=reason)


def cancel(session_id: str | None = None, reason: str = "admin") -> dict[str, Any] | None:
    """IN_STORE / CHECKOUT_PENDING / RETURNING -> CANCELLED (admin reset / force-exit / timeout).
    None if nothing to cancel."""
    try:
        return _transition(ACTIVE_STATES, CANCELLED, session_id=session_id, end=True, reason=reason)
    except InvalidTransition:
        return None


def expire_stale_sessions(now: datetime | None = None) -> list[str]:
    """Cancel IN_STORE sessions older than store.max_session_minutes and RETURNING sessions older than
    RETURN_TIMEOUT_MINUTES (no refund). Returns cancelled ids."""
    now = now or datetime.now(timezone.utc)
    limits = {IN_STORE: timedelta(minutes=float(settings.store.get("max_session_minutes", 15))),
              RETURNING: timedelta(minutes=RETURN_TIMEOUT_MINUTES)}
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, state, started_at FROM store_sessions WHERE state IN (?, ?)",
                            (IN_STORE, RETURNING)).fetchall()
    finally:
        conn.close()
    expired = []
    for row in rows:
        if now - datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")) > limits[row["state"]]:
            if cancel(row["id"], reason="timeout"):
                expired.append(row["id"])
    return expired


def close_paid_sessions(now: datetime | None = None) -> list[str]:
    """PAID sessions whose payment is older than PAID_CLOSE_SECONDS -> CLOSED (receipt never opened)."""
    now = now or datetime.now(timezone.utc)
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, ended_at FROM store_sessions WHERE state = ?", (PAID,)).fetchall()
    finally:
        conn.close()
    closed = []
    for row in rows:
        paid_at = datetime.fromisoformat((row["ended_at"] or db.now_iso()).replace("Z", "+00:00"))
        if (now - paid_at).total_seconds() >= PAID_CLOSE_SECONDS:
            try:
                close(row["id"], reason="paid_timeout")
                closed.append(row["id"])
            except StoreError:
                pass  # closed by the receipt view in the meantime
    return closed


async def timeout_task() -> None:
    """Background loop started by main.py's lifespan (7.1 timeout, PAID -> CLOSED after 60 s)."""
    while True:
        await asyncio.sleep(TIMEOUT_CHECK_SECONDS)
        try:
            await asyncio.to_thread(expire_stale_sessions)
            await asyncio.to_thread(close_paid_sessions)
        except Exception as e:  # never let the checker die
            eventlog.log("timeout_task_error", error=repr(e))
