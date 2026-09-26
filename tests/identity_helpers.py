"""Shared helpers for the identity / gates / checkout tests (S3.x, S4.x)."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from base64 import b64encode

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner


def set_features(monkeypatch, **flags: bool) -> None:
    """Flip feature flags everywhere: every loaded backend module that did `from backend.settings import settings`."""
    from backend import settings as settings_mod

    new = dataclasses.replace(settings_mod.settings,
                              features=dataclasses.replace(settings_mod.settings.features, **flags))
    for name, mod in list(sys.modules.items()):
        if name.startswith("backend") and getattr(mod, "settings", None) is not None \
                and not isinstance(getattr(mod, "settings"), type(sys)):
            monkeypatch.setattr(mod, "settings", new)


def set_env(monkeypatch, **values: str) -> None:
    """Replace .env values (Env dataclass fields) everywhere, like set_features."""
    from backend import settings as settings_mod

    current = sys.modules["backend.settings"].settings
    new = dataclasses.replace(current, env=dataclasses.replace(current.env, **values))
    for name, mod in list(sys.modules.items()):
        if name.startswith("backend") and getattr(mod, "settings", None) is not None \
                and not isinstance(getattr(mod, "settings"), type(sys)):
            monkeypatch.setattr(mod, "settings", new)
    assert settings_mod.settings is new


def login_as(client: TestClient, member_id: str, **extra) -> None:
    """Session cookie for a signed-in member, signed like Starlette's SessionMiddleware."""
    data = b64encode(json.dumps({"member_id": member_id, **extra}).encode())
    client.cookies.clear()  # replaces the whole session (the server's own cookie would shadow ours)
    client.cookies.set("session", TimestampSigner(os.environ["SESSION_SECRET"]).sign(data).decode())
