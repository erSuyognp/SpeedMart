"""End-to-end check of a running SpeedMart backend with no camera and no phone.

    python scripts\e2e_sim.py                    against http://127.0.0.1:8000
    python scripts\e2e_sim.py --url http://...   against somewhere else

Walks the whole demo loop the way a judge would, but driving the shelf over /internal/shelf instead of
picking things up: health -> admin login -> reset -> full shelf -> demo login -> start session -> pick one
-> put it back -> pick two -> quote -> approve -> receipt -> return one of the two (start return, put it back,
confirm refund) -> receipt shows the refund -> reset. Prices and tax come from catalog.json and
config.json, so the totals it asserts are the ones the catalog says, not ones copied from the backend.

A background thread keeps posting the shelf at 5 Hz for the whole run, so /api/health vision_age_ms stays
fresh and store.start_session() can take a baseline (it refuses a snapshot older than 2 s).

Prints PASS / FAIL / SKIP per step and exits non-zero if anything FAILed. SKIP is for things this build
cannot do yet: an endpoint from Section 8.1 that another step has not written (404), or a step gated off
by a feature flag. Reads INTERNAL_TOKEN and ADMIN_PASSWORD from .env (or the environment).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
POST_HZ = 5.0
SETTLE_TIMEOUT_S = 5.0  # how long to wait for the cart to catch up with the shelf
HTTP_TIMEOUT_S = 5.0


# --- outcome plumbing ---

class Fail(Exception):
    """Step did not do what the spec says it should."""


class Skip(Exception):
    """Step cannot run in this build (endpoint not written yet, or feature flag off)."""


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.skipped = 0
        self.blocked = False  # a FAIL happened; later steps are reported but not run

    def line(self, status: str, name: str, detail: str = "") -> None:
        # ASCII only: the Windows console is cp1252 and turns anything fancier into "?".
        print(f"{status:<4} {name}" + (f"  -  {detail}" if detail else ""), flush=True)

    def run(self, name, fn, *args, **kwargs):
        if self.blocked:
            self.skipped += 1
            self.line("SKIP", name, "an earlier step failed")
            return None
        try:
            result = fn(*args, **kwargs)
        except Skip as e:
            self.skipped += 1
            self.line("SKIP", name, str(e))
            return None
        except Fail as e:
            self.failed += 1
            self.blocked = True
            self.line("FAIL", name, str(e))
            return None
        except Exception as e:  # a crash is a failure, not a traceback dumped on the operator
            self.failed += 1
            self.blocked = True
            self.line("FAIL", name, f"{type(e).__name__}: {e}")
            return None
        # A step returns its one-line detail, or (detail, value) when a later step needs the value.
        detail, value = result if isinstance(result, tuple) else (result or "", None)
        self.line("PASS", name, detail)
        return value if value is not None else True


# --- catalog / config: the source of truth for the totals we assert ---

def load_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class Catalog:
    def __init__(self) -> None:
        catalog = load_json("catalog.json")
        config = load_json("config.json")
        self.price_cents = {s["sku"]: to_cents(s["price_usd"]) for s in catalog["skus"]}
        self.name = {s["sku"]: s["name"] for s in catalog["skus"]}
        self.units = {int(u["tag_id"]): (u["sku"], int(u["home_bay"])) for u in catalog["units"]}
        self.bay_ids = [int(b["id"]) for b in config["bays"]]
        self.sku_order = [s["sku"] for s in catalog["skus"]]  # catalog order; the sim never names a SKU itself
        self.tax_rate = config["store"]["tax_rate"]

    def tag_for(self, sku: str, skip: set[int] = frozenset()) -> int:
        for tag, (unit_sku, _) in sorted(self.units.items()):
            if unit_sku == sku and tag not in skip:
                return tag
        raise Fail(f"catalog.json has no spare unit of SKU {sku}")

    def totals(self, qty: dict[str, int]) -> tuple[int, int, int]:
        """(subtotal, tax, total) in cents, computed exactly as backend/cart.py does (7.3)."""
        subtotal = sum(self.price_cents[sku] * n for sku, n in qty.items())
        tax = int((Decimal(subtotal) * Decimal(str(self.tax_rate))).quantize(Decimal("1"),
                                                                            rounding=ROUND_HALF_UP))
        return subtotal, tax, subtotal + tax


def to_cents(usd) -> int:
    return int((Decimal(str(usd)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


# --- the shelf, posted continuously in the background ---

class ShelfPoster(threading.Thread):
    """Posts the full shelf minus `removed` to /internal/shelf at POST_HZ until stopped."""

    daemon = True

    def __init__(self, url: str, token: str, catalog: Catalog) -> None:
        super().__init__(name="shelf-poster")
        self.url = url
        self.token = token
        self.catalog = catalog
        self.removed: set[int] = set()
        self.frame_id = 0
        self.last_error: str | None = None
        self.posts = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def set_removed(self, tags: set[int]) -> None:
        with self._lock:
            self.removed = set(tags)

    def snapshot(self) -> dict:
        with self._lock:
            removed = set(self.removed)
        self.frame_id += 1
        bays: dict[int, list[int]] = {b: [] for b in self.catalog.bay_ids}
        for tag, (_, home) in sorted(self.catalog.units.items()):
            if tag not in removed:
                bays.setdefault(home, []).append(tag)
        return {
            "ts": int(time.time() * 1000),
            "frame_id": self.frame_id,
            "bays": [{"bay": b, "stable": True, "motion": False, "units": u, "yolo_counts": {}}
                     for b, u in sorted(bays.items())],
            "loose_units": sorted(removed),
        }

    def post_once(self, client: httpx.Client) -> dict:
        r = client.post(f"{self.url}/internal/shelf", json=self.snapshot(),
                        headers={"X-Internal-Token": self.token}, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise Fail(f"POST /internal/shelf returned HTTP {r.status_code}: {r.text[:200]}")
        self.posts += 1
        return r.json()

    def run(self) -> None:
        interval = 1.0 / POST_HZ
        with httpx.Client() as client:
            while not self._stop.is_set():
                try:
                    self.post_once(client)
                except Exception as e:  # keep the shelf alive; the step assertions report the damage
                    self.last_error = f"{type(e).__name__}: {e}"
                self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()


# --- HTTP helpers ---

class Api:
    def __init__(self, url: str) -> None:
        self.url = url
        self.client = httpx.Client(base_url=url, timeout=HTTP_TIMEOUT_S, follow_redirects=False)

    def call(self, method: str, path: str, json_body=None, *, skip_on_404: str | None = None) -> dict:
        try:
            r = self.client.request(method, path, json=json_body)
        except httpx.HTTPError as e:
            raise Fail(f"cannot reach {self.url}{path}: {e}") from e
        if r.status_code == 404 and skip_on_404:
            raise Skip(f"{method} {path} returned 404: {skip_on_404}")
        if r.status_code >= 400:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            raise Fail(f"{method} {path} -> HTTP {r.status_code} "
                       f"{body.get('error', '')} {body.get('message', r.text[:160])}".strip())
        return r.json() if r.content else {}

    def get(self, path: str, **kw) -> dict:
        return self.call("GET", path, **kw)

    def post(self, path: str, body=None, **kw) -> dict:
        return self.call("POST", path, body if body is not None else {}, **kw)

    def close(self) -> None:
        self.client.close()


def cart_quantities(cart: dict) -> dict[str, int]:
    return {i["sku"]: i["qty"] for i in cart.get("items", []) if i["qty"]}


def wait_for_cart(api: Api, want: dict[str, int], what: str) -> dict:
    """Poll /api/store/current until the cart matches `want`, or give up after SETTLE_TIMEOUT_S."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    last: dict[str, int] | None = None
    while time.monotonic() < deadline:
        current = api.get("/api/store/current")
        cart = current.get("cart")
        if cart is not None:
            last = cart_quantities(cart)
            if last == want:
                return cart
        time.sleep(0.15)
    raise Fail(f"after {SETTLE_TIMEOUT_S:.0f}s the cart is {last} but {what} means it should be {want}")


