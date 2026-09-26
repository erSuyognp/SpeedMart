"""S3.3: passkey options, challenge storage, verification failure paths, fresh verification rule (7.2).

py_webauthn's verify functions are replaced with fakes; options generation is real.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse

from backend import auth_passkeys, db, routes_api
from backend.main import app
from identity_helpers import set_features
from soft_authenticator import SoftAuthenticator
from test_cart import tmp_data  # noqa: F401

pytestmark = pytest.mark.usefixtures("tmp_data")

CRED_ID = b"\x01\x02credential-one"
CRED_B64 = bytes_to_base64url(CRED_ID)


class FakeVerify:
    """Records the kwargs of each call and returns `result` (or raises it)."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def registered(credential_id: bytes = CRED_ID, sign_count: int = 0):
    return SimpleNamespace(credential_id=credential_id, credential_public_key=b"public-key-bytes", sign_count=sign_count)


def reg_credential(transports=("internal", "hybrid")) -> dict:
    return {"id": CRED_B64, "rawId": CRED_B64, "type": "public-key",
            "response": {"clientDataJSON": "x", "attestationObject": "y", "transports": list(transports)}}


def auth_credential(cred_id: str = CRED_B64) -> dict:
    return {"id": cred_id, "rawId": cred_id, "type": "public-key",
            "response": {"clientDataJSON": "x", "authenticatorData": "y", "signature": "z"}}


def signup(client: TestClient, name: str = "Maya") -> str:
    return client.post("/api/members/signup", json={"name": name}).json()["member"]["id"]


def register_passkey(client: TestClient, monkeypatch) -> FakeVerify:
    fake = FakeVerify(registered())
    monkeypatch.setattr(auth_passkeys, "verify_registration_response", fake)
    assert client.post("/api/passkey/register/options").status_code == 200
    r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
    assert r.status_code == 200 and r.json() == {"ok": True}
    return fake


def passkey_rows() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM passkeys")]
    finally:
        conn.close()


# --- registration ---

def test_register_options_need_login_and_flag(monkeypatch):
    with TestClient(app) as client:
        r = client.post("/api/passkey/register/options")
        assert r.status_code == 401 and r.json()["error"] == "not_logged_in"
        set_features(monkeypatch, passkeys=False)
        signup(client)
        for path in ("/api/passkey/register/options", "/api/passkey/login/options"):
            r = client.post(path)
            assert r.status_code == 404 and r.json()["error"] == "passkeys_off"


def test_register_options_content():
    with TestClient(app) as client:
        member_id = signup(client, "Maya Lopez")
        opts = client.post("/api/passkey/register/options").json()
        assert opts["rp"] == {"id": "testserver", "name": "SpeedMart"}
        assert opts["user"]["name"] == "Maya Lopez" and opts["user"]["displayName"] == "Maya Lopez"
        assert base64url_to_bytes(opts["user"]["id"]) == member_id.encode()
        assert opts["authenticatorSelection"]["residentKey"] == "required"
        assert opts["authenticatorSelection"]["userVerification"] == "required"
        assert opts["excludeCredentials"] == []
        assert len(base64url_to_bytes(opts["challenge"])) >= 32


def test_register_verify_uses_stored_challenge_and_saves_passkey(monkeypatch):
    with TestClient(app) as client:
        member_id = signup(client)
        fake = FakeVerify(registered(sign_count=3))
        monkeypatch.setattr(auth_passkeys, "verify_registration_response", fake)
        opts = client.post("/api/passkey/register/options").json()
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 200, r.text

        call = fake.calls[0]
        assert call["expected_challenge"] == base64url_to_bytes(opts["challenge"])
        assert call["expected_origin"] == "http://testserver" and call["expected_rp_id"] == "testserver"
        assert call["require_user_verification"] is True
        assert call["credential"] == reg_credential()

        [row] = passkey_rows()
        assert row["credential_id"] == CRED_B64 and row["member_id"] == member_id
        assert row["public_key"] == b"public-key-bytes" and row["sign_count"] == 3
        assert row["transports"] == '["internal", "hybrid"]'
        assert client.get("/api/me").json()["has_passkey"] is True

        # The saved passkey is excluded next time, so the phone does not make a duplicate.
        again = client.post("/api/passkey/register/options").json()
        assert [c["id"] for c in again["excludeCredentials"]] == [CRED_B64]
        assert again["excludeCredentials"][0]["transports"] == ["internal", "hybrid"]


def test_register_verify_without_options_or_twice(monkeypatch):
    with TestClient(app) as client:
        signup(client)
        monkeypatch.setattr(auth_passkeys, "verify_registration_response", FakeVerify(registered()))
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 400 and r.json()["error"] == "no_challenge"
        client.post("/api/passkey/register/options")
        assert client.post("/api/passkey/register/verify", json={"credential": reg_credential()}).status_code == 200
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 400 and r.json()["error"] == "no_challenge"  # challenge is single use


