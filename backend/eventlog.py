"""Appends one JSON object per line to data/events.log.jsonl."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from backend.settings import DATA_DIR

EVENTS_PATH = DATA_DIR / "events.log.jsonl"
_lock = threading.Lock()


def log(event_type: str, **fields: Any) -> dict[str, Any]:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             "type": event_type, **fields}
    line = json.dumps(entry, default=str)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return entry
