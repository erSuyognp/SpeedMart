# Overnight build notes: identity, gates, checkout (S3.1 to S4.2)

Branch `claude/speedmart-overnight-build-15d5b2`. Written unattended; every judgement call is listed under
Decisions, and everything that needs a phone, the tunnel, Stripe, or a human is under Morning checklist.

## Status

| Step | Status |
|---|---|
| S3.1 tunnel | done |
| S3.2 signup, members, demo account | done |
| S3.3 passkeys | done (needs phones, see checklist) |
| S3.4 gates, store lock, QR | done |
| S4.1 instruction, mock payment, approve, receipt, loyalty | done |
| S4.2 Stripe test mode | done (real test charge is the human's opt-in smoke test) |

`pytest -q`: 150 passed at the end of the night. Nothing was blocked. Everything needing a phone, the
tunnel, Stripe or the hardware is in the Morning checklist.

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

### S3.3 passkeys

- **Library versions checked.** Installed py_webauthn is **3.0.1** (spec says >= 2.0). Its function names and
  arguments are the same as the spec's 2.x calls; the code is written against 3.0.1.
  `@simplewebauthn/browser` is pinned to **13.3.0** (latest 13.x tonight). I checked the package's file list on
  jsDelivr: the UMD bundle is `dist/bundle/index.umd.min.js` (`index.umd.js` does not exist), exposing the
  global `SimpleWebAuthnBrowser`.
- **Bundle loaded on demand.** Pages do not have a `<script>` tag for the CDN. `passkey.load()` injects it only
  when `features.passkeys` is on, so LAN mode with passkeys off never depends on jsDelivr.
- **User gesture.** Options are fetched when the page loads (and again after every failed attempt), so the button
  tap goes straight to `startRegistration` / `startAuthentication`. The one exception is "Join with Face ID": the
  member must exist first, so that tap does signup, then fetches options, then prompts. If Safari drops the
  gesture there, the phone shows "Face ID was cancelled" with a "Try Face ID again" button whose options are
  already loaded. Check this on the iPhone (Morning checklist).
- **Challenges** live in the signed session cookie, are single use, and expire after 5 minutes (browser side
  re-fetches after 4). A registration challenge is bound to the member who asked for it. A login challenge
  remembers its `purpose`; verify must send the same purpose.
- **Only one outstanding challenge per kind.** Because the session is a cookie, two in-flight option requests
  would overwrite each other's challenge. `passkey.js` keeps one prefetch per kind and never prefetches after a
  success (a late response could restore a fresh verification that `approve` had just used up).
- **Fresh verification (7.2)** is set by any successful passkey login (purpose enter, exit or login), lasts 90 s,
  must be for the same member, and is consumed by the first gate/enter or exit/approve that checks it, whether
  that call then succeeds or not. A stale one is also wiped. With passkeys off the "Confirm" tap counts, and the
  instruction's `cardholder_confirmation.method` is `confirm_button` instead of `passkey`.
- **User verification is required** at registration and login (`require_user_verification=True`). A phone with no
  screen lock fails with "This phone has no screen lock set up, use the demo account".
- **Friendly errors** (`passkey.friendlyError`): cancel / timeout (`NotAllowedError`, `AbortError`,
  `ERROR_CEREMONY_ABORTED`; v13 passes `NotAllowedError` through uncoded, so the DOMException name is checked
  too) -> "Face ID was cancelled. Tap the button to try again."; missing authenticator features or
  no WebAuthn -> the no-screen-lock message; wrong domain -> "Face ID only works on the store's https link";
  already registered -> "use Sign in with Face ID"; CDN failed -> "Face ID could not load". Server errors show
  the backend message.
- **Tests:** mocked verify functions cover options, challenge storage, single use, expiry, purpose, failures,
  unknown credentials, duplicates and the 90 s rule. `tests/soft_authenticator.py` is a small software passkey
  (P-256, "none" attestation), so two extra tests run the **real** py_webauthn checks: register + sign in succeeds;
  wrong origin, missing user verification and a replayed challenge are refused.

### S3.4 gates, store lock, QR

- **`config.json` has `gates: false` tonight** (set before this session; I did not change it). All gate code is
  guarded and tested with the flag both ways. Turn it on for the QR demo (Morning checklist).
- **Gate routes with gates off** answer 404 `gates_off`, mirroring the S1.3 dev routes, which are 404 with gates on.
  `enter.html` then says "Gates are off today" with a link to the cart, and `exit.html` without a token takes its
  quote from `/api/dev/checkout`. The cart's "Checkout" button now opens `exit.html`, so the gates-off path gets
  the same review, approval and receipt screens.
- **Order of checks at `gate/enter`:** token (403) -> signed in (401 `not_logged_in`) -> already inside (200, same
  session, nothing consumed) -> occupied (409 with `occupant_first_name`) -> vision fresh (503) -> fresh Face ID
  (401 `not_verified`, consumed here) -> start session (the lock is checked again under `store._lock`). Refusals
  that need no Face ID come first, so a shopper who hits "occupied" can retry within 90 s without another prompt.
  On success the gate LED gets `GATE,OPEN` for 3 s via `serial_bridge.send_timed`.
- **`gate/exit/quote`:** re-quoting a pending checkout (rescan, page reload) returns the same frozen cart instead
  of an error, and an **empty cart closes the session** (7.1: "Nothing to pay, see you soon", `instruction: null`,
  no payment row). `/api/dev/checkout` shares this code. Three S1.3 tests in `tests/test_live.py` expected the
  old placeholder behaviour (freeze an empty cart, 409 on a second checkout); I updated them to the spec behaviour
  and added `test_dev_checkout_with_empty_cart_closes_session`.
- **`gate/exit/cancel`** needs no gate token (the shopper is standing at the exit page) and is a no-op when the
  session is already back in the store. It works with gates off too.
- **QR codes:** `scripts/gen_qr.py` reads `.env` with python-dotenv directly (no SESSION_SECRET needed), URL-encodes
  the tokens, and writes `qr/1_join.png`, `qr/2_enter.png`, `qr/3_exit.png`, `qr/all.pdf` (one Letter page per
  code at 300 DPI) with a big "1 · JOIN" style label and the URL in small print. I added `qr/` to `.gitignore`
  (outside my file list): codes 2 and 3 contain the gate tokens.
- **The gate tokens in `.env` are the spec's example values** (`entry-7d2f`, `exit-91ac`), which are public in
  the spec. Change them before printing (Morning checklist).

### S4.1 instruction, mock payment, approve, receipt, loyalty

- **Instruction (8.6)** is issued at quote time and kept in memory per session. It is reused on re-quote while
  the amount and budget match and it has not expired (15 minutes, `expires_at = created_at + 15 min`); otherwise a
  new one is issued. After a backend restart a new one is simply issued. Names follow the project rename:
  agent `speedmart-shelf-agent-01` / "SpeedMart Store Agent", token ref `tok_speedmart_<session id>`.
  `cardholder_confirmation.verified_at` is `null` in the quote and filled in at approve time; the filled-in copy
  is what is stored in `payments.instruction_json`.
- **Approve order:** signed in -> own session in CHECKOUT_PENDING (409 `invalid_state` otherwise) -> `over_scope`
  (409, checked **before** consuming Face ID, so the shopper can cancel, put something back, re-quote and approve
  with the same Face ID within 90 s) -> fresh verification (401) -> charge under a lock (a double tap can never
  charge twice; the second call sees PAID and gets 409).
- **Declines are not HTTP errors.** Approve returns 200 `{"payment": {... "status": "DECLINED"}, "message": "..."}`.
  The Payment object keeps exactly the 8.7 keys; the human message sits next to it. The session stays in
  CHECKOUT_PENDING and a retry needs a new Face ID.
- **Force-decline** (admin) stays on until the admin turns it off (it is a toggle in the existing admin UI and
  API), rather than resetting itself after one charge. It returns DECLINED without calling any provider.
- **LEDs:** AUTHORIZED -> `SHELF,GREEN` then `SHELF,IDLE` after 3 s; DECLINED -> `SHELF,RED` then idle after 2 s,
  both via `serial_bridge.send_timed`. The WebSocket gets `gate: paid` (from the store transition) or
  `gate: declined`.
- **Receipt** `GET /api/receipt/{session_id}`: owner or admin only (404 otherwise, so ids cannot be probed).
  Returns items, totals, the authorized payment (or the last attempt), `points_earned`, `points_total`. The owner
  viewing a PAID receipt moves the session to CLOSED; `store.close_paid_sessions()` also closes PAID sessions after
  60 s from the existing 30 s background loop (so it can take up to 90 s).
- **Loyalty:** `floor(total)` points on AUTHORIZED only when `loyalty` is on; the receipt page hides points when
  loyalty is off.
- **Payments log:** every attempt (authorized or declined) goes to SQLite and `data/payments.log.jsonl` with the
  full instruction.
- **Gates off:** the cart's Checkout button opens `exit.html`, which uses `/api/dev/checkout` for the quote and the
  same approve / cancel routes. `store.html` now remembers a paid session and links to its receipt.
- **Checked in a browser tonight** (local scratch copy with passkeys, Stripe and LEDs off; real `config.json`
  untouched): join -> Start shopping -> fake shelf pick -> live cart $8.64 -> Checkout -> "What the payment network
  sees" -> "Confirm $8.64" -> receipt with MOCK auth code and "+8 points · 8 total"; `payments.log.jsonl` written.

### S4.2 Stripe test mode

- **Library:** stripe-python **15.6.1**. The legacy resource calls the spec uses (`stripe.Customer.create`,
  `stripe.PaymentMethod.attach("pm_card_visa", customer=...)`, `stripe.PaymentIntent.create(...)`) all exist,
  and `stripe.CardError` is available at the top level. Request options (`idempotency_key`) are passed as keyword
  arguments.
- **Key rule:** with `stripe` on, a non-empty key that does not start with `sk_test_` aborts startup (checked when
  `backend.payments` is imported, like the settings check). An **empty** key does not abort: it means mock
  payments, which is what the "Stripe key removed" failure drill expects. With the flag off the key is never read.
- **Timeouts:** Stripe uses `stripe.HTTPXClient(timeout=8)` (httpx is already a dependency) with one network retry
  (safe because every write has an idempotency key), so an unreachable Stripe falls back to mock within roughly
  20 s instead of the SDK's default 80 s.
- **Idempotency keys:** `speedmart-customer-<member>`, `speedmart-attach-<member>`, and for charges
  `speedmart-<session>-<attempt>` (spec's `aisle-` prefix renamed with the project). Attempt = number of payment
  rows for that session + 1, so a retry after a decline is a new key and a replayed request is not.
- **Card linking:** at signup when Stripe is active (customer named after the member, `metadata.member_id`, test
  Visa attached; ids saved, label "Visa •••• 4242 (test)"). Failure never blocks signup. **Backfill** happens at
  the exit quote for any member without a Stripe card, which covers the seeded Demo Shopper (its row is created by
  `db.py`, which this session does not own, and `main.py` lifespan is off limits), as well as members who joined
  while Stripe was down. Approve tries once more if the quote's backfill failed.
- **Outcomes:** `succeeded` -> AUTHORIZED, auth code = last 6 chars of the PaymentIntent id upper-cased;
  `CardError` -> DECLINED with Stripe's user message; any other status (e.g. `requires_action`) -> DECLINED with the
  status in the message; **any other exception** (network, auth, rate limit, bug) -> mock provider,
  `provider: "mock"`, event `stripe_fallback`. Force-decline returns DECLINED without calling Stripe.
- **Tests replace the `stripe` module entirely** (`tests/test_stripe.py` `FakeStripe`); `tests/conftest.py`
  also blanks `STRIPE_SECRET_KEY`, so even an unpatched test would take the mock path. No test calls Stripe.
- **`scripts/stripe_smoke.py`** is opt-in: it refuses non-test keys, asks you to type `yes` (or `--yes`), makes one
  $1.00 test-mode charge with its own customer, prints the PaymentIntent and a dashboard link, and never touches
  the SpeedMart database. I only ran its refusal and "no" paths tonight.

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

### 2. Gate tokens and QR codes (S3.4)

1. Put new random gate tokens in `.env`. PowerShell, run twice and paste one value into each line:
   `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(6))"`
   then set `ENTRY_GATE_TOKEN=entry-<value>` and `EXIT_GATE_TOKEN=exit-<value>`. Restart the backend.
2. Turn the gates on: in `config.json` set `"gates": true`. Restart the backend.
3. `.venv\Scripts\python.exe scripts\gen_qr.py` -> prints four `wrote qr\...` lines.
4. Open `qr/all.pdf`: three pages, labels "1 · JOIN", "2 · ENTER", "3 · EXIT", URLs on
   `https://stump-isotope-glorious.ngrok-free.dev`. Print at 100% or show on a tablet.
5. Scan each code with a phone camera: 1 opens the join page, 2 the entry gate ("Enter with Face ID"), 3 the exit
   page ("You're not in the store" until you have entered).

### 3. Before phone tests: shelf feed

Entry is refused ("The shelf camera is offline") unless a shelf snapshot arrived in the last 2 s. Either run the
vision worker, or in a spare PowerShell window: `.venv\Scripts\python.exe scripts\fake_shelf.py loop`
(type a tag id + Enter to take that unit off the shelf, again to put it back).

### 4. Passkeys on an iPhone (Safari) over the tunnel (S3.3)

Settings: `passkeys: true`, `https_tunnel: true`, backend + ngrok running (section 1). The iPhone needs a passcode
and Face ID set up, and iCloud Keychain on (Settings → [name] → iCloud → Passwords & Keychain).

1. Safari → `https://stump-isotope-glorious.ngrok-free.dev/` (or scan QR 1). Enter a first name, move the budget
   slider, tap **Join with Face ID**.
   Expected: iOS sheet "Save a passkey for stump-isotope-glorious.ngrok-free.dev?" → Continue → Face ID →
   "You're a member. Card linked: Visa •••• 4242 (test). Walk to the entry gate."
   If instead you see "Face ID was cancelled. Tap the button to try again." without having cancelled, Safari
   dropped the tap gesture during signup (see Decisions S3.3): tap **Try Face ID again**, it should work first
   time. Write down which happened; if it always needs the second tap, tell the team.
2. Cancel test: tap **Not you? Sign out**, tap **Already a member? Sign in with Face ID**, then press Cancel on
   the iOS sheet. Expected red strip "Face ID was cancelled. Tap the button to try again." and the button still
   there. Tap it again and complete Face ID. Expected: "You're a member." with the same name as before.
3. Reload the page. Expected: still signed in.
4. Desktop: `http://localhost:8000/admin.html` → event log shows `passkey_registered` and `passkey_login`.

### 5. Passkeys on an Android phone (Chrome) over the tunnel (S3.3)

Needs a screen lock (PIN / fingerprint) and a Google account signed in (Google Password Manager stores passkeys).

1. Chrome → the tunnel URL. Join with a different first name. Expected: "Create a passkey" sheet → fingerprint →
   member screen with the card line.
2. Sign out → **Already a member? Sign in with Face ID** → fingerprint. Expected: signed back in as the same name.
3. Cancel test as on the iPhone: dismiss the sheet → "Face ID was cancelled..." + retry works.
4. No-screen-lock test (optional, only on a spare phone without a lock): Join → expected red strip
   "This phone has no screen lock set up, use the demo account."

### 6. Full gate loop on a phone (S3.4, S4.1)

Settings: `gates: true`, `passkeys: true`, `stripe: true` (or false for mock), shelf feed running.

1. Scan QR 2 → **Enter with Face ID** → Face ID. Expected: "Welcome in, <name>", gate LED green for 3 s (if the
   ESP32 is connected), then the cart page with a green live dot.
2. With a second phone (or a desktop browser signed in as another member), scan QR 2 while the first shopper is
   inside. Expected: "Someone is shopping right now. Try again in a minute."
3. Take one electrolyte off the shelf. Expected: row "Electrolyte tabs × 1 $8.00", total $8.64.
4. Scan QR 3. Expected: itemized preview, total $8.64, "Paying with Visa •••• 4242 (test)", collapsible "What the
   payment network sees" (agent, merchant SpeedMart #01, max $20.00, single use, expiry, intent
   "Pay $8.64 to SpeedMart #01 for 1 item", sandbox label). Button **Approve $8.64 with Face ID**.
5. Tap **Keep shopping**. Expected: back on the cart page, cart live again. Scan QR 3 again.
6. Admin page → turn **force-decline** on. Tap Approve → Face ID. Expected: red "Declined: Card declined (sandbox:
   forced by staff). You can try again.", shelf LEDs red for 2 s, button "Try again with Face ID".
