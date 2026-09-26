"""Judges must never think SpeedMart uses their real card or real biometrics.

No page ever asks for card details, every member shows the demo card label, and every customer page says DEMO.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from backend import db, payments
from test_cart import tmp_data  # noqa: F401  (temp DB + event log per test)

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CUSTOMER_PAGES = ["index.html", "enter.html", "store.html", "exit.html", "receipt.html", "intent.html"]
LABEL = "Demo card · Visa test •••• 4242 · not your card"

CARD_AUTOCOMPLETE = {"cc-number", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc"}
CARD_NAME = re.compile(r"card|cvv|cvc", re.IGNORECASE)


class CardFieldFinder(HTMLParser):
    """Collects form fields that would ask for a card number, expiry or security code."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("input", "select", "textarea"):
            return
        a = {k: (v or "") for k, v in attrs}
        tokens = set(a.get("autocomplete", "").lower().split())
        if tokens & CARD_AUTOCOMPLETE or CARD_NAME.search(a.get("name", "")) or CARD_NAME.search(a.get("id", "")):
            self.found.append(self.get_starttag_text() or tag)


def card_fields(html: str) -> list[str]:
    finder = CardFieldFinder()
    finder.feed(html)
    return finder.found


def test_the_card_field_scan_catches_card_fields():
    assert card_fields('<input autocomplete="cc-number">')
    assert card_fields('<input autocomplete="billing cc-exp">')
    assert card_fields('<input autocomplete="cc-csc">')
    assert card_fields('<input name="cvv">')
    assert card_fields('<input name="card_number">')
    assert card_fields('<input id="cvc">')
    assert not card_fields('<input id="name" autocomplete="given-name">')


@pytest.mark.parametrize("page", sorted(p.name for p in WEB.glob("*.html")))
def test_no_page_asks_for_card_details(page):
    assert card_fields((WEB / page).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("page", CUSTOMER_PAGES)
def test_customer_pages_show_the_demo_badge_and_footer(page):
    html = (WEB / page).read_text(encoding="utf-8")
    header = html[html.index("<header"):html.index("</header>")]
    assert 'class="demo-badge"' in header and ">DEMO<" in header
    assert "Sandbox demo" in html


def test_join_page_says_no_card_before_the_join_button():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    note = ("This is a demo. We never ask for your card. A test Visa card is linked for you automatically, "
            "and no real money moves.")
    assert html.index(note) < html.index('id="join-btn"')


def test_verification_buttons_carry_the_on_device_note():
    assert "SpeedMart only receives a yes or no." in (WEB / "js" / "passkey.js").read_text(encoding="utf-8")
    for page, button in [("index.html", "join-btn"), ("index.html", "setup-btn"), ("enter.html", "enter-btn"),
                         ("exit.html", "approve-btn"), ("receipt.html", "return-btn")]:
        html = (WEB / page).read_text(encoding="utf-8")
        after = html[html.index(f'id="{button}"'):]
        next_tag = after[after.index("</button>"):].split("<", 3)[2]  # the element right after the button
        assert "data-verify-note" in next_tag, (page, button)


@pytest.mark.usefixtures("tmp_data")
def test_every_member_gets_the_demo_card_label():
    assert payments.DEFAULT_CARD_LABEL == db.DEMO_CARD_LABEL == LABEL
    conn = db.connect()
    try:
        demo = conn.execute("SELECT card_label FROM members WHERE is_demo = 1").fetchone()
        assert demo["card_label"] == LABEL
        conn.execute("INSERT INTO members (id, name, budget_usd, card_label, created_at) "
                     "VALUES ('mem_old', 'Old', 20, 'Visa •••• 4242 (test)', '2026-09-25T00:00:00Z')")
        conn.execute("INSERT INTO members (id, name, budget_usd, created_at) "
                     "VALUES ('mem_none', 'None', 20, '2026-09-25T00:00:00Z')")
        conn.commit()
    finally:
        conn.close()
    db.init_db()  # the next backend start relabels existing members
    conn = db.connect()
    try:
        labels = {r["card_label"] for r in conn.execute("SELECT card_label FROM members")}
    finally:
        conn.close()
    assert labels == {LABEL}
