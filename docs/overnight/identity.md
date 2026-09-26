# Overnight build notes: identity, gates, checkout (S3.1 to S4.2)

Branch `claude/speedmart-overnight-build-15d5b2`. Written unattended; every judgement call is listed under
Decisions, and everything that needs a phone, the tunnel, Stripe, or a human is under Morning checklist.

## Status

| Step | Status |
|---|---|
| S3.1 tunnel | done |
| S3.2 signup, members, demo account | done |

## Decisions

### S3.1 tunnel

- **Secure cookie rule.** `SessionMiddleware(https_only=True)` only when `features.https_tunnel` is on
  **and** `PUBLIC_ORIGIN` starts with `https://`. With the tunnel flag on but an `http://` LAN origin, secure
  cookies would silently log everyone out, so they stay plain there.
- **Tests never read the real `.env`.** The worktree has a real `.env` (tunnel origin, Stripe test key, LLM
  key). `tests/conftest.py` now pins `PUBLIC_ORIGIN=http://testserver`, `RP_ID=testserver`, gate tokens, and
  blanks `STRIPE_SECRET_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` before the backend is imported. Without
  this the https origin made cookies secure and 12 existing tests failed.
- **ngrok flag.** `run_all.sh` uses `--url=` when `ngrok http --help` lists it (ngrok 3.16+), else `--domain=`.
  ngrok logs go to stdout with a `[tunnel]` prefix and ngrok is stopped with the rest on Ctrl+C. If the flag
  is on but `PUBLIC_ORIGIN` is not https or ngrok is not on PATH, the script prints why and carries on
  without a tunnel instead of failing.
- **`scripts/run_all.ps1` not created.** CLAUDE.md asks for a PowerShell twin of `run_all.sh`, but this
  session only owns the tunnel section of `run_all.sh`. The Morning checklist gives the exact PowerShell
  commands to start the backend and tunnel by hand. Whoever owns `scripts/` should add `run_all.ps1`.
- **Added `.gitattributes` (`*.sh text eol=lf`).** Outside my file list, but necessary: with
  `core.autocrlf=true` the main checkout has `scripts/run_all.sh` with CRLF endings, and Git Bash cannot run
  it (bash reports errors like `command not found` for every line ending in CR). After merging, refresh the
  old CRLF copies once, PowerShell from the repo root: `Remove-Item scripts\*.sh; git checkout -- scripts`.
- ngrok was **not** started. `ngrok` is not on the Git Bash PATH on this machine.

### S3.2 signup, members, demo account

- **`GET /api/me` shape:** `{"member": {...}, "has_passkey": bool, "active_session_id": str|null}`.
  `active_session_id` is set only when the active store session belongs to this member. The public member
  object never includes Stripe ids; it adds `first_name` for the UI.
- **Validation:** name 1 to 40 characters (whitespace collapsed); budget $10 to $50 (the slider range), default
  `store.default_budget_usd`; dietary `none | vegetarian | vegan | gluten_free`, stored as `null`, `vegetarian`,
  `vegan`, `gluten free` (the agent reads it). Bad input: 422 with `bad_name`, `bad_budget` or `bad_dietary`.
- **`signup: false`:** `POST /api/members/signup` answers 404 `signup_off`; `index.html` shows "Joining is closed
  for this demo, ask a team member to sign you in as the Demo Shopper" (the admin demo-login path).
- **Card label** `Visa •••• 4242 (test)` is stored at signup for every member. With Stripe on, S4.2 also attaches
  `pm_card_visa`; with Stripe off, the mock provider uses the same test label.
- **Changing member clears verification.** Signing in as a different member (signup, passkey login) drops
  `verified_at` / `verified_member` / `verified_purpose`, so one member's fresh Face ID can never open the gate
  for another. Admin rights in the same cookie are kept.
- **Demo account:** the seeded Demo Shopper (db.py) is reached via admin demo-login, as before. When a signed-in
  member (including the Demo Shopper on a team phone) has no passkey and passkeys are on, `index.html` shows
  "Set up Face ID", so the team phone can register one for the demo account.
- A deleted member's cookie (e.g. DB wiped) gets 401 and the member keys are cleared.

## Morning checklist

### 1. Tunnel (S3.1)

1. Install / locate ngrok and check it is on PATH. PowerShell: `ngrok version` should print `ngrok version 3.x`.
   If not found: `winget install ngrok.ngrok`, then open a new terminal. Then once:
   `ngrok config add-authtoken <token from dashboard.ngrok.com>`.
2. Check `.env`: `PUBLIC_ORIGIN=https://stump-isotope-glorious.ngrok-free.dev` and
   `RP_ID=stump-isotope-glorious.ngrok-free.dev` (host only, no scheme, no slash). They already match tonight.
3. Start the backend with proxy headers. PowerShell, from the repo root:
   ```powershell
   .venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
   ```
4. In a second PowerShell window:
   ```powershell
   ngrok http --url=stump-isotope-glorious.ngrok-free.dev 8000
   ```
   (older ngrok: `--domain=` instead of `--url=`). Expected: ngrok shows `Forwarding https://stump-isotope-glorious.ngrok-free.dev -> http://localhost:8000`.
   Alternative in Git Bash: `scripts/run_all.sh` starts backend, worker and tunnel together.
5. Phone on **cellular data** (WiFi off): open `https://stump-isotope-glorious.ngrok-free.dev/api/health`.
   Expected: `{"ok":true,...}` (the free ngrok domain may first show an ngrok "Visit Site" interstitial; tap it once).
6. In desktop Chrome DevTools on the tunnel URL, Application → Cookies: after any login the `session` cookie
   shows `Secure` ticked.
