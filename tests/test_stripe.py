"""S4.2: Stripe test mode (9.9). The stripe module is replaced by a fake: no test ever reaches Stripe."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import admin, db, payments
from backend.main import app
from identity_helpers import login_as, set_env, set_features
from test_cart import FULL, TOKEN, member_id, snap, tmp_data, with_bays  # noqa: F401

pytestmark = pytest.mark.usefixtures("tmp_data")

HEADERS = {"X-Internal-Token": TOKEN}
ENTRY = {"gate_token": "test-entry-token"}
EXIT = {"gate_token": "test-exit-token"}
TEST_KEY = "sk_test_fake_key_for_tests"


class FakeStripe:
    """Just the parts of stripe-python that payments.py uses. Records every call."""

    class StripeError(Exception):
        pass

    class CardError(StripeError):
        def __init__(self, message, user_message=None, payment_intent_id=None):
            super().__init__(message)
            self.user_message = user_message
            self.error = SimpleNamespace(payment_intent=SimpleNamespace(id=payment_intent_id))

    class APIConnectionError(StripeError):
        pass

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.api_key = None
        self.max_network_retries = 2
        self.default_http_client = None
        self.intent_status = "succeeded"
        self.intent_error: Exception | None = None
        self.customer_error: Exception | None = None
        fake = self

        class Customer:
            @staticmethod
            def create(**kw):
                fake.calls.append(("Customer.create", kw))
                if fake.customer_error:
                    raise fake.customer_error
                return SimpleNamespace(id=f"cus_{kw['metadata'].get('member_id', 'smoke')}")

        class PaymentMethod:
            @staticmethod
            def attach(pm, **kw):
                fake.calls.append(("PaymentMethod.attach", {"payment_method": pm, **kw}))
                return SimpleNamespace(id="pm_1TestVisa")

        class PaymentIntent:
            @staticmethod
            def create(**kw):
                fake.calls.append(("PaymentIntent.create", kw))
                if fake.intent_error:
                    raise fake.intent_error
                return SimpleNamespace(id="pi_3QabcdEFGH12xyz9", status=fake.intent_status)

        self.Customer, self.PaymentMethod, self.PaymentIntent = Customer, PaymentMethod, PaymentIntent

    def HTTPXClient(self, **kw):  # noqa: N802 (mirrors stripe.HTTPXClient)
        return ("httpx-client", kw)

    def named(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(payments, "stripe", fake)
    monkeypatch.setattr(payments, "_stripe_ready", False)
    set_features(monkeypatch, stripe=True, gates=True, passkeys=True, loyalty=True)
    set_env(monkeypatch, stripe_secret_key=TEST_KEY)
    monkeypatch.setattr(admin, "force_decline", False)
    return fake


def member_row(mid: str) -> dict:
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM members WHERE id = ?", (mid,)).fetchone())
    finally:
        conn.close()


def shop_and_quote(client: TestClient, mid: str) -> dict:
    client.post("/internal/shelf", json=snap(FULL), headers=HEADERS)
    login_as(client, mid, verified_at=time.time(), verified_member=mid)
    assert client.post("/api/gate/enter", json=ENTRY).status_code == 200
    client.post("/internal/shelf", json=snap(with_bays(b0=[1]), frame_id=2), headers=HEADERS)
    r = client.post("/api/gate/exit/quote", json=EXIT)
    assert r.status_code == 200, r.text
    return r.json()


def approve(client: TestClient, mid: str) -> dict:
    login_as(client, mid, verified_at=time.time(), verified_member=mid)
    r = client.post("/api/gate/exit/approve")
    assert r.status_code == 200, r.text
    return r.json()


# --- key rules ---

@pytest.mark.parametrize("key, refused", [
    ("sk_test_abc", False), ("", False), ("sk_live_abc", True), ("rk_test_abc", True), ("pk_test_abc", True),
])
def test_only_test_keys_are_accepted(key, refused):
    features = SimpleNamespace(stripe=True)
    assert (payments.stripe_key_error(features, key) is not None) is refused
    assert payments.stripe_key_error(SimpleNamespace(stripe=False), key) is None  # flag off: key never used


def test_startup_refuses_live_key(monkeypatch):
    set_features(monkeypatch, stripe=True)
    set_env(monkeypatch, stripe_secret_key="sk_live_real_money")
    with pytest.raises(SystemExit):
        payments._refuse_non_test_key()
    assert payments.stripe_active() is False  # and never used even if startup were skipped


def test_stripe_configured_with_short_timeout(fake_stripe):
    s = payments._stripe()
    assert s.api_key == TEST_KEY and s.max_network_retries == 1
    assert s.default_http_client == ("httpx-client", {"timeout": payments.STRIPE_TIMEOUT_S, "allow_sync_methods": True})


# --- signup + backfill ---

def test_signup_attaches_test_visa(fake_stripe):
    with TestClient(app) as client:
        m = client.post("/api/members/signup", json={"name": "Maya"}).json()["member"]
    [cust] = fake_stripe.named("Customer.create")
    assert cust["name"] == "Maya" and cust["metadata"] == {"member_id": m["id"]}
    assert cust["idempotency_key"] == f"speedmart-customer-{m['id']}"
    [attach] = fake_stripe.named("PaymentMethod.attach")
    assert attach["payment_method"] == "pm_card_visa" and attach["customer"] == f"cus_{m['id']}"
    row = member_row(m["id"])
    assert row["stripe_customer_id"] == f"cus_{m['id']}" and row["stripe_pm_id"] == "pm_1TestVisa"
    assert row["card_label"] == "Demo card · Visa test •••• 4242 · not your card"
    assert "stripe_customer_id" not in m


def test_signup_survives_stripe_outage(fake_stripe):
    fake_stripe.customer_error = FakeStripe.APIConnectionError("network down")
    with TestClient(app) as client:
        r = client.post("/api/members/signup", json={"name": "Maya"})
    assert r.status_code == 200
    row = member_row(r.json()["member"]["id"])
    assert row["stripe_customer_id"] is None and row["card_label"] == "Demo card · Visa test •••• 4242 · not your card"


def test_demo_member_backfilled_at_quote(fake_stripe, member_id):
    assert member_row(member_id)["stripe_pm_id"] is None
    with TestClient(app) as client:
        shop_and_quote(client, member_id)
    row = member_row(member_id)
    assert row["stripe_customer_id"] == f"cus_{member_id}" and row["stripe_pm_id"] == "pm_1TestVisa"
    assert len(fake_stripe.named("Customer.create")) == 1


# --- charges ---

def test_approve_creates_payment_intent(fake_stripe, member_id):
    with TestClient(app) as client:
        q = shop_and_quote(client, member_id)
        p = approve(client, member_id)["payment"]
    sid, iid = q["cart"]["session_id"], q["instruction"]["instruction_id"]
    [kw] = fake_stripe.named("PaymentIntent.create")
    assert kw == {
        "amount": 864, "currency": "usd", "customer": f"cus_{member_id}", "payment_method": "pm_1TestVisa",
        "payment_method_types": ["card"], "off_session": True, "confirm": True, "description": "SpeedMart #01",
        "metadata": {"session_id": sid, "instruction_id": iid}, "idempotency_key": f"speedmart-{sid}-1",
    }
    assert p["status"] == "AUTHORIZED" and p["provider"] == "stripe_test"
    assert p["provider_ref"] == "pi_3QabcdEFGH12xyz9" and p["auth_code"] == "12XYZ9"
    assert p["points_earned"] == 8


def test_card_error_is_declined_and_retry_uses_new_idempotency_key(fake_stripe, member_id):
    fake_stripe.intent_error = FakeStripe.CardError("card_declined", user_message="Your card was declined.",
                                                    payment_intent_id="pi_declined1")
    with TestClient(app) as client:
        q = shop_and_quote(client, member_id)
        body = approve(client, member_id)
        assert body["payment"]["status"] == "DECLINED" and body["payment"]["provider"] == "stripe_test"
        assert body["payment"]["provider_ref"] == "pi_declined1" and body["message"] == "Your card was declined."

        fake_stripe.intent_error = None
        assert approve(client, member_id)["payment"]["status"] == "AUTHORIZED"
    sid = q["cart"]["session_id"]
    assert [kw["idempotency_key"] for kw in fake_stripe.named("PaymentIntent.create")] == [
        f"speedmart-{sid}-1", f"speedmart-{sid}-2"]


@pytest.mark.parametrize("error", [FakeStripe.APIConnectionError("network unreachable"), RuntimeError("boom")])
def test_non_card_errors_fall_back_to_mock(fake_stripe, member_id, error):
    fake_stripe.intent_error = error
    with TestClient(app) as client:
        shop_and_quote(client, member_id)
        p = approve(client, member_id)["payment"]
    assert p["status"] == "AUTHORIZED" and p["provider"] == "mock" and p["auth_code"].startswith("MOCK")


def test_card_link_failure_falls_back_to_mock(fake_stripe, member_id):
    fake_stripe.customer_error = FakeStripe.APIConnectionError("network unreachable")
    with TestClient(app) as client:
        shop_and_quote(client, member_id)
        p = approve(client, member_id)["payment"]
    assert p["provider"] == "mock" and p["status"] == "AUTHORIZED"
    assert fake_stripe.named("PaymentIntent.create") == []


def test_unfinished_intent_is_declined(fake_stripe, member_id):
    fake_stripe.intent_status = "requires_action"
    with TestClient(app) as client:
        shop_and_quote(client, member_id)
        body = approve(client, member_id)
    assert body["payment"]["status"] == "DECLINED" and "requires_action" in body["message"]


def test_force_decline_skips_stripe(fake_stripe, member_id, monkeypatch):
    monkeypatch.setattr(admin, "force_decline", True)
    with TestClient(app) as client:
        shop_and_quote(client, member_id)
        body = approve(client, member_id)
    assert body["payment"]["status"] == "DECLINED" and body["payment"]["provider"] == "stripe_test"
    assert fake_stripe.named("PaymentIntent.create") == []


def test_no_key_means_mock_and_no_stripe_calls(fake_stripe, member_id, monkeypatch):
    set_env(monkeypatch, stripe_secret_key="")
    with TestClient(app) as client:
        client.post("/api/members/signup", json={"name": "Maya"})
        shop_and_quote(client, member_id)
        assert approve(client, member_id)["payment"]["provider"] == "mock"
    assert fake_stripe.calls == []


def test_stripe_flag_off_means_mock(fake_stripe, member_id, monkeypatch):
    set_features(monkeypatch, stripe=False)
    with TestClient(app) as client:
        shop_and_quote(client, member_id)
        assert approve(client, member_id)["payment"]["provider"] == "mock"
    assert fake_stripe.calls == []


# --- scripts/stripe_smoke.py (the real run is for a human; here only with the fake module) ---

def _smoke():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import stripe_smoke
    return stripe_smoke


def test_smoke_refuses_non_test_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("STRIPE_SECRET_KEY=sk_live_nope\n", encoding="utf-8")
    assert _smoke().main(["--yes", "--env", str(env)]) == 2


def test_smoke_charges_one_dollar(tmp_path, monkeypatch, capsys):
    import sys

    fake = FakeStripe()
    fake.StripeError = FakeStripe.StripeError
    fake.PaymentIntent.create = staticmethod(lambda **kw: fake.calls.append(("PaymentIntent.create", kw)) or
                                             SimpleNamespace(id="pi_smoke123456", status="succeeded", amount=100,
                                                             currency="usd"))
    monkeypatch.setitem(sys.modules, "stripe", fake)
    env = tmp_path / ".env"
    env.write_text("STRIPE_SECRET_KEY=sk_test_abc\n", encoding="utf-8")
    assert _smoke().main(["--yes", "--env", str(env)]) == 0
    [kw] = fake.named("PaymentIntent.create")
    assert kw["amount"] == 100 and kw["currency"] == "usd" and kw["confirm"] is True
    assert fake.api_key == "sk_test_abc" and "pi_smoke123456" in capsys.readouterr().out
