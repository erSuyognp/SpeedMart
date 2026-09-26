"""Checks every .env setting for SpeedMart without printing any secrets.

Run from the repo root:   python scripts\\check_env.py
For the tunnel check, have uvicorn and ngrok running first.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))


# 1. Settings load (also validates config.json and catalog.json)
try:
    from backend.settings import settings  # noqa: E402
    env = settings.env
    record("settings load", "PASS", "config.json, catalog.json and .env parsed")
except SystemExit:
    print("FAIL  settings load: see the error above")
    sys.exit(1)

# 2. Required values present and not left as placeholders
required = {
    "SESSION_SECRET": env.session_secret,
    "PUBLIC_ORIGIN": env.public_origin,
    "RP_ID": env.rp_id,
    "INTERNAL_TOKEN": env.internal_token,
    "ADMIN_PASSWORD": env.admin_password,
    "ENTRY_GATE_TOKEN": env.entry_gate_token,
    "EXIT_GATE_TOKEN": env.exit_gate_token,
}
for key, value in required.items():
    if not value:
        record(key, "FAIL", "empty")
    elif "change-me" in value:
        record(key, "FAIL", "still the placeholder from .env.example")
    else:
        record(key, "PASS", f"set ({len(value)} chars)")

# 3. Origin and RP_ID agree
origin = urlparse(env.public_origin)
if origin.scheme != "https":
    record("PUBLIC_ORIGIN https", "FAIL", "must start with https://")
elif env.public_origin.endswith("/"):
    record("PUBLIC_ORIGIN format", "FAIL", "remove the trailing slash")
elif origin.hostname != env.rp_id:
    record("RP_ID matches origin", "FAIL", f"RP_ID is '{env.rp_id}', origin host is '{origin.hostname}'")
else:
    record("RP_ID matches origin", "PASS", env.rp_id)

# 4. Stripe test key works
if not settings.features.stripe:
    record("Stripe", "SKIP", "features.stripe is false")
elif not env.stripe_secret_key.startswith("sk_test_"):
    record("Stripe", "FAIL", "key must start with sk_test_")
else:
    try:
        r = httpx.get("https://api.stripe.com/v1/balance", auth=(env.stripe_secret_key, ""), timeout=10)
        record("Stripe", "PASS" if r.status_code == 200 else "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        record("Stripe", "FAIL", repr(e))

# 5. LLM works and is fast enough for the 2.5 s agent timeout
if not settings.features.llm:
    record("LLM", "SKIP", "features.llm is false")
else:
    try:
        t0 = time.monotonic()
        if env.llm_provider == "anthropic":
            r = httpx.post("https://api.anthropic.com/v1/messages", timeout=10,
                           headers={"x-api-key": env.anthropic_api_key, "anthropic-version": "2023-06-01"},
                           json={"model": env.anthropic_model, "max_tokens": 20,
                                 "messages": [{"role": "user", "content": "Say hi in 5 words"}]})
        else:
            r = httpx.post(f"{env.openai_base_url}/chat/completions", timeout=10,
                           headers={"Authorization": f"Bearer {env.openai_api_key}"},
                           json={"model": env.openai_model, "max_tokens": 20,
                                 "messages": [{"role": "user", "content": "Say hi in 5 words"}]})
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            record("LLM", "FAIL", f"HTTP {r.status_code}: {r.text[:120]}")
        elif ms > 2500:
            record("LLM", "WARN", f"works but took {ms} ms; agent timeout is 2500 ms, pick a faster model")
        else:
            record("LLM", "PASS", f"{env.llm_provider}, {ms} ms")
    except Exception as e:
        record("LLM", "FAIL", repr(e))

# 6. Kiosk agent: KIOSK_TOKEN turns the entrance tablet into the talking guide; without it the kiosk is the
#    public page (QR codes, status, shelf map), which is fine, so an empty token is a WARN, not a FAIL.
if not env.kiosk_token:
    record("KIOSK_TOKEN", "WARN", "empty: the kiosk stays the public page (no voice, no shopper panel)")
elif "change-me" in env.kiosk_token:
    record("KIOSK_TOKEN", "FAIL", "still a placeholder")
elif len(env.kiosk_token) < 16:
    record("KIOSK_TOKEN", "WARN", f"only {len(env.kiosk_token)} chars; use a long random string")
else:
    record("KIOSK_TOKEN", "PASS", f"set ({len(env.kiosk_token)} chars); open /kiosk.html?k=<token> on the tablet")

# 7. Kiosk voice (text to speech): the voice id must exist for this API key. GET /v1/voices/{voice_id} costs no
#    characters. https://elevenlabs.io/docs/api-reference/voices/get
if not settings.features.voice:
    record("ELEVENLABS_VOICE_ID", "SKIP", "features.voice is false: the kiosk shows captions only")
elif not env.elevenlabs_voice_id:
    record("ELEVENLABS_VOICE_ID", "WARN", "empty: the kiosk shows captions only (no speech)")
elif not env.elevenlabs_api_key:
    record("ELEVENLABS_VOICE_ID", "WARN", "ELEVENLABS_API_KEY is empty: the kiosk shows captions only")
else:
    try:
        r = httpx.get(f"https://api.elevenlabs.io/v1/voices/{env.elevenlabs_voice_id}", timeout=10,
                      headers={"xi-api-key": env.elevenlabs_api_key})
        if r.status_code == 200:
            record("ELEVENLABS_VOICE_ID", "PASS", f"voice '{r.json().get('name', '?')}'")
        else:
            record("ELEVENLABS_VOICE_ID", "FAIL", f"HTTP {r.status_code}: unknown voice id or key without access")
    except Exception as e:
        record("ELEVENLABS_VOICE_ID", "FAIL", repr(e))

# 8. Local backend and public tunnel
for name, url in (("Local backend", "http://127.0.0.1:8000/api/health"),
                  ("Tunnel", f"{env.public_origin}/api/health")):
    try:
        r = httpx.get(url, timeout=8, headers={"ngrok-skip-browser-warning": "1"})
        ok = r.status_code == 200 and r.json().get("ok") is True
        record(name, "PASS" if ok else "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        record(name, "SKIP", f"not reachable ({type(e).__name__}); start uvicorn/ngrok to test")

width = max(len(n) for n, _, _ in results)
for name, status, detail in results:
    print(f"{status:5} {name:<{width}}  {detail}")
failed = sum(1 for _, s, _ in results if s == "FAIL")
print(f"\n{failed} failed" if failed else "\nAll required checks passed")
sys.exit(1 if failed else 0)
