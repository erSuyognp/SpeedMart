"""S3.1: secure session cookie behind the tunnel, run_all.sh tunnel + proxy headers."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import main
from backend.settings import settings

RUN_ALL = Path(__file__).resolve().parent.parent / "scripts" / "run_all.sh"


def _with(tunnel: bool, origin: str):
    return dataclasses.replace(settings, features=dataclasses.replace(settings.features, https_tunnel=tunnel),
                               env=dataclasses.replace(settings.env, public_origin=origin))


def test_https_only_cookie_when_tunnel_on():
    assert main.session_https_only(_with(True, "https://speedmart-demo.ngrok-free.app")) is True


def test_plain_cookie_when_tunnel_off_or_lan_origin():
    assert main.session_https_only(_with(False, "https://speedmart-demo.ngrok-free.app")) is False
    assert main.session_https_only(_with(True, "http://192.168.1.20:8000")) is False
    assert main.SESSION_HTTPS_ONLY is False  # tests run against http://testserver


def test_run_all_has_proxy_headers_and_guarded_tunnel():
    text = RUN_ALL.read_text(encoding="utf-8")
    assert "--proxy-headers" in text and '--forwarded-allow-ips "*"' in text
    assert "https_tunnel" in text and "PUBLIC_ORIGIN" in text
    assert 'ngrok http "$URL_FLAG=$TUNNEL_DOMAIN" 8000' in text
    assert "$TUNNEL_PID" in text  # stopped on Ctrl+C with the others


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_run_all_parses():
    assert subprocess.run(["bash", "-n", str(RUN_ALL)], capture_output=True).returncode == 0


# --- PlainHttpCookieFix: the tunnel keeps Secure, LAN/localhost clients stay logged in ---------------

RUN_ALL_PS1 = Path(__file__).resolve().parent.parent / "scripts" / "run_all.ps1"


def _cookie_over(scheme: str) -> str:
    """Set-Cookie the wrapped app emits for a request arriving with this scheme."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def login(request):
        r = PlainTextResponse("ok")
        r.set_cookie("session", "abc", httponly=True, samesite="lax", secure=True)
        return r

    app = main.PlainHttpCookieFix(Starlette(routes=[Route("/login", login)]))
    with TestClient(app, base_url=f"{scheme}://testserver") as client:
        return client.get("/login").headers["set-cookie"]


def test_secure_is_kept_for_https_requests():
    assert "secure" in _cookie_over("https").lower()


def test_secure_is_dropped_for_plain_http_requests():
    cookie = _cookie_over("http")
    assert "secure" not in cookie.lower()
    # Nothing else about the cookie changes.
    assert cookie.startswith("session=abc") and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()


def test_only_the_session_cookie_is_touched():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def handler(request):
        r = PlainTextResponse("ok")
        r.set_cookie("other", "1", secure=True)
        return r

    app = main.PlainHttpCookieFix(Starlette(routes=[Route("/x", handler)]))
    with TestClient(app, base_url="http://testserver") as client:
        assert "secure" in client.get("/x").headers["set-cookie"].lower()


def test_run_all_ps1_starts_and_stops_all_three():
    text = RUN_ALL_PS1.read_text(encoding="utf-8")
    assert "--proxy-headers" in text
    # Deliberately absent: on Windows "*" is glob expanded into file names, and uvicorn's default
    # (127.0.0.1) already trusts the local ngrok agent's X-Forwarded-* headers.
    assert "--forwarded-allow-ips" not in text
    assert "uvicorn" in text and "vision.worker" in text and "ngrok" in text
    assert "https_tunnel" in text and "PUBLIC_ORIGIN" in text
    assert "taskkill.exe" in text and "Stop-Children" in text  # every child stopped on Ctrl+C