def check_totals(catalog: Catalog, cart: dict, want: dict[str, int]) -> str:
    """Compare the cart's money against catalog.json prices and the config.json tax rate."""
    subtotal, tax, total = catalog.totals(want)
    got = (to_cents(cart["subtotal_usd"]), to_cents(cart["tax_usd"]), to_cents(cart["total_usd"]))
    if got != (subtotal, tax, total):
        raise Fail(f"totals are subtotal {usd(got[0])} tax {usd(got[1])} total {usd(got[2])}, "
                   f"but the catalog says {usd(subtotal)} / {usd(tax)} / {usd(total)}")
    names = ", ".join(f"{want[s]}x {catalog.name[s]}" for s in sorted(want))
    return f"{names}: subtotal {usd(subtotal)} + tax {usd(tax)} = {usd(total)}"


# --- the steps ---

def step_health(api: Api) -> str:
    health = api.get("/api/health")
    if not health.get("ok"):
        raise Fail(f"/api/health did not report ok: {health}")
    return f"vision_age_ms={health.get('vision_age_ms')} serial={health.get('serial')}"


def step_admin_login(api: Api, password: str) -> str:
    if not password:
        raise Fail("ADMIN_PASSWORD is not set in .env or the environment")
    api.post("/admin/login", {"password": password})
    return "admin cookie set"


