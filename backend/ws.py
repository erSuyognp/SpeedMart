"""WebSocket connection manager + broadcast (8.4).

Each socket is tagged with the member id from the session cookie, or "admin". publish() is thread-safe and
never blocks: store.py calls it from request threads, the event loop and the timeout task alike. Every
socket has its own queue and writer task, so messages arrive in order and a slow phone never delays others.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend import eventlog

ADMIN = "admin"


@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    tag: str | None  # member id, ADMIN, or None (not signed in: store_status only)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class ConnectionManager:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conns: set[Connection] = set()  # only touched on the event loop

    def bind(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Called from main.py's lifespan. Until bound (e.g. unit tests), publish() is a no-op."""
        self._loop = loop

    def publish(self, msg: dict[str, Any], *, member_id: str | None = None, admin: bool = False,
                everyone: bool = False) -> None:
        loop = self._loop
        if loop is None:
            return
        with suppress(RuntimeError):  # loop already closed during shutdown
            loop.call_soon_threadsafe(self._dispatch, msg, member_id, admin, everyone)

    def _dispatch(self, msg: dict[str, Any], member_id: str | None, admin: bool, everyone: bool) -> None:
        for conn in self._conns:
            if everyone or (admin and conn.tag == ADMIN) or (member_id is not None and conn.tag == member_id):
                conn.queue.put_nowait(msg)

    def count(self) -> dict[str, int]:
        admins = sum(1 for c in self._conns if c.tag == ADMIN)
        members = sum(1 for c in self._conns if c.tag not in (None, ADMIN))
        return {"admin": admins, "member": members, "anonymous": len(self._conns) - admins - members}

    async def serve(self, websocket: WebSocket, tag: str | None, initial: list[dict[str, Any]]) -> None:
        conn = Connection(websocket, tag)
        for msg in initial:
            conn.queue.put_nowait(msg)
        self._conns.add(conn)
        eventlog.log("ws_connect", tag=tag)
        writer = asyncio.create_task(self._writer(conn))
        try:
            while True:  # clients send nothing meaningful; this just notices the disconnect
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self._conns.discard(conn)
            writer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await writer
            eventlog.log("ws_disconnect", tag=tag)

    @staticmethod
    async def _writer(conn: Connection) -> None:
        while True:
            msg = await conn.queue.get()
            await conn.websocket.send_json(msg)


manager = ConnectionManager()


# --- helpers used by store.py (message shapes from 8.4) ---

def broadcast_cart(member_id: str, snapshot: dict[str, Any]) -> None:
    manager.publish({"type": "cart", "data": snapshot}, member_id=member_id, admin=True)


def broadcast_gate(member_id: str, event: str) -> None:
    manager.publish({"type": "gate", "data": {"event": event}}, member_id=member_id, admin=True)


def broadcast_store_status(occupied: bool) -> None:
    manager.publish({"type": "store_status", "data": {"occupied": occupied}}, everyone=True)


def broadcast_plan_bays(bays: list[int]) -> None:
    """Public: the bays the current plan points at ([] when cleared). Bay ids only, never who asked."""
    manager.publish({"type": "plan_bays", "data": {"bays": list(bays)}}, everyone=True)


def broadcast_admin(msg: dict[str, Any]) -> None:
    manager.publish(msg, admin=True)


def _log_to_admins(entry: dict[str, Any]) -> None:
    broadcast_admin({"type": "log", "data": entry})


eventlog.subscribe(_log_to_admins)


router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    from backend import intent, shelf_state, store  # local import: store imports this module

    await websocket.accept()
    session = websocket.session
    if websocket.query_params.get("role") == ADMIN and session.get("admin"):
        tag: str | None = ADMIN
    else:
        tag = session.get("member_id")

    # First messages bring a (re)connected client up to date without waiting for the next change.
    current = await asyncio.to_thread(store.current_session)
    initial: list[dict[str, Any]] = [{"type": "store_status", "data": {"occupied": current is not None}}]
    if current is not None and tag in (ADMIN, current["member_id"]):
        initial.append({"type": "cart", "data": await asyncio.to_thread(store.cart_for, current)})
    if tag == ADMIN:
        initial.append({"type": "shelf", "data": shelf_state.state()})
    glowing = intent.shown_bays()
    if glowing:  # clients start with nothing glowing, so an empty list needs no message
        initial.append({"type": "plan_bays", "data": {"bays": glowing}})
    await manager.serve(websocket, tag, initial)
