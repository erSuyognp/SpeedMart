"""Dev tool: post fake shelf snapshots to /internal/shelf so the backend can run without a camera.

    python scripts/fake_shelf.py full            every unit in its home bay, all bays stable
    python scripts/fake_shelf.py remove <tag>    same as full, without that tag
    python scripts/fake_shelf.py loop            send the shelf every 200 ms until Ctrl+C;
                                                 type a tag id + Enter to toggle it off/on the shelf

Reads INTERNAL_TOKEN from .env. Backend URL defaults to http://127.0.0.1:8000 (override with --url).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LOOP_INTERVAL_S = 0.2


def load_units() -> dict[int, int]:
    """tag_id -> home_bay, from catalog.json."""
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    return {int(u["tag_id"]): int(u["home_bay"]) for u in catalog["units"]}


def load_bay_ids() -> list[int]:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return [int(b["id"]) for b in config["bays"]]


def build_snapshot(units: dict[int, int], bay_ids: list[int], removed: set[int], frame_id: int) -> dict:
    bays = {b: [] for b in bay_ids}
    for tag, home in sorted(units.items()):
        if tag not in removed:
            bays.setdefault(home, []).append(tag)
    return {
        "ts": int(time.time() * 1000),
        "frame_id": frame_id,
        "bays": [{"bay": b, "stable": True, "motion": False, "units": u, "yolo_counts": {}} for b, u in bays.items()],
        "loose_units": [],
    }


def post(client: httpx.Client, url: str, token: str, snapshot: dict) -> dict:
    r = client.post(f"{url}/internal/shelf", json=snapshot, headers={"X-Internal-Token": token}, timeout=0.5)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json()


def shelf_line(units: dict[int, int], removed: set[int]) -> str:
    on = [t for t in sorted(units) if t not in removed]
    off = sorted(removed)
    return f"on shelf: {on or '-'}   off shelf: {off or '-'}"


def run_once(url: str, token: str, removed: set[int]) -> None:
    units, bay_ids = load_units(), load_bay_ids()
    with httpx.Client() as client:
        result = post(client, url, token, build_snapshot(units, bay_ids, removed, frame_id=1))
    print(f"sent ({shelf_line(units, removed)}) -> {result}")


def read_stdin(lines: queue.Queue) -> None:
    for line in sys.stdin:
        lines.put(line.strip())


def run_loop(url: str, token: str) -> None:
    units, bay_ids = load_units(), load_bay_ids()
    removed: set[int] = set()  # the shelf state kept between iterations
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=read_stdin, args=(lines,), daemon=True).start()

    print(f"Posting to {url}/internal/shelf every {int(LOOP_INTERVAL_S * 1000)} ms. "
          f"Type a tag id + Enter to toggle it. Ctrl+C to stop.")
    print(shelf_line(units, removed))
    frame_id = 0
    backend_ok: bool | None = None
    with httpx.Client() as client:
        while True:
            started = time.monotonic()
            while not lines.empty():
                text = lines.get()
                if not text:
                    continue
                try:
                    tag = int(text)
                except ValueError:
                    print(f"'{text}' is not a tag id. Known tags: {sorted(units)}")
                    continue
                if tag not in units:
                    print(f"Unknown tag {tag}. Known tags: {sorted(units)}")
                    continue
                removed ^= {tag}
                print(f"tag {tag} {'OFF' if tag in removed else 'ON'} the shelf -> {shelf_line(units, removed)}")

            frame_id += 1
            try:
                post(client, url, token, build_snapshot(units, bay_ids, removed, frame_id))
                if backend_ok is not True:
                    print("backend reachable, snapshots flowing")
                backend_ok = True
            except (httpx.HTTPError, RuntimeError) as e:
                if backend_ok is not False:  # print once per outage, not every 200 ms
                    print(f"backend unreachable ({e}); retrying...")
                backend_ok = False
            time.sleep(max(0.0, LOOP_INTERVAL_S - (time.monotonic() - started)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Post fake shelf snapshots (no camera needed).")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="backend base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("full", help="every unit in its home bay")
    rm = sub.add_parser("remove", help="full shelf without one tag")
    rm.add_argument("tag", type=int)
    sub.add_parser("loop", help="send every 200 ms; type a tag id + Enter to toggle it")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    token = os.getenv("INTERNAL_TOKEN", "").strip()
    if not token:
        print("INTERNAL_TOKEN is not set in .env", file=sys.stderr)
        return 1
    url = args.url.rstrip("/")

    try:
        if args.cmd == "full":
            run_once(url, token, set())
        elif args.cmd == "remove":
            if args.tag not in load_units():
                print(f"Unknown tag {args.tag}. Known tags: {sorted(load_units())}", file=sys.stderr)
                return 1
            run_once(url, token, {args.tag})
        else:
            run_loop(url, token)
    except KeyboardInterrupt:
        print("\nstopped")
    except (httpx.HTTPError, RuntimeError) as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
