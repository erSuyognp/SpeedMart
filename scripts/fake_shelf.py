"""Dev tool: post fake shelf snapshots to /internal/shelf so the backend can run without a camera.

    python scripts/fake_shelf.py full            every unit in its home bay, all bays stable
    python scripts/fake_shelf.py remove <tag>    same as full, without that tag
    python scripts/fake_shelf.py cover <tag>     full shelf, but that tag is hidden (as under a hand or glare)
    python scripts/fake_shelf.py loop            send the shelf every 200 ms until Ctrl+C;
                                                 type a tag id + Enter to toggle it off/on the shelf,
                                                 c<tag> + Enter (e.g. c1) to toggle covering its tag

    --yolo   also send yolo_counts (F13) as a perfect YOLO would: every unit on the shelf is counted by
             SKU in its home bay, covered or not. With features.yolo true in config.json, covering a tag
             then leaves the cart unchanged; with it false, the backend counts tags only.

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


def load_unit_skus() -> dict[int, str]:
    """tag_id -> sku, from catalog.json."""
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    return {int(u["tag_id"]): u["sku"] for u in catalog["units"]}


def load_bay_ids() -> list[int]:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return [int(b["id"]) for b in config["bays"]]


def build_snapshot(units: dict[int, int], bay_ids: list[int], removed: set[int], frame_id: int,
                   covered: set[int] | None = None, unit_skus: dict[int, str] | None = None) -> dict:
    """Every unit not removed sits in its home bay. Covered tags are left out of units. With unit_skus
    (--yolo) each bay also gets yolo_counts of all its units, covered ones included."""
    covered = covered or set()
    bays = {b: [] for b in bay_ids}
    yolo: dict[int, dict[str, int]] = {b: {} for b in bay_ids}
    for tag, home in sorted(units.items()):
        if tag in removed:
            continue
        if tag not in covered:
            bays.setdefault(home, []).append(tag)
        if unit_skus is not None:
            counts = yolo.setdefault(home, {})
            counts[unit_skus[tag]] = counts.get(unit_skus[tag], 0) + 1
    return {
        "ts": int(time.time() * 1000),
        "frame_id": frame_id,
        "bays": [{"bay": b, "stable": True, "motion": False, "units": u,
                  "yolo_counts": yolo.get(b, {}) if unit_skus is not None else {}} for b, u in bays.items()],
        "loose_units": [],
    }


def post(client: httpx.Client, url: str, token: str, snapshot: dict) -> dict:
    r = client.post(f"{url}/internal/shelf", json=snapshot, headers={"X-Internal-Token": token}, timeout=0.5)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json()


def shelf_line(units: dict[int, int], removed: set[int], covered: set[int] | None = None) -> str:
    on = [t for t in sorted(units) if t not in removed]
    off = sorted(removed)
    line = f"on shelf: {on or '-'}   off shelf: {off or '-'}"
    hidden = sorted((covered or set()) - removed)
    return line + (f"   tags covered: {hidden}" if hidden else "")


def run_once(url: str, token: str, removed: set[int], covered: set[int] | None = None, yolo: bool = False) -> None:
    units, bay_ids = load_units(), load_bay_ids()
    skus = load_unit_skus() if yolo else None
    with httpx.Client() as client:
        result = post(client, url, token, build_snapshot(units, bay_ids, removed, 1, covered, skus))
    print(f"sent ({shelf_line(units, removed, covered)}{'  +yolo' if yolo else ''}) -> {result}")


def read_stdin(lines: queue.Queue) -> None:
    for line in sys.stdin:
        lines.put(line.strip())


def run_loop(url: str, token: str, yolo: bool = False) -> None:
    units, bay_ids = load_units(), load_bay_ids()
    skus = load_unit_skus() if yolo else None
    removed: set[int] = set()  # the shelf state kept between iterations
    covered: set[int] = set()
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=read_stdin, args=(lines,), daemon=True).start()

    print(f"Posting to {url}/internal/shelf every {int(LOOP_INTERVAL_S * 1000)} ms. "
          f"Type a tag id + Enter to toggle it, c<tag> to toggle covering it"
          f"{' (sending yolo_counts)' if yolo else ''}. Ctrl+C to stop.")
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
                cover = text[:1].lower() == "c"
                if cover:
                    text = text[1:].strip()
                try:
                    tag = int(text)
                except ValueError:
                    print(f"'{text}' is not a tag id. Known tags: {sorted(units)}")
                    continue
                if tag not in units:
                    print(f"Unknown tag {tag}. Known tags: {sorted(units)}")
                    continue
                if cover:
                    covered ^= {tag}
                    state = "COVERED" if tag in covered else "uncovered"
                    print(f"tag {tag} {state} -> {shelf_line(units, removed, covered)}")
                else:
                    removed ^= {tag}
                    print(f"tag {tag} {'OFF' if tag in removed else 'ON'} the shelf -> "
                          f"{shelf_line(units, removed, covered)}")

            frame_id += 1
            try:
                post(client, url, token, build_snapshot(units, bay_ids, removed, frame_id, covered, skus))
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
    parser.add_argument("--yolo", action="store_true", help="also send yolo_counts (F13), covered tags included")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("full", help="every unit in its home bay")
    rm = sub.add_parser("remove", help="full shelf without one tag")
    rm.add_argument("tag", type=int)
    cv = sub.add_parser("cover", help="full shelf with one tag hidden")
    cv.add_argument("tag", type=int)
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
            run_once(url, token, set(), yolo=args.yolo)
        elif args.cmd in ("remove", "cover"):
            if args.tag not in load_units():
                print(f"Unknown tag {args.tag}. Known tags: {sorted(load_units())}", file=sys.stderr)
                return 1
            if args.cmd == "remove":
                run_once(url, token, {args.tag}, yolo=args.yolo)
            else:
                run_once(url, token, set(), covered={args.tag}, yolo=args.yolo)
        else:
            run_loop(url, token, yolo=args.yolo)
    except KeyboardInterrupt:
        print("\nstopped")
    except (httpx.HTTPError, RuntimeError) as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