def step_reset(api: Api) -> str:
    result = api.post("/admin/reset")
    cancelled = result.get("cancelled_session_id")
    return f"cancelled {cancelled}" if cancelled else "store was already free"


def step_full_shelf(poster: ShelfPoster, api: Api) -> str:
    poster.set_removed(set())
    poster.start()
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        age = api.get("/api/health").get("vision_age_ms", -1)
        if 0 <= age < 1000 and poster.posts >= 2:
            return (f"{len(poster.catalog.units)} units on the shelf, posting at {POST_HZ:.0f} Hz, "
                    f"vision_age_ms={age}")
        time.sleep(0.2)
    raise Fail(f"the shelf never went fresh (last error: {poster.last_error})")


def step_demo_login(api: Api) -> str:
    result = api.post("/admin/demo-login")
    member = result.get("member", {})
    return f"signed in as {member.get('name', '?')} ({member.get('id', '?')})"


def step_start_session(api: Api) -> tuple[str, dict]:
    # /api/dev/start is the gates-off fallback, and admins may call it even when gates are on (8.3 / S1.3).
    result = api.post("/api/dev/start", skip_on_404="the dev start route is not available")
    session = result.get("session") or {}
    baseline = session.get("baseline", {})
    if not baseline:
        raise Fail(f"session started with an empty baseline: {session}")
    return f"{session.get('id')} state={session.get('state')} baseline={baseline}", session


def step_pick_one(api: Api, poster: ShelfPoster, catalog: Catalog, sku: str) -> str:
    tag = catalog.tag_for(sku)
    poster.set_removed({tag})
    want = {sku: 1}
    cart = wait_for_cart(api, want, f"unit {tag} ({catalog.name[sku]}) is off the shelf")
    return f"tag {tag} removed. " + check_totals(catalog, cart, want)


def step_put_back(api: Api, poster: ShelfPoster, catalog: Catalog) -> str:
    poster.set_removed(set())
    cart = wait_for_cart(api, {}, "everything is back on the shelf")
    if to_cents(cart["total_usd"]) != 0:
        raise Fail(f"cart is empty but the total is {cart['total_usd']}")
    return "cart empty, total $0.00"


def step_pick_two(api: Api, poster: ShelfPoster, catalog: Catalog, skus: tuple[str, str]) -> tuple[str, dict]:
    tags = {catalog.tag_for(s) for s in skus}
    poster.set_removed(tags)
    want = {s: 1 for s in skus}
    cart = wait_for_cart(api, want, f"units {sorted(tags)} are off the shelf")
    return f"tags {sorted(tags)} removed. " + check_totals(catalog, cart, want), cart


def step_quote(api: Api, catalog: Catalog, gates_on: bool, exit_token: str, want: dict[str, int]) -> tuple[str, dict]:
    if gates_on:
        # 8.1 gate/exit/quote. Written by S3.4; until then this is a 404 and the step skips.
        result = api.post("/api/gate/exit/quote", {"gate_token": exit_token},
                          skip_on_404="gate/exit/quote is not written yet (S3.4)")
    else:
        result = api.post("/api/dev/checkout", skip_on_404="the dev checkout route is not available")
    cart = result.get("cart") or {}
    if cart.get("state") != "CHECKOUT_PENDING":
        raise Fail(f"after the quote the session state is {cart.get('state')}, expected CHECKOUT_PENDING")
    if cart_quantities(cart) != want:
        raise Fail(f"the frozen cart is {cart_quantities(cart)}, expected {want}")
    detail = check_totals(catalog, cart, want)
    if "instruction" in result:
        detail += f", instruction {result['instruction'].get('instruction_id', '?')}"
    return f"cart frozen. {detail}", cart


