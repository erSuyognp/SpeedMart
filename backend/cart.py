"""Baseline vs shelf state -> cart, totals, tax (7.3). All money math in integer cents."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from backend.db import now_iso
from backend.settings import settings


def to_cents(usd: float | int | str) -> int:
    return int((Decimal(str(usd)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_usd(cents: int) -> float:
    return float(Decimal(cents) / 100)


def tax_cents(subtotal_cents: int, tax_rate: float) -> int:
    return int((Decimal(subtotal_cents) * Decimal(str(tax_rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cart_quantities(baseline: dict[str, int], shelf_counts: dict[str, int], overrides: dict[str, int]) -> dict[str, int]:
    """cart_qty = clamp(baseline - shelf_now + override, 0, baseline) for every SKU in the baseline."""
    qty = {}
    for sku, base in baseline.items():
        raw = base - shelf_counts.get(sku, 0) + overrides.get(sku, 0)
        qty[sku] = max(0, min(base, raw))
    return qty


def compute_cart(session: dict[str, Any], shelf_counts: dict[str, int], overrides: dict[str, int],
                 member: dict[str, Any], misplaced: list[dict[str, Any]] | None = None,
                 agent_line: str = "") -> dict[str, Any]:
    """Build a CartSnapshot (8.5). Computed from scratch every call, never accumulated."""
    qty = cart_quantities(session["baseline"], shelf_counts, overrides)
    return priced_cart(session, qty, member, misplaced, agent_line)


def priced_cart(session: dict[str, Any], qty: dict[str, int], member: dict[str, Any],
                misplaced: list[dict[str, Any]] | None = None, agent_line: str = "") -> dict[str, Any]:
    """CartSnapshot (8.5) for these quantities per SKU: prices, tax and totals in integer cents."""
    items = []
    subtotal = 0
    for sku, s in settings.skus.items():  # catalog order
        n = qty.get(sku, 0)
        if n <= 0:
            continue
        unit = to_cents(s.price_usd)
        line = unit * n
        subtotal += line
        items.append({"sku": sku, "name": s.name, "qty": n,
                      "unit_price_usd": to_usd(unit), "line_total_usd": to_usd(line)})

    tax = tax_cents(subtotal, settings.store["tax_rate"])
    total = subtotal + tax
    budget_usd = member.get("budget_usd", settings.store.get("default_budget_usd", 20))

    warnings = [{"kind": "misplaced", "sku": m["sku"], "name": m["name"], "bay": m["bay"],
                 "tag_id": m["tag_id"], "message": f"{m['name']} is in the wrong bay"}
                for m in (misplaced or [])]

    return {
        "session_id": session["id"],
        "state": session["state"],
        "items": items,
        "subtotal_usd": to_usd(subtotal),
        "tax_usd": to_usd(tax),
        "total_usd": to_usd(total),
        "budget_usd": budget_usd,
        "over_budget": total > to_cents(budget_usd),
        "warnings": warnings,
        "agent_line": agent_line,
        "updated_at": now_iso(),
    }
