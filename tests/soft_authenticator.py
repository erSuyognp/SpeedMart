"""A tiny software passkey (P-256, "none" attestation) so tests can run the real py_webauthn checks.

It builds exactly what a phone sends through @simplewebauthn/browser: base64url JSON with clientDataJSON,
attestationObject / authenticatorData and signature. Both cbor2 and cryptography come with py_webauthn.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str, user_verified: bool = True):
        self.rp_id = rp_id
        self.origin = origin
        self.user_verified = user_verified
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.user_handle: str | None = None

    @property
    def id(self) -> str:
        return bytes_to_base64url(self.credential_id)

    def _flags(self, extra: int = 0) -> int:
        return FLAG_UP | (FLAG_UV if self.user_verified else 0) | extra

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def _cose_public_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def create(self, options: dict, origin: str | None = None) -> dict:
        """navigator.credentials.create() for PublicKeyCredentialCreationOptionsJSON."""
        self.user_handle = options["user"]["id"]
        rp_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        attested = bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self._cose_public_key()
        auth_data = rp_hash + bytes([self._flags(FLAG_AT)]) + struct.pack(">I", self.sign_count) + attested
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": self.id, "rawId": self.id, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(self._client_data("webauthn.create", options["challenge"], origin)),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {}, "authenticatorAttachment": "platform",
        }

    def get(self, options: dict, origin: str | None = None) -> dict:
        """navigator.credentials.get() for PublicKeyCredentialRequestOptionsJSON."""
        self.sign_count += 1
        rp_hash = hashlib.sha256(options["rpId"].encode()).digest()
        auth_data = rp_hash + bytes([self._flags()]) + struct.pack(">I", self.sign_count)
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.id, "rawId": self.id, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": self.user_handle,
            },
            "clientExtensionResults": {}, "authenticatorAttachment": "platform",
        }