def step_approve(api: Api, catalog: Catalog, passkeys_on: bool, want: dict[str, int]) -> str:
    if passkeys_on:
        raise Skip("features.passkeys is true, so approval needs a real Face ID prompt on a phone")
    # 8.1 gate/exit/approve. Written by S4.1; until then this is a 404 and the step skips.
    result = api.post("/api/gate/exit/approve",
                      skip_on_404="gate/exit/approve is not written yet (S4.1)")
    payment = result.get("payment") or {}
    if payment.get("status") != "AUTHORIZED":
        raise Fail(f"payment status is {payment.get('status')!r}, expected AUTHORIZED: {payment}")
    _, _, total = catalog.totals(want)
    if to_cents(payment.get("amount_usd", 0)) != total:
        raise Fail(f"charged {payment.get('amount_usd')} but the cart total is {usd(total)}")
    return (f"{payment.get('payment_id')} {usd(total)} via {payment.get('provider')} "
            f"auth {payment.get('auth_code')}")


def step_receipt(api: Api, session_id: str) -> str:
    """Measured results on the receipt, and the return offer (Continue stage)."""
    r = api.get(f"/api/receipt/{session_id}")
    if not r.get("paid"):
        raise Fail(f"the receipt is not paid: {r}")
    if r.get("in_and_out_s") is None or r.get("approvals") != 1:
        raise Fail(f"expected 'In and out in N seconds' and 1 tap, got in_and_out_s={r.get('in_and_out_s')} "
                   f"approvals={r.get('approvals')}")
    if not (r.get("return") or {}).get("eligible"):
        raise Fail(f"a fresh paid visit should be returnable: {r.get('return')}")
    return f"in and out in {r['in_and_out_s']} s, {r['approvals']} tap to pay, returnable"


def step_start_return(api: Api, session_id: str, passkeys_on: bool) -> str:
    if passkeys_on:
        raise Skip("features.passkeys is true, so starting a return needs a real Face ID prompt on a phone")
    ret = api.post("/api/returns/start", {"session_id": session_id},
                   skip_on_404="the returns routes are not written yet").get("return") or {}
    if ret.get("state") != "RETURNING" or ret.get("items"):
        raise Fail(f"expected a RETURNING session with nothing detected yet, got {ret}")
    return f"{ret.get('session_id')} RETURNING, returnable {[(i['sku'], i['qty']) for i in ret['returnable']]}"


def step_put_one_back(api: Api, poster: ShelfPoster, catalog: Catalog, bought: tuple[str, str]) -> str:
    """Put the first SKU back on its bay; the camera (here: the poster) must show it as a detected return."""
    back, kept = bought
    poster.set_removed({catalog.tag_for(kept)})
    want = {back: 1}
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        ret = api.get("/api/returns/current").get("return") or {}
        last = {i["sku"]: i["qty"] for i in ret.get("items", [])}
        if last == want:
            _, _, total = catalog.totals(want)
            if to_cents(ret["total_usd"]) != total:
                raise Fail(f"refund shows {ret['total_usd']} but 1 {catalog.name[back]} with tax is {usd(total)}")
            return f"{catalog.name[back]} detected back on the shelf, refund {usd(total)}"
        time.sleep(0.15)
    raise Fail(f"after {SETTLE_TIMEOUT_S:.0f}s the detected return is {last}, expected {want}")


def step_confirm_refund(api: Api, catalog: Catalog, sku: str) -> str:
    result = api.post("/api/returns/confirm")
    refund = result.get("refund") or {}
    _, _, total = catalog.totals({sku: 1})
    if refund.get("status") != "SUCCEEDED" or to_cents(refund.get("amount_usd", 0)) != total:
        raise Fail(f"expected a SUCCEEDED refund of {usd(total)}, got {refund}")
    if "is on its way to" not in (result.get("agent_line") or ""):
        raise Fail(f"agent line missing: {result.get('agent_line')!r}")
    return f"{refund.get('refund_id')} {usd(total)} via {refund.get('provider')} ({refund.get('provider_ref')})"


