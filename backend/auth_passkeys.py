"""WebAuthn registration + authentication routes (F7, 9.8) and the fresh verification rule (7.2).

Written against py_webauthn 3.0.1 (same call signatures as the 2.x API the spec names). Challenges live in
the signed session cookie, are single use, and expire after CHALLENGE_TTL_S. Login uses discoverable
credentials: empty allow_credentials, so the phone offers its passkey and we look the member up by
credential id.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from backend import db, eventlog, members
from backend.routes_api import ApiError
from backend.settings import settings

CHALLENGE_TTL_S = 300          # options fetched ahead of the tap stay usable this long
FRESH_VERIFICATION_S = 90      # 7.2: gate/enter and exit/approve need Face ID this recent
PURPOSES = ("enter", "exit", "login", "return")

# Errors py_webauthn raises for a bad or tampered credential; malformed JSON shows up as the others.
VERIFY_ERRORS = (WebAuthnException, ValueError, KeyError, TypeError)


class RegisterVerifyBody(BaseModel):
    credential: dict[str, Any]


class LoginOptionsBody(BaseModel):
    purpose: Literal["enter", "exit", "login", "return"] = "login"


class LoginVerifyBody(BaseModel):
    credential: dict[str, Any]
    purpose: Literal["enter", "exit", "login", "return"] = "login"


router = APIRouter()


def require_passkeys() -> None:
    if not settings.features.passkeys:
        raise ApiError(404, "passkeys_off", "Face ID is turned off for this demo.")


def _options_response(options: Any) -> JSONResponse:
    return JSONResponse(json.loads(options_to_json(options)))


def _store_challenge(request: Request, kind: str, challenge: bytes, **extra: str) -> None:
    request.session[f"{kind}_challenge"] = bytes_to_base64url(challenge)
    request.session[f"{kind}_challenge_at"] = time.time()
    for key, value in extra.items():
        request.session[f"{kind}_{key}"] = value


def _take_challenge(request: Request, kind: str) -> tuple[bytes, dict[str, Any]]:
    """Pop the stored challenge (single use). 400 when missing or expired."""
    challenge = request.session.pop(f"{kind}_challenge", None)
    issued = request.session.pop(f"{kind}_challenge_at", None)
    extra = {k.removeprefix(f"{kind}_"): request.session.pop(k) for k in list(request.session)
             if k.startswith(f"{kind}_")}
    if not challenge or not isinstance(issued, (int, float)) or time.time() - issued > CHALLENGE_TTL_S:
        raise ApiError(400, "no_challenge", "That Face ID request expired. Please tap the button again.")
    return base64url_to_bytes(challenge), extra


def _passkeys_of(conn: sqlite3.Connection, member_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT credential_id, transports FROM passkeys WHERE member_id = ?", (member_id,)).fetchall()


def _transports(raw: str | None) -> list[AuthenticatorTransport] | None:
    out = []
    for t in json.loads(raw) if raw else []:
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            continue  # a transport this library does not know
    return out or None


# --- registration (signed-in member adds a passkey) ---

@router.post("/api/passkey/register/options")
def register_options(request: Request):
    require_passkeys()
    member = members.current_member(request)
    conn = db.connect()
    try:
        existing = _passkeys_of(conn, member["id"])
    finally:
        conn.close()
    options = generate_registration_options(
        rp_id=settings.env.rp_id,
        rp_name=settings.env.rp_name,
        user_id=member["id"].encode(),
        user_name=member["name"],
        user_display_name=member["name"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id"]),
                                                           transports=_transports(row["transports"]))
                             for row in existing],
    )
    _store_challenge(request, "reg", options.challenge, member=member["id"])
    eventlog.log("passkey_register_options", member_id=member["id"], existing=len(existing))
    return _options_response(options)


@router.post("/api/passkey/register/verify")
def register_verify(body: RegisterVerifyBody, request: Request):
    require_passkeys()
    member = members.current_member(request)
    challenge, extra = _take_challenge(request, "reg")
    if extra.get("member") != member["id"]:
        raise ApiError(400, "no_challenge", "That Face ID request expired. Please tap the button again.")
    try:
        verified = verify_registration_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_origin=settings.env.public_origin,
            expected_rp_id=settings.env.rp_id,
            require_user_verification=True,
        )
    except VERIFY_ERRORS as e:
        eventlog.log("passkey_register_failed", member_id=member["id"], error=str(e))
        raise ApiError(400, "passkey_failed", "Face ID could not be verified. Please try again.") from None

    credential_id = bytes_to_base64url(verified.credential_id)
    response = body.credential.get("response")
    transports = response.get("transports") if isinstance(response, dict) else None
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO passkeys (credential_id, member_id, public_key, sign_count, transports, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (credential_id, member["id"], verified.credential_public_key, verified.sign_count,
             json.dumps(transports) if isinstance(transports, list) else None, db.now_iso()))
        conn.commit()
    except sqlite3.IntegrityError:
        raise ApiError(409, "passkey_exists", "This passkey is already saved.") from None
    finally:
        conn.close()
    eventlog.log("passkey_registered", member_id=member["id"], credential_id=credential_id)
    return {"ok": True}


# --- authentication (discoverable: works with no cookie) ---

@router.post("/api/passkey/login/options")
def login_options(request: Request, body: LoginOptionsBody | None = None):
    require_passkeys()
    purpose = body.purpose if body else "login"
    options = generate_authentication_options(
        rp_id=settings.env.rp_id,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _store_challenge(request, "auth", options.challenge, purpose=purpose)
    return _options_response(options)


@router.post("/api/passkey/login/verify")
def login_verify(body: LoginVerifyBody, request: Request):
    require_passkeys()
    challenge, extra = _take_challenge(request, "auth")
    if extra.get("purpose") != body.purpose:
        raise ApiError(400, "no_challenge", "That Face ID request expired. Please tap the button again.")
    credential_id = body.credential.get("id") or body.credential.get("rawId")
    if not isinstance(credential_id, str) or not credential_id:
        raise ApiError(400, "bad_request", "Missing credential id.")

    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM passkeys WHERE credential_id = ?", (credential_id,)).fetchone()
        if row is None:
            eventlog.log("passkey_login_unknown", credential_id=credential_id, purpose=body.purpose)
            raise ApiError(401, "unknown_passkey",
                           "This passkey isn't registered here. Join first, or use the demo account.")
        try:
            verified = verify_authentication_response(
                credential=body.credential,
                expected_challenge=challenge,
                expected_origin=settings.env.public_origin,
                expected_rp_id=settings.env.rp_id,
                credential_public_key=row["public_key"],
                credential_current_sign_count=row["sign_count"],
                require_user_verification=True,
            )
        except VERIFY_ERRORS as e:
            eventlog.log("passkey_login_failed", member_id=row["member_id"], purpose=body.purpose, error=str(e))
            raise ApiError(401, "passkey_failed", "Face ID could not be verified. Please try again.") from None
        conn.execute("UPDATE passkeys SET sign_count = ? WHERE credential_id = ?",
                     (verified.new_sign_count, credential_id))
        conn.commit()
        member = members.get_member(row["member_id"], conn)
    finally:
        conn.close()
    if member is None:
        raise ApiError(401, "unknown_passkey", "This passkey isn't registered here. Join first, or use the demo account.")

    members.log_in(request, member["id"])
    request.session["verified_at"] = time.time()
    request.session["verified_member"] = member["id"]
    request.session["verified_purpose"] = body.purpose
    eventlog.log("passkey_login", member_id=member["id"], purpose=body.purpose)
    return {"ok": True, "member": members.public_member(member)}


# --- 7.2 fresh verification ---

def consume_fresh_verification(request: Request, member_id: str) -> dict[str, Any]:
    """Gate/enter and exit/approve call this. Returns the cardholder confirmation for the instruction (8.6).

    passkeys on: a passkey login for this member in the last 90 s is required, and used up by this call.
    passkeys off: being signed in is enough (the caller already checked that); the tap on "Confirm" counts.
    """
    if not settings.features.passkeys:
        return {"method": "confirm_button", "verified_at": db.now_iso()}
    verified_at = request.session.get("verified_at")
    fresh = (request.session.get("verified_member") == member_id and isinstance(verified_at, (int, float))
             and 0 <= time.time() - verified_at <= FRESH_VERIFICATION_S)
    for key in ("verified_at", "verified_member", "verified_purpose"):
        request.session.pop(key, None)  # one use, and a stale one is useless anyway
    if not fresh:
        raise ApiError(401, "not_verified", "Please confirm with Face ID first.")
    when = datetime.fromtimestamp(verified_at, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"method": "passkey", "verified_at": when}