7. Turn force-decline off. Tap **Try again with Face ID** → Face ID. Expected: shelf green 3 s, receipt page:
   green check, "Paid $8.64", card label, auth code, "+8 points · 8 total" (points accumulate per member),
   "Show payment record" JSON. `data\payments.log.jsonl` has a DECLINED and an AUTHORIZED line.
8. Over-budget test: sign up with budget $10, enter, take two electrolytes ($17.28), scan QR 3. Expected: amber
   "This is over your $10.00 limit. Put something back to continue." and no approve button. Keep shopping, put one
   back, scan QR 3 again, approve works.
9. Empty-cart test: enter, take nothing, scan QR 3. Expected: "Nothing to pay, see you soon" and the store is free.
10. Admin reset while someone is inside frees the store (QR 2 works for the next person).
11. Passkeys-off check: set `"passkeys": false`, restart. Join shows **Join**, entry shows **Enter**, exit shows
    **Confirm $X.XX**; no Face ID prompts anywhere.

### 7. Stripe test mode (S4.2)

1. Confirm `.env` `STRIPE_SECRET_KEY` starts with `sk_test_` (it does tonight). A key starting with anything else
   makes the backend refuse to start with a clear message; that is intended.
2. Smoke test, one real **test-mode** charge of $1.00 (opt-in, never run by the tests):
   ```powershell
   .venv\Scripts\python.exe scripts\stripe_smoke.py
   ```
   Type `yes`. Expected: `status succeeded`, `amount $1.00 USD`, and a dashboard link. Open
   https://dashboard.stripe.com/test/payments: a $1.00 payment "SpeedMart smoke test", Succeeded.