def step_receipt_refund(api: Api, session_id: str) -> str:
    r = api.get(f"/api/receipt/{session_id}")
    refunds = r.get("refunds") or []
    if len(refunds) != 1:
        raise Fail(f"the receipt lists {len(refunds)} refunds, expected 1")
    state = api.get("/admin/state")
    if state["lock"]["occupied"]:
        raise Fail("the store is still locked after the refund")
    m = state.get("metrics") or {}
    if m.get("sessions_today", 0) < 1 or m.get("refunds_today", 0) < 1:
        raise Fail(f"admin metrics did not count this visit: {m}")
    return (f"Refunded {usd(to_cents(refunds[0]['amount_usd']))} for {refunds[0]['items_text']}; "
            f"metrics: {m['sessions_today']} sessions, {m['refunds_today']} refunds today")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="backend base URL")
    args = parser.parse_args()
    url = args.url.rstrip("/")

    load_dotenv(ROOT / ".env")
    internal_token = os.getenv("INTERNAL_TOKEN", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()

    catalog = Catalog()
    api = Api(url)
    poster = ShelfPoster(url, internal_token, catalog)
    report = Report()

    print(f"SpeedMart end-to-end simulation against {url}\n")
    started = time.monotonic()
    try:
        if not internal_token:
            report.failed += 1
            report.blocked = True
            report.line("FAIL", "read .env", "INTERNAL_TOKEN is not set in .env or the environment")
        else:
            report.line("PASS", "read .env", "INTERNAL_TOKEN and ADMIN_PASSWORD loaded")

        report.run("health", step_health, api)
        report.run("admin login", step_admin_login, api, admin_password)
        report.run("admin reset", step_reset, api)
        report.run("shelf full", step_full_shelf, poster, api)

        flags = {}
        if not report.blocked:
            flags = api.get("/api/config/public").get("features", {})
        gates_on = bool(flags.get("gates"))
        passkeys_on = bool(flags.get("passkeys"))

        report.run("demo login", step_demo_login, api)
        report.run("start session", step_start_session, api)
        first = catalog.sku_order[0]
        report.run(f"pick 1 {catalog.name[first].lower()}", step_pick_one, api, poster, catalog, first)
        report.run("put it back", step_put_back, api, poster, catalog)

        # First and last catalog SKUs, so the pair spans the shelf however many bays there are.
        two = (first, catalog.sku_order[-1])
        want_two = {s: 1 for s in two}
        report.run("pick 2 of different SKUs", step_pick_two, api, poster, catalog, two)
        quoted = report.run("checkout quote", step_quote, api, catalog, gates_on,
                            os.getenv("EXIT_GATE_TOKEN", "").strip(), want_two)
        paid = None
        if quoted is None and not report.blocked:
            report.skipped += 1
            report.line("SKIP", "approve payment", "there is nothing quoted to approve")
        else:
            paid = report.run("approve payment", step_approve, api, catalog, passkeys_on, want_two)

        # Continue stage: put one item back and get it refunded, verified by the shelf.
        if paid and isinstance(quoted, dict):
            sid = quoted["session_id"]
            report.run("receipt: time and taps", step_receipt, api, sid)
            returning = report.run("start return", step_start_return, api, sid, passkeys_on)
            if returning:
                report.run(f"put 1 {catalog.name[first].lower()} back", step_put_one_back, api, poster, catalog, two)
                report.run("confirm refund", step_confirm_refund, api, catalog, first)
                report.run("receipt shows the refund", step_receipt_refund, api, sid)
        elif not report.blocked:
            report.skipped += 1
            report.line("SKIP", "return and refund", "nothing was paid, so there is nothing to return")
    finally:
        poster.stop()
        # The final reset runs even after a failure, so the next run starts from a free store.
        try:
            api.post("/admin/reset")
            report.line("PASS", "admin reset (cleanup)", "store free, overrides cleared")
        except Exception as e:
            message = str(e) or type(e).__name__
            if report.blocked:
                # The run already failed for a reason that is reported above; do not count it twice.
                report.skipped += 1
                report.line("SKIP", "admin reset (cleanup)", "an earlier step failed")
            else:
                report.failed += 1
                report.line("FAIL", "admin reset (cleanup)", message)
        api.close()

    elapsed = time.monotonic() - started
    print(f"\n{report.failed} failed, {report.skipped} skipped, {poster.posts} shelf snapshots "
          f"posted in {elapsed:.1f}s")
    if poster.last_error:
        print(f"last shelf poster error: {poster.last_error}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