def test_register_verify_expired_challenge(monkeypatch):
    with TestClient(app) as client:
        signup(client)
        fake = FakeVerify(registered())
        monkeypatch.setattr(auth_passkeys, "verify_registration_response", fake)
        client.post("/api/passkey/register/options")
        real_time = time.time
        monkeypatch.setattr(auth_passkeys.time, "time", lambda: real_time() + auth_passkeys.CHALLENGE_TTL_S + 1)
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 400 and r.json()["error"] == "no_challenge" and fake.calls == []


def test_register_challenge_belongs_to_the_member_who_asked(monkeypatch):
    with TestClient(app) as client:
        signup(client, "Maya")
        client.post("/api/passkey/register/options")
        signup(client, "Sam")  # different member on the same phone before finishing
        monkeypatch.setattr(auth_passkeys, "verify_registration_response", FakeVerify(registered()))
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 400 and r.json()["error"] == "no_challenge" and passkey_rows() == []


@pytest.mark.parametrize("error", [InvalidRegistrationResponse("bad attestation"), KeyError("response")])
def test_register_verify_failure(monkeypatch, error):
    with TestClient(app) as client:
        signup(client)
        monkeypatch.setattr(auth_passkeys, "verify_registration_response", FakeVerify(error))
        client.post("/api/passkey/register/options")
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 400 and r.json()["error"] == "passkey_failed"
        assert passkey_rows() == [] and client.get("/api/me").json()["has_passkey"] is False


def test_register_same_credential_twice_is_409(monkeypatch):
    with TestClient(app) as client:
        signup(client)
        register_passkey(client, monkeypatch)
        client.post("/api/passkey/register/options")
        r = client.post("/api/passkey/register/verify", json={"credential": reg_credential()})
        assert r.status_code == 409 and r.json()["error"] == "passkey_exists"


# --- authentication ---

def test_login_options_work_without_cookie():
    with TestClient(app) as client:
        opts = client.post("/api/passkey/login/options", json={"purpose": "enter"}).json()
        assert opts["rpId"] == "testserver" and opts["userVerification"] == "required"
        assert opts["allowCredentials"] == []  # discoverable: the phone picks the passkey
        r = client.post("/api/passkey/login/options", json={"purpose": "shopping"})
        assert r.status_code == 422 and r.json()["error"] == "bad_request"


def test_login_verify_logs_in_and_sets_fresh_verification(monkeypatch):
    with TestClient(app) as phone:
        member_id = signup(phone)
        register_passkey(phone, monkeypatch)
        phone.post("/api/logout")

    with TestClient(app) as client:  # a fresh browser: no cookie at all
        fake = FakeVerify(SimpleNamespace(new_sign_count=7))
        monkeypatch.setattr(auth_passkeys, "verify_authentication_response", fake)
        opts = client.post("/api/passkey/login/options", json={"purpose": "enter"}).json()
        r = client.post("/api/passkey/login/verify", json={"credential": auth_credential(), "purpose": "enter"})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and r.json()["member"]["id"] == member_id

        call = fake.calls[0]
        assert call["expected_challenge"] == base64url_to_bytes(opts["challenge"])
        assert call["credential_public_key"] == b"public-key-bytes" and call["credential_current_sign_count"] == 0
        assert call["expected_origin"] == "http://testserver" and call["require_user_verification"] is True
        assert passkey_rows()[0]["sign_count"] == 7
        assert client.get("/api/me").json()["member"]["id"] == member_id


def test_login_verify_unknown_credential(monkeypatch):
    with TestClient(app) as client:
        fake = FakeVerify(SimpleNamespace(new_sign_count=1))
        monkeypatch.setattr(auth_passkeys, "verify_authentication_response", fake)
        client.post("/api/passkey/login/options", json={"purpose": "login"})
        r = client.post("/api/passkey/login/verify", json={"credential": auth_credential("nope"), "purpose": "login"})
        assert r.status_code == 401 and r.json()["error"] == "unknown_passkey" and fake.calls == []
        assert client.get("/api/me").status_code == 401


def test_login_verify_failure_does_not_log_in(monkeypatch):
    with TestClient(app) as phone:
        signup(phone)
        register_passkey(phone, monkeypatch)
    with TestClient(app) as client:
        monkeypatch.setattr(auth_passkeys, "verify_authentication_response",
                            FakeVerify(InvalidAuthenticationResponse("bad signature")))
        client.post("/api/passkey/login/options", json={"purpose": "exit"})
        r = client.post("/api/passkey/login/verify", json={"credential": auth_credential(), "purpose": "exit"})
        assert r.status_code == 401 and r.json()["error"] == "passkey_failed"
        assert client.get("/api/me").status_code == 401
        assert passkey_rows()[0]["sign_count"] == 0