3. App charge: with `stripe: true`, run section 6 steps 1 to 7 once. Expected: receipt meta says "Stripe test mode";
   auth code = last 6 characters of the PaymentIntent id. In the Stripe test dashboard the payment shows $8.64,
   description "SpeedMart #01", metadata `session_id` and `instruction_id` matching the receipt's payment record.
   The customer is named after the member, with the Visa 4242 test card attached.
4. Demo member backfill: admin page → demo-login on a team phone, shop, scan QR 3. Expected: the Stripe dashboard
   gains a customer "Demo Shopper" with a test Visa at that moment (it is created at the first checkout, not at
   startup), and the approval appears as a test payment.
5. Network-down fallback: while on the exit page (after the quote), disconnect the laptop's internet (keep the
   phone reaching the laptop over LAN, or unplug only the uplink), then approve. Expected: within about 20 s the
   receipt appears with "sandbox mock" and a `MOCK####` auth code; the event log has `stripe_fallback`. No crash.

### 8. Housekeeping

1. After merging this branch into the main checkout, refresh the old CRLF copies of the shell scripts once:
   `Remove-Item scripts\*.sh; git checkout -- scripts` (see Decisions S3.1).
2. Someone who owns `scripts/` should add `scripts/run_all.ps1` (CLAUDE.md asks for it). Until then use the two
   PowerShell commands in section 1, or Git Bash `scripts/run_all.sh`.
3. `pytest -q` from the repo root: expected all tests pass (150 tonight).
