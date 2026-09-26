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
