# SpeedMart

**The shelf builds your cart. You approve the charge.**

SpeedMart is a one-shelf grab-and-go store built for HackGT 13 (Shipyard hardware track + Visa challenge).
You sign up once with a passkey (Face ID / fingerprint). An overhead camera watches a three-bay shelf: take an
item and it appears in your phone cart in about a second, put it back and it disappears. A store agent explains
your cart in one sentence and suggests complements that fit your budget. At the exit you review the total and
approve it with Face ID against a scoped, single-use agent token. The shelf lights up green.

Everything payment-related is **sandbox**: Stripe test mode with a test Visa card, or a mock provider. The
authorization object is modeled on Visa Intelligent Commerce concepts. It is not a Visa API call.

The full design lives in [SpeedMart_BUILD_SPEC.md](SpeedMart_BUILD_SPEC.md). The demo script is in
[docs/DEMO.md](docs/DEMO.md) and the Devpost draft is in [docs/DEVPOST.md](docs/DEVPOST.md).

## Architecture

```
 overhead USB camera
        │
        ▼
 vision/worker.py ── ArUco tags → per-bay unit sets → stability ──POST /internal/shelf (5 Hz)──┐
 (separate process, overlay window for judges)                                                 │
                                                                                               ▼
 ┌──────────────────────────── backend (FastAPI, one process) ─────────────────────────────────────┐
 │ shelf_state.py   latest stable contents per bay                                                │
 │ store.py         session state machine + one-shopper store lock (SQLite: data/speedmart.db)    │
 │ cart.py          cart = baseline − shelf now (+ admin override), integer cents, tax            │
 │ agent.py         deterministic policy → template → optional LLM phrasing (1.2 s debounce)       │
 │ ws.py            /ws pushes cart, agent, gate and store status messages                        │
 │ serial_bridge.py USB serial to the shelf controller (bay LEDs, gate/shelf status)              │
 │ admin.py         team control panel: reset, overrides, demo login, LED tests                   │
 │ eventlog.py      everything important → data/events.log.jsonl                                  │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
        │ WebSocket + JSON API                                  │ USB serial (COM port)
        ▼                                                       ▼
 phone browser (web/, plain HTML + JS)                 ESP32 shelf controller (firmware/)
```

Key ideas:

- **The cart is computed, never accumulated.** `cart_qty = clamp(baseline − shelf_now + override, 0, baseline)`.
  Duplicate frames can't double count, put-backs just work, and a backend restart recomputes the same cart.
- **Unstable bays keep their last stable contents**, so a hand over a tag doesn't make the cart flicker.
- **The backend is the only source of truth.** The phone only renders snapshots.
- **The agent's policy is deterministic.** The LLM only phrases the decision, and templates cover every case
  when the LLM is off, slow, or says something invalid.
- **Every optional feature has a flag** in `config.json`. Turning one off never breaks the core loop.

### Build status

Done: backend core, cart engine, WebSocket live cart, admin panel, shelf controller + serial bridge, camera +
calibration, ArUco detection + overlay, AI store agent (S4.3).
Still to build, per the spec: motion freeze (S2.3), HTTPS tunnel, signup, passkeys, gates + QR codes (S3.x),
checkout + Stripe (S4.1, S4.2), `scripts/run_all.ps1`, `scripts/reset_demo.sh`. Until gates are built the
store page shows **Start shopping** / **Checkout** buttons (`features.gates` is `false` in `config.json`).

## Setup

Requires Python 3.11. The dev machine is Windows.

