"""Appends one JSON object per line to data/events.log.jsonl."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from backend.settings import DATA_DIR

EVENTS_PATH = DATA_DIR / "events.log.jsonl"
_lock = threading.Lock()
_subscribers: list[Callable[[dict[str, Any]], None]] = []


def subscribe(fn: Callable[[dict[str, Any]], None]) -> None:
    """Call fn(entry) after every log line (ws.py pushes them to admin sockets). fn must not block."""
    _subscribers.append(fn)


def log(event_type: str, **fields: Any) -> dict[str, Any]:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             "type": event_type, **fields}
    line = json.dumps(entry, default=str)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    for fn in _subscribers:
        try:
            fn(json.loads(line))
        except Exception:  # a broken listener must never break logging
            pass
    return entry


def tail(n: int = 50) -> list[dict[str, Any]]:
    """Last n entries, oldest first. Reads only the end of the file."""
    with _lock:
        if not EVENTS_PATH.exists():
            return []
        with EVENTS_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            raw = f.read().decode("utf-8", errors="replace")
    lines = raw.splitlines()
    if size > 256 * 1024:
        lines = lines[1:]  # first line may be cut in half
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
