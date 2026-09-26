"""Loads .env, config.json and catalog.json into typed settings. Fails fast on bad config."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
CONFIG_PATH = ROOT / "config.json"
CATALOG_PATH = ROOT / "catalog.json"
ENV_PATH = ROOT / ".env"


class SettingsError(Exception):
    """Raised when .env, config.json or catalog.json is missing or inconsistent."""


# config.json vision.mode: how the shelf is counted (9.4).
#   "tags"   ArUco tags only; yolo_counts in snapshots are ignored even with features.yolo on.
#   "yolo"   YOLO only, no tags at all: counts, stability, misplaced items and evidence come from YOLO boxes.
#   "fusion" tags + YOLO, max(tag_count, yolo_count) per bay per SKU (the default; tags only while features.yolo is off).
VISION_MODES = ("tags", "yolo", "fusion")
DEFAULT_VISION_MODE = "fusion"
DEFAULT_AUTO_REFUND_MAX_USD = 2.00  # config.json disputes.auto_refund_max_usd when the section is absent


def effective_vision_mode(mode: str, yolo_on: bool) -> str:
    """The mode the backend counts in: "yolo" and "fusion" need features.yolo, else they count tags only."""
    if mode == "tags" or not yolo_on:
        return "tags"
    return mode


@dataclass(frozen=True)
class Features:
    motion_freeze: bool
    signup: bool
    passkeys: bool
    gates: bool
    stripe: bool
    llm: bool
    hardware_leds: bool
    yolo: bool
    https_tunnel: bool
    loyalty: bool
    load_cells: bool
    voice: bool
    disputes: bool

    def as_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Env:
    session_secret: str
    public_origin: str
    rp_id: str
    rp_name: str
    internal_token: str
    admin_password: str
    entry_gate_token: str
    exit_gate_token: str
    stripe_secret_key: str
    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    review_provider: str  # AI dispute review (8.14): anthropic | openai; empty = same as llm_provider
    review_model: str  # empty = that provider's model (ANTHROPIC_MODEL / OPENAI_MODEL)
    elevenlabs_api_key: str
    elevenlabs_agent_id: str


@dataclass(frozen=True)
class Sku:
    sku: str
    name: str
    price_usd: float
    tags: list[str]
    yolo_class: str


@dataclass(frozen=True)
class Unit:
    tag_id: int
    sku: str
    home_bay: int


@dataclass(frozen=True)
class Bay:
    id: int
    roi: list[int]
    sku: str
    led_index: int


@dataclass(frozen=True)
class Settings:
    env: Env
    features: Features
    store: dict[str, Any]
    camera: dict[str, Any]
    vision: dict[str, Any]
    serial: dict[str, Any]
    bays: list[Bay]
    skus: dict[str, Sku]
    units: dict[int, Unit]
    disputes: dict[str, Any] = field(default_factory=lambda: {"auto_refund_max_usd": DEFAULT_AUTO_REFUND_MAX_USD})

    @property
    def vision_mode(self) -> str:
        """config.json vision.mode as written ("tags" | "yolo" | "fusion")."""
        return str(self.vision.get("mode", DEFAULT_VISION_MODE))

    @property
    def counting_mode(self) -> str:
        """The mode the shelf is actually counted in, given features.yolo (effective_vision_mode)."""
        return effective_vision_mode(self.vision_mode, self.features.yolo)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SettingsError(f"{path.name} not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path.name} is not valid JSON: {e}") from e


def _load_env() -> Env:
    load_dotenv(ENV_PATH)
    g = lambda k, d="": os.getenv(k, d).strip()  # noqa: E731
    env = Env(
        session_secret=g("SESSION_SECRET"),
        public_origin=g("PUBLIC_ORIGIN").rstrip("/"),
        rp_id=g("RP_ID"),
        rp_name=g("RP_NAME", "SpeedMart"),
        internal_token=g("INTERNAL_TOKEN"),
        admin_password=g("ADMIN_PASSWORD"),
        entry_gate_token=g("ENTRY_GATE_TOKEN"),
        exit_gate_token=g("EXIT_GATE_TOKEN"),
        stripe_secret_key=g("STRIPE_SECRET_KEY"),
        llm_provider=g("LLM_PROVIDER", "anthropic"),
        anthropic_api_key=g("ANTHROPIC_API_KEY"),
        anthropic_model=g("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        openai_api_key=g("OPENAI_API_KEY"),
        openai_model=g("OPENAI_MODEL"),
        openai_base_url=g("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        review_provider=g("REVIEW_PROVIDER").lower(),
        review_model=g("REVIEW_MODEL"),
        elevenlabs_api_key=g("ELEVENLABS_API_KEY"),
        elevenlabs_agent_id=g("ELEVENLABS_AGENT_ID"),
    )
    if not env.session_secret:
        raise SettingsError("SESSION_SECRET is not set. Copy .env.example to .env and fill it in.")
    return env


def _build(env: Env, config: dict[str, Any], catalog: dict[str, Any]) -> Settings:
    errors: list[str] = []

    for key in ("features", "store", "camera", "vision", "bays", "serial"):
        if key not in config:
            errors.append(f"config.json: missing top-level key '{key}'")
    for key in ("skus", "units"):
        if key not in catalog:
            errors.append(f"catalog.json: missing top-level key '{key}'")
    if errors:
        raise SettingsError("\n".join(errors))

    feature_names = [f.name for f in fields(Features)]
    missing = [n for n in feature_names if n not in config["features"]]
    if missing:
        raise SettingsError(f"config.json: features missing flags: {', '.join(missing)}")
    features = Features(**{n: bool(config["features"][n]) for n in feature_names})

    vision = dict(config["vision"])
    vision.setdefault("mode", DEFAULT_VISION_MODE)
    if vision["mode"] not in VISION_MODES:
        raise SettingsError(f"config.json: vision.mode {vision['mode']!r} is not one of {', '.join(VISION_MODES)}")

    try:
        skus = {s["sku"]: Sku(s["sku"], s["name"], float(s["price_usd"]), list(s.get("tags", [])), s.get("yolo_class", ""))
                for s in catalog["skus"]}
        units_list = [Unit(int(u["tag_id"]), u["sku"], int(u["home_bay"])) for u in catalog["units"]]
        bays = [Bay(int(b["id"]), list(b["roi"]), b["sku"], int(b["led_index"])) for b in config["bays"]]
    except (KeyError, TypeError, ValueError) as e:
        raise SettingsError(f"malformed entry in config.json bays or catalog.json skus/units: {e!r}") from e

    disputes = dict(config.get("disputes") or {})  # optional section: F20 dispute review settings
    disputes.setdefault("auto_refund_max_usd", DEFAULT_AUTO_REFUND_MAX_USD)
    line = disputes["auto_refund_max_usd"]
    if isinstance(line, bool) or not isinstance(line, (int, float)) or line < 0:
        errors.append(f"config.json: disputes.auto_refund_max_usd must be a dollar amount of 0 or more, got {line!r}")

    bay_ids = {b.id for b in bays}
    for b in bays:
        if b.sku not in skus:
            errors.append(f"config.json: bay {b.id} uses unknown SKU '{b.sku}' (known: {', '.join(skus)})")
    units: dict[int, Unit] = {}
    for u in units_list:
        if u.tag_id in units:
            errors.append(f"catalog.json: duplicate unit tag_id {u.tag_id}")
        units[u.tag_id] = u
        if u.sku not in skus:
            errors.append(f"catalog.json: unit tag_id {u.tag_id} uses unknown SKU '{u.sku}' (known: {', '.join(skus)})")
        if u.home_bay not in bay_ids:
            errors.append(f"catalog.json: unit tag_id {u.tag_id} has home_bay {u.home_bay}, "
                          f"which is not a bay id in config.json (known: {', '.join(map(str, sorted(bay_ids)))})")
    if errors:
        raise SettingsError("\n".join(errors))

    return Settings(
        env=env,
        features=features,
        store=config["store"],
        camera=config["camera"],
        vision=vision,
        serial=config["serial"],
        bays=bays,
        skus=skus,
        units=units,
        disputes=disputes,
    )


def load_settings() -> Settings:
    return _build(_load_env(), _read_json(CONFIG_PATH), _read_json(CATALOG_PATH))


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except SettingsError as e:
        lines = "\n".join(f"  - {line}" for line in str(e).splitlines())
        print(f"\n[settings] Startup aborted, configuration error:\n{lines}\n", file=sys.stderr)
        raise SystemExit(1) from None


settings = _load_or_exit()
