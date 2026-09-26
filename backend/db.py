"""SQLite connection, schema creation (Section 6) and helpers."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from backend import eventlog
from backend.settings import DATA_DIR, settings

DB_PATH = DATA_DIR / "speedmart.db"

# Every member (the demo member too) gets Stripe's test Visa (pm_card_visa) or the mock that stands in for it.
# The label says so wherever a card is shown, so no judge mistakes it for their own card.
DEMO_CARD_LABEL = "Demo card · Visa test •••• 4242 · not your card"

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
  ended_at TEXT,
  return_of TEXT,
  first_pick_at TEXT,
  quoted_at TEXT,
  approved_at TEXT
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

CREATE TABLE IF NOT EXISTS refunds (
  id TEXT PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(id),
  store_session_id TEXT NOT NULL REFERENCES store_sessions(id),
  return_session_id TEXT NOT NULL REFERENCES store_sessions(id),
  member_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_ref TEXT,
  status TEXT NOT NULL,
  items_json TEXT NOT NULL,
  points_removed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  reason TEXT,
  dispute_id TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_session_id TEXT NOT NULL,
  bay INTEGER NOT NULL,
  kind TEXT NOT NULL,
  file TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  units_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
  id TEXT PRIMARY KEY,
  store_session_id TEXT NOT NULL REFERENCES store_sessions(id),
  member_id TEXT NOT NULL,
  sku TEXT NOT NULL,
  stage TEXT NOT NULL,
  outcome TEXT NOT NULL,
  status TEXT NOT NULL,
  amount_cents INTEGER,
  refund_id TEXT,
  evidence_json TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  review_json TEXT,
  decision_json TEXT
);

CREATE TABLE IF NOT EXISTS bank_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id TEXT NOT NULL REFERENCES members(id),
  type TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL,
  description TEXT NOT NULL,
  related_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_session_id TEXT NOT NULL,
  bay INTEGER NOT NULL,
  sku TEXT NOT NULL,
  file TEXT NOT NULL,
  keyframes_json TEXT NOT NULL,
  change_at TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  units_before_json TEXT NOT NULL,
  units_after_json TEXT NOT NULL,
  motion_json TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  fps REAL NOT NULL,
  frames INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""

# Columns added after the first release. init_db() adds any that an older data/speedmart.db is missing.
ADDED_COLUMNS = {
    "store_sessions": ("return_of TEXT", "first_pick_at TEXT", "quoted_at TEXT", "approved_at TEXT"),
    "refunds": ("reason TEXT", "dispute_id TEXT"),  # cart disputes (8.13): reason "return" | "dispute"
    "disputes": ("review_json TEXT", "decision_json TEXT"),  # AI review + staff / auto decision (8.14)
    # Tag free mode (vision.mode "yolo"): YOLO counts and boxes per crop, counts around each clip's change.
    "evidence": ("yolo_json TEXT",),
    "clips": ("counts_before_json TEXT", "counts_after_json TEXT"),
}


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
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column.split()[0] not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        # Members saved with an older label (or none, like the old demo member) show the demo card too.
        conn.execute("UPDATE members SET card_label = ? WHERE card_label IS NULL OR card_label <> ?",
                     (DEMO_CARD_LABEL, DEMO_CARD_LABEL))
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
            "INSERT INTO members (id, name, budget_usd, card_label, is_demo, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (member_id, "Demo Shopper", float(settings.store.get("default_budget_usd", 20)), DEMO_CARD_LABEL,
             now_iso()),
        )
        conn.commit()
        eventlog.log("demo_member_seeded", member_id=member_id)
    finally:
        conn.close()