def test_login_verify_needs_matching_challenge_and_purpose(monkeypatch):
    with TestClient(app) as phone:
        signup(phone)
        register_passkey(phone, monkeypatch)
    with TestClient(app) as client:
        fake = FakeVerify(SimpleNamespace(new_sign_count=1))
        monkeypatch.setattr(auth_passkeys, "verify_authentication_response", fake)
        r = client.post("/api/passkey/login/verify", json={"credential": auth_credential(), "purpose": "login"})
        assert r.status_code == 400 and r.json()["error"] == "no_challenge"
        client.post("/api/passkey/login/options", json={"purpose": "login"})
        r = client.post("/api/passkey/login/verify", json={"credential": auth_credential(), "purpose": "exit"})
        assert r.status_code == 400 and r.json()["error"] == "no_challenge" and fake.calls == []


# --- 7.2 fresh verification rule ---

def request_with(session: dict) -> Request:
    return Request({"type": "http", "session": dict(session)})


def test_fresh_verification_is_consumed_once():
    req = request_with({"member_id": "mem_a", "verified_at": time.time() - 5, "verified_member": "mem_a",
                        "verified_purpose": "enter"})
    confirmation = auth_passkeys.consume_fresh_verification(req, "mem_a")
    assert confirmation["method"] == "passkey" and confirmation["verified_at"].endswith("Z")
    assert req.session == {"member_id": "mem_a"}
    with pytest.raises(routes_api.ApiError) as e:
        auth_passkeys.consume_fresh_verification(req, "mem_a")
    assert e.value.status == 401 and e.value.code == "not_verified"


@pytest.mark.parametrize("session", [
    {"member_id": "mem_a"},                                                             # never verified
    {"member_id": "mem_a", "verified_at": time.time() - 91, "verified_member": "mem_a"},  # older than 90 s
    {"member_id": "mem_a", "verified_at": time.time() - 5, "verified_member": "mem_b"},   # someone else
    {"member_id": "mem_a", "verified_at": "yesterday", "verified_member": "mem_a"},       # junk
])
def test_stale_or_foreign_verification_is_refused(session):
    req = request_with(session)
    with pytest.raises(routes_api.ApiError) as e:
        auth_passkeys.consume_fresh_verification(req, "mem_a")
    assert e.value.code == "not_verified"
    assert "verified_at" not in req.session and "verified_member" not in req.session


def test_passkeys_off_needs_no_verification(monkeypatch):
    set_features(monkeypatch, passkeys=False)
    confirmation = auth_passkeys.consume_fresh_verification(request_with({"member_id": "mem_a"}), "mem_a")
    assert confirmation["method"] == "confirm_button"


# --- real py_webauthn verification with a software passkey (no mocks) ---

def test_real_round_trip_register_then_sign_in():
    phone = SoftAuthenticator("testserver", "http://testserver")
    with TestClient(app) as client:
        member_id = signup(client)
        opts = client.post("/api/passkey/register/options").json()
        r = client.post("/api/passkey/register/verify", json={"credential": phone.create(opts)})
        assert r.status_code == 200, r.text
        client.post("/api/logout")

        opts = client.post("/api/passkey/login/options", json={"purpose": "enter"}).json()
        r = client.post("/api/passkey/login/verify", json={"credential": phone.get(opts), "purpose": "enter"})
        assert r.status_code == 200, r.text
        assert r.json()["member"]["id"] == member_id and passkey_rows()[0]["sign_count"] == 1


def test_real_verification_rejects_wrong_origin_and_missing_user_verification():
    phone = SoftAuthenticator("testserver", "http://testserver")
    with TestClient(app) as client:
        signup(client)
        opts = client.post("/api/passkey/register/options").json()
        r = client.post("/api/passkey/register/verify",
                        json={"credential": phone.create(opts, origin="https://evil.example")})
        assert r.status_code == 400 and r.json()["error"] == "passkey_failed"

        opts = client.post("/api/passkey/register/options").json()
        assert client.post("/api/passkey/register/verify", json={"credential": phone.create(opts)}).status_code == 200

        phone.user_verified = False  # screen lock skipped: must not count as Face ID
        opts = client.post("/api/passkey/login/options", json={"purpose": "exit"}).json()
        r = client.post("/api/passkey/login/verify", json={"credential": phone.get(opts), "purpose": "exit"})
        assert r.status_code == 401 and r.json()["error"] == "passkey_failed"

        phone.user_verified = True  # a replayed assertion (old challenge) is refused too
        opts = client.post("/api/passkey/login/options", json={"purpose": "exit"}).json()
        stale = phone.get({**opts, "challenge": bytes_to_base64url(b"not-the-challenge-we-issued")})
        r = client.post("/api/passkey/login/verify", json={"credential": stale, "purpose": "exit"})
        assert r.status_code == 401 and r.json()["error"] == "passkey_failed"