Windows (PowerShell):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env
notepad .env
```

macOS / Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Fill in `.env`: `SESSION_SECRET`, `INTERNAL_TOKEN`, `ADMIN_PASSWORD` at least. Optional: `ANTHROPIC_API_KEY` (or
`LLM_PROVIDER=openai` + `OPENAI_API_KEY` + `OPENAI_MODEL`) for LLM agent lines, and `STRIPE_SECRET_KEY`
(`sk_test_...` only) for Stripe test mode. `.env` is gitignored. Never commit it.

Then set up the hardware side:

1. **Tags:** `python scripts/gen_aruco.py` writes `tags/aruco_tags.pdf`. Print at 100% scale ("fit to page"
   off) and check a black square with a ruler: 6.0 cm.
2. **Camera:** set `camera.index` in `config.json` (the overhead camera is `1` on the dev laptop). Mount it
   50 to 80 cm above the shelf, pointing straight down.
3. **Calibrate bays:** `python -m vision.calibrate`. Drag one rectangle per bay in order, `s` to save.
4. **Shelf controller:** flash `firmware/shelf_esp32` with PlatformIO (`pio run -t upload`). Find its port in
   Device Manager → Ports (COM & LPT) and set `serial.port` in `config.json` (e.g. `"COM4"`).

Run the tests:

```powershell
python -m pytest -q
```

## Run

`scripts/run_all.sh` starts the backend (uvicorn on `0.0.0.0:8000`) and the vision worker. Output is prefixed
`[api]` / `[vision]`, and Ctrl+C stops both. Extra arguments go to the worker (e.g. `--no-window`).

On Windows, run it from **Git Bash**:

```bash
scripts/run_all.sh
```

Or start the two processes yourself in two PowerShell windows (venv activated in each):

```powershell
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

```powershell
python -m vision.worker
```

No camera? Feed the backend fake shelf snapshots instead of starting the worker:

```powershell
python scripts/fake_shelf.py loop
```

Check it's up: `Invoke-RestMethod http://localhost:8000/api/health` (or `curl localhost:8000/api/health`).

Pages: `http://<laptop-ip>:8000/store.html` (shopper cart) and `http://<laptop-ip>:8000/admin.html` (team panel).
Find the laptop IP with `ipconfig`. The phone must be on the same Wi-Fi or on the laptop hotspot.

## Feature flags

All in `config.json` → `features`. Core features (backend, shelf vision, cart, live phone cart, checkout,
demo tools) have no flag and can't be turned off.

| Flag | Feature | Default | When `false` |
|---|---|---|---|
| `motion_freeze` | F3: ignore a bay while a hand is in it | `true` | Bays count as stable after `stable_ms`; more flicker risk |
| `signup` | F6: member signup | `true` | Everyone uses the Demo Shopper |
| `passkeys` | F7: Face ID / fingerprint via WebAuthn | `true` | "Confirm" buttons replace Face ID; HTTPS not required |
| `gates` | F8: QR entry/exit gates, one-shopper lock | `false` on this machine for now | "Start shopping" / "Checkout" buttons on the store page |
| `stripe` | F10: Stripe test-mode charge | `true` | Mock payment provider only |
| `llm` | F11: LLM phrasing of the agent line | `true` | Template lines only (also the case when no API key is set) |
| `hardware_leds` | F12: shelf controller over USB serial | `true` | No serial; overlay + phone only |
| `yolo` | F13: YOLO detection fused with tags | `false` | Tags only |
| `https_tunnel` | F14: ngrok static domain | `true` | LAN http only (needs `passkeys` off) |
| `loyalty` | F15: points on the receipt | `true` | No points |
| `load_cells` | F16: HX711 load cells | `false` | Default |

Cut order if behind (cut from the top first): load cells → YOLO → loyalty → Stripe → passkeys → LLM (templates
stay) → LEDs.

## Reset the demo

Do this before every judge:

1. Open `admin.html`, log in, press **Reset**. This cancels the active session, clears overrides, frees the
   store lock and sets the LEDs to idle. Holding the controller's button for 1 s does the same.
2. Put every item back in its home bay and check the overlay: all bays green, every tag ID visible.
3. Check the admin badges: vision age under 500 ms, serial connected.

From PowerShell, without the browser:

```powershell
$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-RestMethod -Method Post http://localhost:8000/admin/login -WebSession $s -ContentType application/json -Body '{"password":"<ADMIN_PASSWORD>"}'
Invoke-RestMethod -Method Post http://localhost:8000/admin/reset -WebSession $s
```

The full pre-judge checklist is in [docs/DEMO.md](docs/DEMO.md).

## Logs

- `data/events.log.jsonl`: one JSON object per line (shelf changes, cart changes, sessions, agent lines, admin
  actions). The admin page shows the last 50.
- `data/speedmart.db`: SQLite (members, passkeys, sessions, payments).

## Team

| Role | Owns | Name |
|---|---|---|
| HW: hardware / firmware | shelf, camera mount, shelf controller, LEDs | _TBD_ |
| VA: vision + API | vision worker, backend, passkeys, Stripe | _TBD_ |
| APP: app + story | phone pages, agent, demo script, Devpost | _TBD_ |
