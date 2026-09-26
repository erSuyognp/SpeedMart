"""SQLite connection, schema creation (Section 6) and helpers."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from backend import eventlog
from backend.settings import DATA_DIR, settings

DB_PATH = DATA_DIR / "aisle.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  budget_usd REAL NOT NULL,
  dietary TEXT,
  stripe_customer_id TEXT,
  stripe_pm_id TEXT,
  card_label TEXT,
  points INTEGER NOT NULL DEFAULT 0,
  is_demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passkeys (
  credential_id TEXT PRIMARY KEY,
  member_id TEXT NOT NULL REFERENCES members(id),
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL,
  transports TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_sessions (
  id TEXT PRIMARY KEY,
  member_id TEXT NOT NULL REFERENCES members(id),
  state TEXT NOT NULL,
  baseline_json TEXT NOT NULL,
  final_cart_json TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT
);

CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,
  store_session_id TEXT NOT NULL REFERENCES store_sessions(id),
  member_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_ref TEXT,
  status TEXT NOT NULL,
  auth_code TEXT,
  instruction_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def seed_demo_member() -> None:
    """Create the demo member if none exists. Stripe card linking is added in S4.2 (F10)."""
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM members WHERE is_demo = 1").fetchone():
            return
        member_id = new_id("mem")
        conn.execute(
            "INSERT INTO members (id, name, budget_usd, is_demo, created_at) VALUES (?, ?, ?, 1, ?)",
            (member_id, "Demo Shopper", float(settings.store.get("default_budget_usd", 20)), now_iso()),
        )
        conn.commit()
        eventlog.log("demo_member_seeded", member_id=member_id)
    finally:
        conn.close()
