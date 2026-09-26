# SpeedMart — Build Spec for Claude Code

**Event:** HackGT 13 (Fri Sep 25, 8:00 PM → Sun Sep 27, 8:00 AM, Atlanta)
**Track:** The Shipyard (Hardware) + Visa challenge (Reimagine Shopping with Generative AI)
**One line:** Sign up once. Face ID at the door. Grab items off a camera-watched shelf and they appear in your cart live. Face ID at the exit approves the charge.

---

## 0. INSTRUCTIONS FOR CLAUDE CODE (read first, follow always)

You are implementing this project step by step from this file. Rules:

1. **Work one step at a time.** Steps live in Section 10 and have IDs like `S1.2`. Only implement the step you are asked to implement. Do not jump ahead.
2. **Every step has an Acceptance section.** A step is done only when every acceptance check passes. Run the listed commands yourself where possible and report results. If a check needs a human (phone, camera, physical shelf), say exactly what the human should do and what they should see.
3. **Respect feature flags.** Every optional feature has a flag in `config.json` → `features`. Code for a feature must be guarded by its flag so turning it off never breaks the core loop. If a flag is `false`, do not build that feature unless asked.
4. **Do not invent features.** If something is not in this file, ask before adding it. No accounts beyond what Section 6 defines, no extra pages, no extra services.
5. **Backend is the single source of truth.** The phone UI never computes the cart, totals, or payment status. It renders snapshots from the backend.
6. **Keep dependencies minimal.** Python 3.11, FastAPI, plain HTML/JS frontend with no build step. Only add packages listed in Section 3 unless asked.
7. **Never hardcode secrets.** API keys go in `.env` and are read via `backend/settings.py`. `.env` is gitignored. Provide `.env.example`.
8. **Log everything important** to `data/events.log.jsonl` (one JSON object per line) so the team can debug live and screenshot for Devpost.
9. **When unsure about a library API, check the installed version** (`pip show <pkg>`, read its docs or source) instead of guessing. APIs referenced here: OpenCV ≥ 4.8 (`cv2.aruco.ArucoDetector`), `webauthn` (py_webauthn) ≥ 2.0, `@simplewebauthn/browser` v13, `stripe` Python ≥ 10, `ultralytics` ≥ 8.3.
10. **Be honest in UI copy.** Anything mocked is labeled "sandbox" or "demo". Never display text implying a real bank charge.

---

## 1. THE DEMO JOURNEY (what the judge experiences)

1. **Signup.** Judge scans **QR A (signup)** on the table → opens `https://<domain>/` on their phone → enters first name → phone prompts Face ID / fingerprint to create a passkey → a sandbox Visa test card is linked automatically → "You're a member of SpeedMart Market."
2. **Entry.** Judge scans **QR B (entry gate)** → app asks for Face ID → verified → store door "opens" (gate LED turns green, phone shows "Welcome in, Maya") → store session starts. Only one shopper can be inside at a time.
3. **Shopping.** Judge picks an item off the shelf. The overhead camera sees the item leave its bay. Within ~1 second the item appears in the phone cart. The bay LED turns off. A one-sentence AI store agent line appears ("Electrolytes added. A recovery drink is $4 and keeps you under your $20 budget.").
4. **Put back.** Judge puts an item back. It disappears from the cart. (Proves it's live, not scripted.)
5. **Exit.** Judge scans **QR C (exit gate)** → phone shows itemized receipt and total → Face ID to approve → sandbox charge runs → shelf flashes green → phone shows "Paid · auth code · points earned".
6. **Continue (return).** Within 30 minutes, on the receipt: "Return an item" → Face ID → put the item back on its bay → the camera sees it return and the phone lists it → "Confirm refund $X" → sandbox refund of exactly those items plus tax → gate screen shows REFUNDED.

The four stages of Visa's framework map onto this: Discover (agent line, F18 plan), Decide (live cart, budget,
permissions card), Transact (scoped instruction + Face ID), Continue (camera-verified returns and refunds).

**Definition of "in the cart":** an item is in the shopper's cart when it has left the shelf (not visible in any bay). An item returned to any bay is no longer in the cart.

---

## 2. FEATURE MODULES AND FLAGS

Each feature has an ID and a flag. The human will decide which to keep. **Core** features cannot be turned off; everything else can.

| ID | Feature | Flag in `config.json.features` | Core? | Depends on | Est. build time |
|---|---|---|---|---|---|
| F1 | Backend API + SQLite + event log | — | Core | — | 1.5 h |
| F2 | Shelf vision with ArUco tags | — | Core | F1 | 3 h |
| F3 | Motion freeze (ignore bays while a hand is in them) | `motion_freeze` | Strongly recommended | F2 | 1 h |
| F4 | State-based cart (cart = baseline minus what's on shelf) | — | Core | F2 | 1.5 h |
| F5 | Live phone cart over WebSocket | — | Core | F1, F4 | 2 h |
| F6 | Signup / member account | `signup` | Optional | F1 | 1 h |
| F7 | Passkey biometric (WebAuthn Face ID / fingerprint) | `passkeys` | Optional | F6, F14 | 3 h |
| F8 | Entry + exit gates via QR, one-shopper store lock | `gates` | Optional (else a single "Start" button) | F1 | 1.5 h |
| F9 | Checkout on exit with Visa Intelligent Commerce–shaped instruction (mock) | — | Core | F4 | 1 h |
| F10 | Stripe test-mode charge (real sandbox API, test Visa card) | `stripe` | Optional | F9 | 1.5 h |
| F11 | AI store agent line (LLM + template fallback) | `llm` | Optional (templates always on) | F4 | 1 h |
| F12 | ESP32 shelf + gate LEDs over USB serial | `hardware_leds` | Optional | F1 | 2 h |
| F13 | YOLO product detection fused with tags | `yolo` | Optional | F2 | 4 h incl. data |
| F14 | HTTPS tunnel with stable domain | `https_tunnel` | Required if F7 on | — | 0.5 h |
| F15 | Receipt + loyalty points | `loyalty` | Optional | F9 | 0.5 h |
| F16 | Load cells via HX711 per bay | `load_cells` | Optional, default OFF | F12 | 3 h |
| F17 | Demo tools: admin panel, override keys, reset, demo account | — | Core | F1 | 1.5 h |
| F18 | "Tell us what you need": intent planner (LLM proposes a plan, the store validates it against stock and budget) | `llm` | Optional (keyword planner always on) | F4, F6 | 2 h |
| F19 | Voice store agent (ElevenLabs Agents): the shopper talks to the store; the agent acts only through client tools over the store's own endpoints. See `docs/voice.md` | `voice` | Optional (typed F18 flow is the fallback) | F18, F14 | 2 h |

**Default config for the weekend:** all flags `true` except `yolo` (turn on Saturday if ahead) and `load_cells` (off).

**Cut order if behind (cut from the top first):** F16 → F13 → F15 → F10 → F7 → F11 LLM (templates stay) → F12. Never cut: F1, F2, F4, F5, F9, F17.

If F7 (passkeys) is cut: signup, entry, and exit use a big "Confirm" button instead of Face ID. If F8 (gates) is cut: a "Start shopping" and "Checkout" button replace the QR gates. The rest of the system does not change.

---

## 3. TECH STACK

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python 3.11, FastAPI, uvicorn | One process, `backend/main.py` |
| DB | SQLite via stdlib `sqlite3` | File at `data/SpeedMart.db`. No ORM. |
| Sessions | Starlette `SessionMiddleware` (signed cookie) | Needs `itsdangerous` |
| Realtime | FastAPI WebSocket `/ws` | Broadcast cart snapshots |
| Vision | OpenCV (`opencv-contrib-python`), NumPy | Separate process, `vision/worker.py` |
| Detection (opt.) | `ultralytics` YOLO11n | Separate model file in `models/` |
| Passkeys | Backend `webauthn` (py_webauthn); frontend `@simplewebauthn/browser` v13 via CDN | Requires HTTPS + stable domain |
| Payments (opt.) | `stripe` Python SDK in test mode | Test PaymentMethod `pm_card_visa` |
| LLM (opt.) | `httpx` calling Anthropic or OpenAI-compatible API | 2.5 s timeout, template fallback |
| Serial | `pyserial` | Backend talks to ESP32 |
| Frontend | Plain HTML + vanilla JS + one CSS file | No bundler, no framework |
| QR codes | `qrcode[pil]` | Script generates printable PNGs |
| Tunnel | ngrok with free static domain (or Cloudflare named tunnel on own domain) | Domain must NOT change between restarts |
| Firmware | Arduino framework on ESP32, `Adafruit_NeoPixel` | PlatformIO or Arduino IDE |

`requirements.txt`:

```
fastapi>=0.115
uvicorn[standard]>=0.30
itsdangerous>=2.2
python-dotenv>=1.0
httpx>=0.27
pyserial>=3.5
opencv-contrib-python>=4.8
numpy>=1.26
webauthn>=2.0
stripe>=10.0
qrcode[pil]>=7.4
```

Optional (only if F13 on): `ultralytics>=8.3`.

---

## 4. FILE STRUCTURE

```
SpeedMart/
├── README.md                    # how to run, team roles, demo steps
├── SpeedMart_BUILD_SPEC.md          # this file
├── .env.example                 # every env var with a comment
├── .gitignore                   # .env, data/, models/*.pt, __pycache__, .venv
├── requirements.txt
├── config.json                  # feature flags, camera, bays, serial, budget
├── catalog.json                 # SKUs and tag→SKU units
│
├── backend/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, mounts routers + static web/
│   ├── settings.py              # loads .env + config.json + catalog.json
│   ├── db.py                    # sqlite connection, schema creation, helpers
│   ├── eventlog.py              # append JSON lines to data/events.log.jsonl
│   ├── ws.py                    # WebSocket connection manager + broadcast
│   ├── store.py                 # store lock, store sessions, state machine
│   ├── cart.py                  # baseline vs shelf state → cart, totals, tax
│   ├── shelf_state.py           # latest shelf snapshot from vision, stability
│   ├── auth_passkeys.py         # WebAuthn registration + authentication routes
│   ├── members.py               # signup, /me, demo account
│   ├── payments.py              # VIC-shaped instruction + mock/Stripe charge
│   ├── agent.py                 # AI store agent line: LLM + templates
│   ├── serial_bridge.py         # ESP32 serial read/write thread
│   ├── admin.py                 # admin routes: reset, overrides, logs, metrics
│   ├── returns.py               # Continue stage: returns + camera-verified refunds (8.1, 8.11, 8.12)
│   └── routes_api.py            # public API routes (catalog, session, gates, receipt, guardrails)
│
├── vision/
│   ├── worker.py                # main loop: camera → snapshot → POST /internal/shelf
│   ├── camera.py                # open camera, lock exposure/focus, read frames
│   ├── calibrate.py             # click to draw bay ROIs, saves into config.json
│   ├── aruco_detect.py          # tag detection → per-bay unit sets
│   ├── motion.py                # per-bay motion detection (F3)
│   ├── yolo_detect.py           # YOLO inference → per-bay SKU counts (F13)
│   ├── fusion.py                # combine tags + YOLO into per-bay SKU counts
│   └── overlay.py               # debug window drawing
│
├── training/                    # F13 only
│   ├── capture.py               # save frames from the overhead camera for labeling
│   ├── data.yaml                # YOLO dataset config (exported from Roboflow)
│   └── TRAINING.md              # exact steps to label + train + export
│
├── firmware/
│   └── shelf_esp32/
│       ├── platformio.ini
│       └── src/main.cpp         # LEDs + serial protocol (+ HX711 if F16)
│
├── web/                         # served at / by FastAPI
│   ├── index.html               # landing / signup
│   ├── enter.html               # entry gate page (QR B target)
│   ├── store.html               # live cart while shopping
│   ├── exit.html                # exit gate: receipt review + approve (QR C target)
│   ├── receipt.html             # paid receipt + points
│   ├── admin.html               # team-only control panel
│   ├── js/
│   │   ├── api.js               # fetch helpers, error toasts
│   │   ├── passkey.js           # wraps SimpleWebAuthnBrowser
│   │   ├── ws.js                # WebSocket with auto-reconnect
│   │   ├── guardrails.js        # "Your agent's permissions" card (8.10)
│   │   └── pages/*.js           # one small script per page
│   └── css/styles.css
│
├── scripts/
│   ├── gen_aruco.py             # printable PDF of tags at exact size
│   ├── gen_qr.py                # printable QR A/B/C with labels
│   ├── run_all.sh               # starts backend + vision worker
│   └── reset_demo.sh            # calls admin reset
│
├── models/                      # YOLO weights (gitignored)
├── data/                        # SpeedMart.db, events.log.jsonl, payments.log.jsonl (gitignored)
└── docs/
    ├── DEMO.md                  # 90-second script + judge Q&A
    └── DEVPOST.md               # write-up draft
```

---

## 5. CONFIGURATION FILES

### 5.1 `.env.example`

```
# Session cookie signing
SESSION_SECRET=change-me-long-random-string

# Public HTTPS origin (must match the tunnel domain exactly, no trailing slash)
PUBLIC_ORIGIN=https://SpeedMart-demo.ngrok-free.app
# WebAuthn relying party id = the domain only
RP_ID=SpeedMart-demo.ngrok-free.app
RP_NAME=SpeedMart Market

# Shared secret the vision worker sends on /internal/* routes
INTERNAL_TOKEN=change-me-too

# Admin panel password
ADMIN_PASSWORD=change-me-three

# Gate tokens embedded in QR B (entry) and QR C (exit) URLs
ENTRY_GATE_TOKEN=entry-7d2f
EXIT_GATE_TOKEN=exit-91ac

# Stripe test mode (F10). Must start with sk_test_
STRIPE_SECRET_KEY=

# LLM (F11). Set one provider.
LLM_PROVIDER=anthropic          # anthropic | openai
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-haiku-4-5
OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_BASE_URL=https://api.openai.com/v1
```

### 5.2 `config.json`

```json
{
  "features": {
    "motion_freeze": true,
    "signup": true,
    "passkeys": true,
    "gates": true,
    "stripe": true,
    "llm": true,
    "hardware_leds": true,
    "yolo": false,
    "https_tunnel": true,
    "loyalty": true,
    "load_cells": false
  },
  "store": {
    "name": "SpeedMart Market #01",
    "currency": "USD",
    "tax_rate": 0.08,
    "default_budget_usd": 20,
    "max_session_minutes": 15
  },
  "camera": {
    "index": 0,
    "width": 1280,
    "height": 720,
    "fps": 30,
    "auto_exposure": false,
    "exposure": -6,
    "autofocus": false,
    "focus": 0
  },
  "vision": {
    "aruco_dict": "DICT_4X4_50",
    "snapshot_hz": 5,
    "stable_ms": 400,
    "motion_threshold": 0.02,
    "motion_settle_ms": 300,
    "yolo_model": "models/SpeedMart_yolo.pt",
    "yolo_conf": 0.55,
    "yolo_every_n_frames": 3
  },
  "bays": [
    {"id": 0, "roi": [80, 200, 360, 620], "sku": "elx", "led_index": 0},
    {"id": 1, "roi": [400, 200, 680, 620], "sku": "rec", "led_index": 1},
    {"id": 2, "roi": [720, 200, 1000, 620], "sku": "bar", "led_index": 2}
  ],
  "serial": {
    "port": "/dev/ttyUSB0",
    "baud": 115200,
    "gate_led_index": 3
  }
}
```

ROIs are `[x1, y1, x2, y2]` in pixels at the configured resolution. `calibrate.py` overwrites them.

### 5.3 `catalog.json`

Every **physical unit** has its own ArUco tag ID. Multiple units can share a SKU.

```json
{
  "skus": [
    {"sku": "elx", "name": "Electrolyte tabs", "price_usd": 8.00, "tags": ["recovery"], "yolo_class": "electrolytes"},
    {"sku": "rec", "name": "Recovery drink",   "price_usd": 4.00, "tags": ["drink", "complements:elx"], "yolo_class": "recovery_drink"},
    {"sku": "bar", "name": "Protein bar",      "price_usd": 3.50, "tags": ["snack"], "yolo_class": "protein_bar"}
  ],
  "units": [
    {"tag_id": 0, "sku": "elx", "home_bay": 0},
    {"tag_id": 1, "sku": "elx", "home_bay": 0},
    {"tag_id": 2, "sku": "rec", "home_bay": 1},
    {"tag_id": 3, "sku": "rec", "home_bay": 1},
    {"tag_id": 4, "sku": "bar", "home_bay": 2},
    {"tag_id": 5, "sku": "bar", "home_bay": 2}
  ]
}
```

To add SKUs 4 and 5 (chips, water) later: add rows to `skus`, `units`, and `bays`. No code changes.

---

## 6. DATA MODEL (SQLite, `backend/db.py`)

Create tables on startup if missing. All timestamps are ISO 8601 UTC strings. IDs are short random strings with a prefix (`mem_`, `ses_`, `pay_`) from `secrets.token_urlsafe(8)`.

```sql
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,               -- mem_xxx
  name TEXT NOT NULL,
  budget_usd REAL NOT NULL,
  dietary TEXT,                      -- optional tag, e.g. "vegan"; used by agent
  stripe_customer_id TEXT,           -- F10
  stripe_pm_id TEXT,                 -- F10
  card_label TEXT,                   -- "Visa •••• 4242 (test)"
  points INTEGER NOT NULL DEFAULT 0, -- F15
  is_demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passkeys (          -- F7
  credential_id TEXT PRIMARY KEY,              -- base64url
  member_id TEXT NOT NULL REFERENCES members(id),
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL,
  transports TEXT,                             -- JSON array
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_sessions (
  id TEXT PRIMARY KEY,                         -- ses_xxx
  member_id TEXT NOT NULL REFERENCES members(id),
  state TEXT NOT NULL,                         -- see 7.1
  baseline_json TEXT NOT NULL,                 -- {"elx": 2, "rec": 2, "bar": 2}
  final_cart_json TEXT,                        -- frozen at CHECKOUT_PENDING
  started_at TEXT NOT NULL,                    -- measured results: "entered"
  ended_at TEXT,
  return_of TEXT,                              -- RETURNING sessions: the paid session being returned
  first_pick_at TEXT,                          -- measured results: first item in the cart
  quoted_at TEXT,                              -- measured results: last exit scan (quote)
  approved_at TEXT                             -- measured results: payment approved
);

CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,                         -- pay_xxx
  store_session_id TEXT NOT NULL REFERENCES store_sessions(id),
  member_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  provider TEXT NOT NULL,                      -- "mock" | "stripe_test"
  provider_ref TEXT,                           -- Stripe PaymentIntent id
  status TEXT NOT NULL,                        -- AUTHORIZED | DECLINED | ERROR
  auth_code TEXT,
  instruction_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (           -- Continue stage (8.11)
  id TEXT PRIMARY KEY,                         -- ref_xxx
  payment_id TEXT NOT NULL REFERENCES payments(id),
  store_session_id TEXT NOT NULL REFERENCES store_sessions(id),   -- the paid visit
  return_session_id TEXT NOT NULL REFERENCES store_sessions(id),  -- the RETURNING session
  member_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  provider TEXT NOT NULL,                      -- "mock" | "stripe_test"
  provider_ref TEXT,                           -- Stripe Refund id (re_...) or re_mock_...
  status TEXT NOT NULL,                        -- SUCCEEDED | FAILED
  items_json TEXT NOT NULL,                    -- [{"sku","name","qty"}]
  points_removed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
```

`init_db()` adds the `store_sessions` columns after `ended_at` to an older `data/speedmart.db` with
`ALTER TABLE ... ADD COLUMN`, so no reset is needed.

Seed a **demo member** on startup if none exists: name "Demo Shopper", `is_demo=1`, budget 20, linked test card if Stripe is on.

---

## 7. STATE MACHINES AND RULES

### 7.1 Store session states (`backend/store.py`)

```
            gate/enter (verified)
  (none) ─────────────────────────▶ IN_STORE
                                     │   ▲
               gate/exit/quote       │   │ gate/exit/cancel
                                     ▼   │
                                CHECKOUT_PENDING
                                  │         │
             approve + charge OK  │         │ approve + charge DECLINED
                                  ▼         ▼
                                PAID     CHECKOUT_PENDING (show error, allow retry or cancel)
                                  │
                   receipt viewed │ or 60 s
                                  ▼
                                CLOSED

  Any state ──admin reset / force-exit / timeout──▶ CANCELLED
  Exit with empty cart ──▶ CLOSED (no payment, "Nothing to pay, see you soon")

  Continue stage: a NEW session with return_of = the paid visit
                       returns/start (Face ID, within 30 min of payment)
  PAID or CLOSED visit ──────────────────────────────────────────────▶ RETURNING
                                                                          │
        returns/confirm + refund SUCCEEDED ──▶ CLOSED (refunded)  ◀───────┤
        returns/cancel                     ──▶ CLOSED (no refund) ◀───────┤
        3 min timeout / admin reset        ──▶ CANCELLED (no refund) ◀────┘
```

**Store lock:** at most one session may be in `IN_STORE`, `CHECKOUT_PENDING` or `RETURNING`. `gate/enter` while occupied returns `409 {"error":"store_occupied","occupant_first_name":"Maya"}`. Implement with a `threading.Lock` around state changes plus a DB check.

**Timeout:** a session in `IN_STORE` longer than `store.max_session_minutes` becomes `CANCELLED` (background task checks every 30 s). A `RETURNING` session older than 3 minutes becomes `CANCELLED` with no refund (same task; `returns/confirm` also refuses an expired one). Admin can always force-exit.

**Returning:** baseline = shelf counts when the return starts. `returned[sku] = min(max(0, shelf_now − baseline), purchased − already_refunded)`; any other rise is ignored and listed as "not from this purchase". The refund is computed like the cart (never accumulated): `subtotal = Σ returned × unit price paid`, `tax = round(subtotal × tax_rate)` in integer cents, capped at what is left of the payment; returning everything that is left refunds exactly what is left, so several partial returns never add up to more than was charged. A returning session has no cart (`CartSnapshot.items` is always empty) and never charges anything.

**Cart freezing:** on `gate/exit/quote`, copy the current cart into `final_cart_json`. The charge uses the frozen cart, not the live one. If the shopper cancels, unfreeze.

### 7.2 Fresh verification rule (F7)

If `passkeys` is on, `gate/enter`, `gate/exit/approve` and `returns/start` require a passkey authentication completed within the last 90 seconds for the same member (`request.session["verified_at"]`, `request.session["verified_member"]`). The flag is consumed after one use. If `passkeys` is off, these routes require only a logged-in member.

### 7.3 Cart rule (F4), the heart of the system

The cart is **computed, never accumulated**:

```
baseline[sku]   = count of that SKU on the shelf when the shopper entered
shelf_now[sku]  = count of that SKU currently seen in ANY bay (stable bays only)
override[sku]   = manual admin adjustment (default 0)
cart_qty[sku]   = max(0, baseline[sku] - shelf_now[sku] - override_return[sku] + override_remove[sku])
```

Simplify overrides as a single signed integer `override[sku]` added to cart_qty, then clamp to `[0, baseline[sku]]`.

Consequences, all automatic:
- Pick → shelf count drops → item in cart.
- Put back in any bay → shelf count rises → item leaves cart.
- Duplicate or repeated vision frames cannot double count.
- Items missing before the shopper entered are never charged.
- Restarting the backend during a session recomputes the correct cart from baseline + current shelf.

**Misplaced items:** a unit tag seen in a bay that is not its `home_bay` counts as "on shelf" (not in cart) and is listed in `warnings` so the phone shows "Protein bar is in the wrong bay" and the agent line mentions it.

**Unstable bays:** if a bay is not `stable` in the latest snapshot (hand inside, or content still changing), keep that bay's last stable contents. This is what prevents flicker when a hand covers a tag.

**Totals:** `subtotal = Σ qty × price`, `tax = round(subtotal × tax_rate, 2)`, `total = subtotal + tax`. Use integer cents internally to avoid float drift; convert to dollars for display.

### 7.4 Agent rule (F11)

A deterministic **policy** decides what to say. The LLM only **phrases** it. This keeps the agent reliable and gives a clean judge answer ("the policy is deterministic; generative AI explains it").

Policy (first match wins):
1. Cart empty → `{"kind":"empty"}`
2. Misplaced warning exists → `{"kind":"misplaced","sku":...,"bay":...}`
3. `total > budget` → `{"kind":"over_budget","put_back": most expensive SKU in cart, "over_by": ...}`
4. Some SKU in cart has a complement (catalog tag `complements:<sku>` on another SKU) not in cart, and `total + complement price (with tax) ≤ budget` → `{"kind":"suggest","sku":...,"price":...,"remaining_after":...}`
5. Otherwise → `{"kind":"ok","remaining": budget - total}`

---

## 8. API CONTRACT (`backend/routes_api.py`, `auth_passkeys.py`, `members.py`, `admin.py`)

All JSON. Errors are `{"error": "<code>", "message": "<human text>"}` with a proper status code.

### 8.1 Public

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `/api/health` | — | `{"ok":true,"vision_age_ms":int,"serial":bool}` | `vision_age_ms` = ms since last shelf snapshot |
| GET | `/api/config/public` | — | features, store name, currency | Frontend reads flags from here |
| GET | `/api/catalog` | — | `{"skus":[{sku,name,price_usd}],"bays":[{bay,card,sku,name,on_shelf}]}` | `bays` feeds the shelf map: `card` is the printed number (bay id 0 is card 1), `on_shelf` the SKU's units on the shelf now (`null` before the first snapshot) |
| POST | `/api/members/signup` | `{"name":str,"budget_usd"?:num,"dietary"?:str}` | `{"member":{...}}` | Creates member, logs in via cookie. If F10 on, creates Stripe customer + attaches `pm_card_visa` |
| GET | `/api/me` | — | member + `has_passkey` + active session id | 401 if not logged in |
| POST | `/api/logout` | — | `{"ok":true}` | |
| POST | `/api/passkey/register/options` | — | WebAuthn creation options JSON | Logged-in member only |
| POST | `/api/passkey/register/verify` | `{"credential":{...}}` | `{"ok":true}` | Saves passkey |
| POST | `/api/passkey/login/options` | `{"purpose":"enter"\|"exit"\|"login"\|"return"}` | WebAuthn request options JSON | Works with no cookie (discoverable credentials) |
| POST | `/api/passkey/login/verify` | `{"credential":{...},"purpose":...}` | `{"ok":true,"member":{...}}` | Logs in + sets fresh verification |
| POST | `/api/gate/enter` | `{"gate_token":str}` | `{"session":{...},"cart":CartSnapshot}` | 403 bad token, 401 not verified, 409 occupied, 409 `returning` (your own return is open) |
| GET | `/api/store/current` | — | `{"session":...,"cart":CartSnapshot}` or `{"session":null}` | |
| POST | `/api/gate/exit/quote` | `{"gate_token":str}` | `{"cart":CartSnapshot,"instruction":Instruction,"plan_check":PlanCheck\|null}` | Freezes cart, state → CHECKOUT_PENDING. `plan_check` (8.9) is `null` unless the shopper made an F18 plan this visit. `POST /api/dev/checkout`, the gates-off fallback, returns the same three keys |
| POST | `/api/gate/exit/approve` | — | `{"payment":Payment}` | Needs fresh verification if F7 on |
| POST | `/api/gate/exit/cancel` | — | `{"ok":true}` | Back to IN_STORE |
| GET | `/api/receipt/{session_id}` | — | receipt with items, payment, points, `in_and_out_s`, `approvals`, `refunds: [Refund]`, `refunded_usd`, `return: {eligible, reason, message, deadline}` | Only the owner. `in_and_out_s` = entered → approved; `approvals` = payment attempts, each one Face ID (or Confirm) tap |
| GET | `/api/guardrails` | — | `Guardrails` (8.10) | Logged-in member. 401 otherwise |
| POST | `/api/returns/start` | `{"session_id":str}` (the paid visit) | `{"return":ReturnSnapshot}` | Needs fresh verification if F7 on (7.2). 404 not yours, 409 `not_returnable` / `return_window_closed` (30 min after payment) / `nothing_to_return` / `store_occupied` (with `occupant_first_name`), 503 `vision_unavailable`. Refusals that need no Face ID come first. The same visit tapped twice returns the open return |
| GET | `/api/returns/current` | — | `{"return":ReturnSnapshot\|null}` | Your open return, if any |
| POST | `/api/returns/confirm` | — | `{"refund":Refund,"agent_line":str}`, or `{"refund":Refund,"message":str}` when the refund FAILED | Refunds exactly the detected items + tax, then RETURNING → CLOSED. 409 `no_active_return` / `nothing_returned` / `return_expired` |
| POST | `/api/returns/cancel` | — | `{"ok":true}` | RETURNING → CLOSED, no refund |
| WS | `/ws` | — | stream of messages (8.4) | Identifies member by cookie |
| POST | `/api/intent` | `{"text":str}` (≤ 300 chars) | `Plan` (8.8) | F18. Logged-in member. 401 `not_logged_in`, 400 `empty_text` / `text_too_long`, 404 `unknown_member` |
| GET | `/api/intent/current` | — | `Plan` (8.8) or `null` | F18. 401 if not logged in |
| DELETE | `/api/intent` | — | `{"ok":true}` | F18. Clears the plan; its bays stop glowing (`plan_bays` with `[]`) |

### 8.2 Internal (vision worker → backend)

`POST /internal/shelf` with header `X-Internal-Token: <INTERNAL_TOKEN>`. Body:

```json
{
  "ts": 1727222400123,
  "frame_id": 18422,
  "bays": [
    {"bay": 0, "stable": true, "motion": false, "units": [0, 1], "yolo_counts": {"elx": 2}},
    {"bay": 1, "stable": false, "motion": true, "units": [2], "yolo_counts": {}},
    {"bay": 2, "stable": true, "motion": false, "units": [4, 5, 3], "yolo_counts": {}}
  ],
  "loose_units": [7]
}
```

`units` = unit tag IDs whose center lies in that bay's ROI. `loose_units` = tags seen outside every ROI (item in a hand). `yolo_counts` present only if F13 on.

### 8.3 Admin (team only)

Protected by admin cookie set via `POST /admin/login {"password":...}`.

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/admin/state` | — | Full state: lock, session, baseline, shelf, cart, `return` (ReturnSnapshot while RETURNING, else null), `metrics` (below), last 50 events, serial status |
| POST | `/admin/reset` | — | Cancel active session, clear overrides, clear lock, LEDs idle |
| POST | `/admin/force-exit` | — | Cancel active session only |
| POST | `/admin/override` | `{"sku":"elx","delta":1}` | Adjust cart qty for that SKU by delta (manual fallback). Logged as `source:"override"` |
| POST | `/admin/demo-login` | — | Logs this browser in as the demo member |
| POST | `/admin/led` | `{"cmd":"DISP,IDLE"}` | Raw serial command (gate screen test) |
| POST | `/admin/force-decline` | `{"on":bool}` | Next charge returns DECLINED (for Q&A demos) |

`metrics` (the laptop's local day): `{"since", "sessions_today", "paid_today", "avg_in_store_s", "avg_exit_to_approval_s", "refunds_today", "refunded_usd_today"}`. Shopping sessions only (returns are not sessions); averages are over paid visits, `null` when there are none. The raw timestamps are also logged: `first_pick`, `session_metrics` (at approval: entered, first pick, quote, approved, seconds between), `return_started`, `return_detected`, `refund`.

### 8.4 WebSocket messages (server → client)

```json
{"type":"cart","data":CartSnapshot}
{"type":"agent","data":{"line":"Electrolytes added. A recovery drink is $4 and keeps you under $20."}}
{"type":"gate","data":{"event":"entered"|"exit_pending"|"paid"|"declined"|"cancelled"|"return_started"|"refunded"}}
{"type":"return","data":ReturnSnapshot}   // the returning member + admins, on every shelf change
{"type":"store_status","data":{"occupied":true}}
{"type":"plan_bays","data":{"bays":[0,1]}}   // public: bays the current plan points at, [] when cleared
{"type":"shelf","data":{...}}          // admin sockets only
{"type":"log","data":{...}}            // admin sockets only
```

Clients reconnect with exponential backoff (0.5 s → 4 s max) and call `GET /api/store/current` on reconnect.

`plan_bays` goes to every socket (the kiosk is not signed in) and carries bay ids only, never who made the
plan. It is sent when a plan is created (unless someone else is in the store) and with `[]` when it is cleared,
when the session ends, or when a new shopper enters. Only changes are sent; a socket that connects while bays
are glowing gets it among its first messages.

### 8.5 CartSnapshot

```json
{
  "session_id": "ses_Ab3xY9",
  "state": "IN_STORE",
  "items": [
    {"sku": "elx", "name": "Electrolyte tabs", "qty": 1, "unit_price_usd": 8.00, "line_total_usd": 8.00}
  ],
  "subtotal_usd": 8.00,
  "tax_usd": 0.64,
  "total_usd": 8.64,
  "budget_usd": 20,
  "over_budget": false,
  "warnings": [],
  "agent_line": "Electrolytes added. A recovery drink is $4 and keeps you under $20.",
  "updated_at": "2026-09-26T02:14:03Z"
}
```

### 8.6 Instruction (VIC-shaped, `backend/payments.py`)

```json
{
  "instruction_id": "instr_7f3c",
  "label": "SANDBOX: structure modeled on Visa Intelligent Commerce concepts. Not a Visa API call.",
  "agent": {"id": "SpeedMart-shelf-agent-01", "name": "SpeedMart Store Agent"},
  "agent_token": {
    "token_ref": "tok_SpeedMart_ses_Ab3xY9",
    "scope": {
      "merchant": "SpeedMart Market #01",
      "max_amount_usd": 20.00,
      "currency": "USD",
      "single_use": true,
      "expires_at": "2026-09-26T02:29:03Z"
    }
  },
  "user_intent": "Pay $8.64 to SpeedMart Market #01 for 1 item",
  "items": [{"sku": "elx", "qty": 1, "unit_price_usd": 8.00}],
  "amount_usd": 8.64,
  "cardholder_confirmation": {"method": "passkey", "verified_at": "2026-09-26T02:14:01Z"},
  "created_at": "2026-09-26T02:13:50Z"
}
```

`max_amount_usd` = member budget. If `total > max_amount_usd`, approval is refused with `over_scope` and the phone tells the shopper to put something back or raise the budget. This is the "scoped agent token" story.

### 8.7 Payment

```json
{
  "payment_id": "pay_91Kd",
  "instruction_id": "instr_7f3c",
  "provider": "stripe_test",
  "provider_ref": "pi_3Q...",
  "amount_usd": 8.64,
  "currency": "USD",
  "status": "AUTHORIZED",
  "auth_code": "A1B2C3",
  "card_label": "Visa •••• 4242 (test)",
  "points_earned": 8,
  "created_at": "2026-09-26T02:14:03Z"
}
```

Write every payment (success or failure) to `data/payments.log.jsonl` as well as SQLite.

### 8.8 Plan (F18, `backend/intent.py`)

What the shopper asked for, turned into real SKUs. The LLM proposes; the store validates every plan against
the catalog, the stock on the shelf and the budget, and falls back to a keyword planner on any failure.
Plans live in memory, one per member, and are lost on a backend restart.

```json
{
  "plan_id": "pln_Ab3xY9",
  "goal_summary": "run recovery",
  "items": [
    {"sku": "elx", "name": "Electrolyte tabs", "qty": 1, "unit_price_usd": 8.00,
     "reason": "Replaces salts lost on your run"}
  ],
  "est_total_usd": 12.96,
  "budget_usd": 15.00,
  "fits_budget": true,
  "bays": [0, 1],
  "source": "llm",
  "created_at": "2026-09-26T02:14:03Z"
}
```

`est_total_usd` includes tax, like `CartSnapshot.total_usd`, so a plan and a cart compare like for like.
`budget_usd` is the **lowest** of the member's budget, any dollar amount in the text ("under $15") and the
LLM's own override — an override may only lower it. `source` is `"llm"` or `"rules"`. `bays` are the bays
holding the planned SKUs; they glow on every shelf map (`plan_bays`, 8.4) while the plan is current, and with
F12 on the gate screen shows `DISP,FIND,<cards>` ("Find bay 2 and 4") for 6 s. There are no bay LEDs.
An empty `items` list is a valid rules plan ("nothing on our shelf matches that yet"); an empty list from
the LLM is invalid and falls back to rules.

### 8.9 PlanCheck (F18)

Returned by the exit quote so the shopper can see how the cart compares with what they asked for. `qty` is
the shortfall in `missing` and the surplus in `extra`.

```json
{
  "matches": false,
  "missing": [{"sku": "rec", "name": "Recovery drink", "qty": 1}],
  "extra": [],
  "summary": "You asked for run recovery under $15: you still need Recovery drink, $8.64."
}
```

Pure function of the frozen `CartSnapshot` and the `Plan`. With no plan the quote returns `"plan_check": null`.

### 8.10 Guardrails ("Your agent's permissions")

What the agent may do on its own and where only the shopper decides. Built by the backend from the signed-in
member and the agent token's scope (8.6): the pending checkout's instruction when there is one (`source:
"instruction"`), else the scope this member's next instruction will carry (`source: "member"`). The limit
comes from `scope.max_amount_usd`, "A reusable payment token" only appears when `scope.single_use` is true,
and the approval wording follows F7 ("Face ID" or "a tap on Confirm"). Shown collapsed on `store.html` and
expanded on `exit.html` (`web/js/guardrails.js`).

```json
{
  "title": "Your agent's permissions",
  "agent_can": ["See the shelf", "Suggest items", "Build your cart"],
  "only_you": ["Approve a payment with Face ID", "Spend more than your $20 limit", "Request a refund"],
  "never": ["Your face data leaving your phone", "A reusable payment token", "A charge without your approval"],
  "scope": {"merchant": "SpeedMart #01", "max_amount_usd": 20.0, "currency": "USD", "single_use": true,
            "expires_at": "2026-09-26T02:29:03Z", "confirmation": "passkey"},
  "source": "member"
}
```

### 8.11 Refund (`backend/payments.py`)

```json
{
  "refund_id": "ref_8Hd2",
  "payment_id": "pay_91Kd",
  "session_id": "ses_Ab3xY9",
  "return_session_id": "ses_Rt7Qa1",
  "provider": "stripe_test",
  "provider_ref": "re_3Q...",
  "amount_usd": 8.64,
  "currency": "USD",
  "status": "SUCCEEDED",
  "items": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1}],
  "items_text": "1 Electrolyte tabs",
  "points_removed": 8,
  "card_label": "Visa •••• 4242 (test)",
  "created_at": "2026-09-26T02:20:11Z"
}
```

Stripe path (F10 on and the original payment was `stripe_test`): `stripe.Refund.create(payment_intent=<original
PaymentIntent>, amount=<cents>, metadata={session_id, return_session_id, payment_id},
idempotency_key=f"speedmart-refund-{return_session_id}-{attempt}")`. `succeeded` or `pending` → SUCCEEDED.
`CardError` → FAILED (the return stays open for a retry or cancel). Any other exception, F10 off, or a mock
payment → mock provider (`re_mock_` + hex, SUCCEEDED). Every refund goes to SQLite and to
`data/payments.log.jsonl` with `"type":"REFUND"` (charges are logged with `"type":"CHARGE"`). Loyalty: subtract
`floor(amount_usd)` points, never below zero. The agent line on success is fixed wording, not LLM phrased:
"Refund of $8.64 is on its way to your Visa ending 4242."

### 8.12 ReturnSnapshot

```json
{
  "session_id": "ses_Rt7Qa1",
  "state": "RETURNING",
  "original_session_id": "ses_Ab3xY9",
  "payment_id": "pay_91Kd",
  "returnable": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1}],
  "items": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1, "unit_price_usd": 8.00, "line_total_usd": 8.00}],
  "ignored": [{"sku": "rec", "name": "Recovery drink", "qty": 1, "message": "not from this purchase"}],
  "subtotal_usd": 8.00,
  "tax_usd": 0.64,
  "total_usd": 8.64,
  "expires_at": "2026-09-26T02:23:03Z",
  "updated_at": "2026-09-26T02:20:05Z"
}
```

`returnable` = bought in that visit and not yet refunded; `items` = detected returns (7.1 Returning), priced at
what was paid; `total_usd` is the refund `returns/confirm` will make.

---

## 9. CORE ALGORITHMS

### 9.1 Vision worker loop (`vision/worker.py`)

```
open camera (camera.py): set resolution, fps; if auto_exposure false, set manual exposure; if autofocus false, set focus
build ArUco detector once: cv2.aruco.ArucoDetector(getPredefinedDictionary(DICT_4X4_50), DetectorParameters())
if yolo on: load YOLO model once

per frame:
  gray = cvtColor(frame, BGR2GRAY); optionally CLAHE(clipLimit=2.0, tileGridSize=(8,8))
  corners, ids = detector.detectMarkers(gray)
  for each id: center = mean of 4 corners; assign to the bay whose ROI contains center, else loose
  motion per bay (9.2)
  yolo every N frames (9.4): per-bay SKU counts
  per bay stability (9.3)
  draw overlay (9.5); key handling (q quit, c recalibrate hint, space pause)
  every 1/snapshot_hz seconds: POST snapshot to /internal/shelf (timeout 0.5 s, never block the loop; use a background thread + queue of size 1 that keeps only the newest)
```

Only consider tag IDs that exist in `catalog.json.units`. Ignore unknown IDs.

### 9.2 Motion freeze (`vision/motion.py`, F3)

For each bay keep the previous grayscale ROI (blurred with 5×5 Gaussian).

```
diff = absdiff(roi_now, roi_prev)
changed_fraction = count(diff > 25) / roi_area
motion = changed_fraction > motion_threshold (default 0.02)
last_motion_ts[bay] = now if motion
```

A bay is **motion-free** when `now - last_motion_ts[bay] >= motion_settle_ms`.

### 9.3 Bay stability (`vision/worker.py`)

```
content = (sorted unit ids in bay, yolo_counts if on)
if content != candidate[bay]: candidate[bay] = content; candidate_since[bay] = now
stable = motion_free AND (now - candidate_since[bay] >= stable_ms)
```

Send `stable: true` with the content only when both hold. The backend ignores contents of unstable bays and keeps the last stable ones (7.3).

### 9.4 Tag + YOLO fusion (`vision/fusion.py`, F13)

Per bay, per SKU:

```
tag_count  = number of unit tags of that SKU in the bay
yolo_count = number of YOLO boxes of that SKU's yolo_class with conf ≥ yolo_conf whose center is in the bay
fused      = max(tag_count, yolo_count)
```

Rationale: tags can never overcount; YOLO fills in when a tag is covered or glared. If YOLO is off, `fused = tag_count`. The backend's `shelf_now[sku]` = sum of fused counts over all stable bays. When YOLO is on, the backend uses `yolo_counts` merged with tag counts using the rule above; when off, it counts `units`.

### 9.5 Overlay (`vision/overlay.py`)

Draw on a copy of the frame: bay rectangles (green = stable, yellow = motion/unstable), bay id + SKU name, detected tag outlines with ids, YOLO boxes with class + conf, FPS, and "snapshot OK / backend unreachable". Show in a window titled `SpeedMart shelf cam`. Keep this window visible on the laptop facing judges.

### 9.6 Agent line generation (`backend/agent.py`)

```
on cart change: schedule generate() with 1.2 s debounce (latest cart wins)
decision = policy(cart, member)             # 7.4
line = template(decision)                    # always computed first
if llm flag and key present:
    try: line = llm_phrase(decision, cart, member) with 2.5 s timeout
    validate: single line, ≤ 25 words, no emoji, mentions only catalog names; else keep template
broadcast {"type":"agent"} and store on snapshot
```

LLM system prompt:

```
You are the SpeedMart store agent inside a small smart shelf store.
Write exactly one sentence of at most 20 words for the shopper's phone.
Follow the DECISION exactly. Use only prices and product names given. No emojis, no hashtags, no quotes.
Friendly, brief, practical. Use the shopper's first name at most once.
```

LLM user message: JSON with `first_name`, `dietary`, `budget_usd`, `cart` items, `total_usd`, `decision`.

Anthropic call (httpx): `POST https://api.anthropic.com/v1/messages`, headers `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`, body `{model, max_tokens: 80, system, messages:[{role:"user",content:...}]}`; text at `content[0].text`.
OpenAI-compatible: `POST {OPENAI_BASE_URL}/chat/completions` with `{model, messages:[system,user], max_tokens: 80}`; text at `choices[0].message.content`.

Templates:

```
empty:       "Cart's empty. Grab anything from a lit bay."
misplaced:   "{name} is in the wrong bay. Please return it to its lit slot."
over_budget: "You're ${over_by} over budget. Putting back the {put_back_name} fixes it."
suggest:     "{cart_item} added. {sku_name} pairs well at ${price} and keeps you under budget."
ok:          "Looking good. ${remaining} left in your budget."
```

### 9.7 Serial bridge (`backend/serial_bridge.py`, F12)

Background thread. Opens the port; on failure retries every 3 s and reports `serial:false` in `/api/health`. Never crash the backend if the ESP32 is missing.

Outgoing (PC → ESP32), newline-terminated ASCII. **Design change:** the build has no bay LEDs and no status
LED; the board is only the gate screen. The backend sends DISP commands only; the firmware still accepts the
old LED, GATE, SHELF and HILITE lines and ignores them harmlessly.

```
DISP,IDLE | DISP,WELCOME,<name> | DISP,TOTAL,<total>,<count> | DISP,PAID,<total>,<auth>
DISP,DECLINED | DISP,OCCUPIED,<name>
DISP,FIND,<cards>                     "Find bay 2 and 4" (cards as printed, "2 and 4"), 6 s, then back to TOTAL
DISP,REFUND,<amount>                  green, animated return arrow, "REFUNDED", amount; held 5 s after the return
                                      closes, then IDLE (the firmware also falls back to IDLE after 5 s)
PING
```

Incoming (ESP32 → PC):

```
READY
PONG
BTN,0              onboard button pressed (maps to admin reset)
W,<bay>,<grams>    load cell reading, F16 only
```

Backend sends: the screen for each store event (WELCOME on enter, TOTAL on cart changes, PAID / DECLINED on
payment, REFUND on a successful refund, IDLE after the session) and FIND when a plan is made. Bays are found by printed number cards 1 to 5
on the shelf front (`scripts/gen_bay_cards.py`).

### 9.8 Passkeys (`backend/auth_passkeys.py`, F7)

Use py_webauthn:

- Registration options: `generate_registration_options(rp_id=RP_ID, rp_name=RP_NAME, user_id=member_id.encode(), user_name=name, user_display_name=name, authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED), exclude_credentials=[existing])`. Store `options.challenge` (base64url) in `request.session`. Return `options_to_json(options)`.
- Registration verify: `verify_registration_response(credential=body, expected_challenge=..., expected_origin=PUBLIC_ORIGIN, expected_rp_id=RP_ID, require_user_verification=True)`. Save credential id, public key, sign count.
- Authentication options: `generate_authentication_options(rp_id=RP_ID, user_verification=REQUIRED)` with empty `allow_credentials` (discoverable, so the phone picks the passkey). Store challenge + purpose in session.
- Authentication verify: find passkey by credential id, `verify_authentication_response(..., credential_public_key=..., credential_current_sign_count=..., require_user_verification=True)`, update sign count, set session member, `verified_at`, `verified_member`.

Frontend (`web/js/passkey.js`): load `@simplewebauthn/browser@13` UMD bundle from jsDelivr, then `SimpleWebAuthnBrowser.startRegistration({ optionsJSON })` and `SimpleWebAuthnBrowser.startAuthentication({ optionsJSON })`. Verify the bundle path against the package's `dist` folder when implementing. Show friendly errors: "Face ID was cancelled", "This phone has no screen lock set up, use the demo account".

**Critical:** `RP_ID` and `PUBLIC_ORIGIN` must match the tunnel domain exactly, and that domain must stay the same all weekend or every passkey breaks. Use an ngrok free static domain or a named Cloudflare tunnel on your own domain. Do not use random quick-tunnel URLs.

### 9.9 Stripe test mode (`backend/payments.py`, F10)

- Refuse to start if `STRIPE_SECRET_KEY` does not start with `sk_test_`.
- On signup: `stripe.Customer.create(name=..., metadata={"member_id":...})`, then `pm = stripe.PaymentMethod.attach("pm_card_visa", customer=customer.id)`; save `customer.id`, `pm.id`, label `Visa •••• 4242 (test)`. No card entry UI needed.
- On approve: `stripe.PaymentIntent.create(amount=cents, currency="usd", customer=..., payment_method=..., payment_method_types=["card"], off_session=True, confirm=True, description="SpeedMart Market #01", metadata={"session_id":..., "instruction_id":...}, idempotency_key=f"SpeedMart-{session_id}-{attempt}")`.
- `status == "succeeded"` → AUTHORIZED, `auth_code` = last 6 chars of the PaymentIntent id uppercased. `stripe.CardError` (older SDKs: `stripe.error.CardError`) → DECLINED with message. Any other exception → fall back to mock provider and mark `provider:"mock"` so the demo continues.
- If `force_decline` is on (admin), return DECLINED without calling Stripe.

Mock provider (F10 off or fallback): AUTHORIZED with `auth_code` `MOCK` + 4 random digits.

### 9.10 Loyalty (F15)

`points_earned = floor(total_usd)`. Add to member. Show on receipt: "+8 points · 23 total".

---

## 10. BUILD STEPS

### How to run a step with Claude Code

Paste this, changing the step ID:

```
Read SpeedMart_BUILD_SPEC.md sections 0 to 9. Implement step S1.1 only.
Follow its Files, Instructions and Acceptance exactly. Respect feature flags.
When finished, run every acceptance check you can, report pass/fail for each,
and list anything a human must verify physically.
```

**Roles:** `HW` = hardware/firmware · `VA` = vision + API · `APP` = app + story. Times are targets, not limits.

---

### PHASE 0: Before hacking starts (accounts and printing only, no project code)

Check HackGT rules: project code should be written during the hacking window. Accounts, printing, and packing are fine beforehand.

#### S0.1 Accounts and keys (human)
1. ngrok account → claim the free static domain → note it (e.g. `SpeedMart-demo.ngrok-free.app`). Install ngrok and run `ngrok config add-authtoken <token>`.
2. Stripe account → Test mode → copy `sk_test_...` secret key.
3. LLM API key (Anthropic or OpenAI-compatible) with a little credit.
4. Roboflow and Google accounts ready if F13 is planned.

**Acceptance:** all keys saved in a password manager, static domain name known.

#### S0.2 Print and pack (human)
1. Print ArUco tags (IDs 0 to 9) at 6 cm, matte paper, mounted on cardstock with a 1 cm white border. Keep spares.
2. Plan for QR codes: they will be generated at the event (S3.4). Either print them at the venue or display them on a tablet / second phone.

---

### PHASE 1: Vertical slice without vision (Fri 8:00 → 11:00 PM)

Goal: an admin button changes the shelf, and a phone on the same network sees the cart update live.

#### S1.1 Scaffold, settings, DB, event log
**Feature:** F1 · **Owner:** VA · **Time:** 45 min · **Depends:** none
**Files:** `requirements.txt`, `.env.example`, `.gitignore`, `config.json`, `catalog.json`, `backend/__init__.py`, `backend/main.py`, `backend/settings.py`, `backend/db.py`, `backend/eventlog.py`, `README.md` (run instructions only)
**Instructions:**
1. Create the structure from Section 4 (empty modules are fine for later steps).
2. `settings.py`: load `.env` (python-dotenv), `config.json`, `catalog.json` into typed dicts/dataclasses; expose `settings.features.<flag>`; validate that every `bays[].sku` and `units[].sku` exists in `skus`, and every `units[].home_bay` exists in `bays`; fail fast with a clear message.
3. `db.py`: sqlite connection per request (`check_same_thread=False`, WAL mode), schema from Section 6, `seed_demo_member()`.
4. `eventlog.py`: `log(event_type: str, **fields)` appends `{"ts":..., "type":..., ...}` to `data/events.log.jsonl`; create `data/` if missing.
5. `main.py`: FastAPI app, `SessionMiddleware(secret_key=SESSION_SECRET, same_site="lax", https_only=False)`, mount `web/` as static at `/` with `html=True` (mount last so API routes win), `GET /api/health`, `GET /api/config/public`, `GET /api/catalog`.
**Acceptance:**
- `uvicorn backend.main:app --host 0.0.0.0 --port 8000` starts with no errors.
- `curl localhost:8000/api/health` returns `ok: true`.
- `data/SpeedMart.db` exists with 4 tables; demo member seeded.
- Breaking `catalog.json` (unknown SKU in a unit) makes startup fail with a readable error.

#### S1.2 Shelf state, store sessions, cart engine (+ tests)
**Feature:** F4, F8 core logic, F17 overrides · **Owner:** VA · **Time:** 1 h · **Depends:** S1.1
**Files:** `backend/shelf_state.py`, `backend/store.py`, `backend/cart.py`, `tests/test_cart.py`, add `pytest` to a `requirements-dev.txt`
**Instructions:**
1. `shelf_state.py`: holds latest per-bay stable contents; `apply_snapshot(snapshot)` updates only stable bays (7.3); `shelf_counts() -> dict[sku,int]`; `misplaced() -> list`; `last_snapshot_age_ms()`. Thread-safe with a lock. Also implements `POST /internal/shelf` (token checked).
2. `store.py`: state machine 7.1, store lock, timeout task, `start_session(member_id)` captures baseline from `shelf_counts()`, `current_session()`, `freeze_cart()`, `unfreeze_cart()`, `close()`, `cancel()`.
3. `cart.py`: `compute_cart(session, shelf_counts, overrides, member) -> CartSnapshot` implementing 7.3 exactly with integer cents.
4. Every state change and cart change calls `eventlog.log`.
5. Tests (use fake snapshots, no camera):
   - pick one unit → qty 1; put back → qty 0
   - put back into wrong bay → qty 0 + misplaced warning
   - unstable bay keeps previous contents (no flicker)
   - items missing before entry are not charged
   - repeated identical snapshots do not change the cart
   - override +1 / −1 clamps to `[0, baseline]`
   - second `start_session` while occupied raises occupied error
   - totals: 1 × $8.00 at 8% tax → $8.64
**Acceptance:** `pytest -q` passes all tests.

#### S1.3 WebSocket, live cart page, admin panel (button mode)
**Feature:** F5, F17 · **Owner:** APP (with VA) · **Time:** 1.5 h · **Depends:** S1.2
**Files:** `backend/ws.py`, `backend/admin.py`, `backend/routes_api.py` (store/current + temporary button routes), `web/store.html`, `web/admin.html`, `web/js/api.js`, `web/js/ws.js`, `web/js/pages/store.js`, `web/js/pages/admin.js`, `web/css/styles.css`
**Instructions:**
1. `ws.py`: connection manager; each socket tagged with member id or `admin`; `broadcast_cart(session_id, snapshot)`, `broadcast_admin(msg)`. Recompute + broadcast on every shelf change, override, and state change.
2. Admin routes from 8.3 (login, state, reset, force-exit, override, demo-login, led stub, force-decline).
3. Temporary routes used when `gates` is false: `POST /api/dev/start` (start session for logged-in member) and `POST /api/dev/checkout` (quote). Keep them permanently as the gates-off fallback, but hide their buttons when `gates` is true.
4. `admin.html`: login, live state JSON panel, per-SKU "+1 to cart" / "−1 from cart" override buttons (8.3 `delta`), reset, demo-login, force-decline toggle, event log tail (last 50), vision age badge (red if > 2000 ms), serial badge.
5. `store.html`: see 11.3. Renders only from snapshots.
**Acceptance:**
- Laptop and phone on the same WiFi (or laptop hotspot). Phone opens `http://<laptop-ip>:8000/admin.html`, logs in, taps demo-login, starts a session, opens `store.html`.
- Clicking an override on the laptop admin page updates the phone cart in under 300 ms.
- Killing and restarting uvicorn: phone reconnects automatically and shows the same cart.

#### S1.4 ESP32 firmware + serial bridge
**Feature:** F12 · **Owner:** HW · **Time:** 1.5 h · **Depends:** S1.1
**Files:** `firmware/shelf_esp32/platformio.ini`, `firmware/shelf_esp32/src/main.cpp`, `backend/serial_bridge.py`
**Instructions:**
1. Firmware per Section 12. Verify pins for the actual board (classic ESP32: avoid GPIO 6 to 11 and 12; ESP32-S3: GPIO 4 is fine).
2. Serial bridge per 9.7: background thread, auto-reconnect, command queue, never crashes backend when unplugged. Hook bay LEDs to cart/shelf changes and `/admin/led`.
3. Set `serial.port` in `config.json` (macOS: `/dev/cu.usbserial-*` or `/dev/cu.SLAB_USBtoUART`; Linux: `/dev/ttyUSB0`; Windows: `COM3`).
**Acceptance:**
- `pio run -t upload` succeeds; serial monitor shows `READY`; typing `PING` returns `PONG`.
- `/admin/led` with `LED,0,OFF` turns bay 0 LED off.
- Unplugging the ESP32 flips `/api/health` `serial` to false without errors; replugging recovers within 5 s.
- Posting a fake snapshot to `/internal/shelf` (curl, with `X-Internal-Token`) where bay 0 has `"units": []` and `stable: true` turns bay 0's LED off; restoring the units turns it back on.

---

### PHASE 2: Vision (Fri 11:00 PM → 2:00 AM)

#### S2.1 Camera + calibration
**Feature:** F2 · **Owner:** VA + HW (mount) · **Time:** 45 min · **Depends:** S1.1
**Files:** `vision/camera.py`, `vision/calibrate.py`
**Instructions:**
1. `camera.py`: `open_camera(cfg)` sets width/height/fps; if `auto_exposure` false, set manual exposure (on V4L2 `CAP_PROP_AUTO_EXPOSURE=1` means manual; on macOS AVFoundation some props are ignored, so log what actually applied). Print a warning if settings didn't take. Provide `read()` returning the latest frame.
2. `calibrate.py`: show live frame; user drags a rectangle per bay in order 0,1,2…; `r` redo, `s` save into `config.json` `bays[].roi`, `q` quit. Keep all other config keys unchanged.
3. HW: mount camera 50 to 80 cm above the shelf, lens pointing straight down, tape tripod feet position.
**Acceptance:** calibrate saves ROIs; reopening shows the saved boxes aligned to the bays.

#### S2.2 ArUco detection, snapshot posting, overlay
**Feature:** F2 · **Owner:** VA · **Time:** 1.5 h · **Depends:** S2.1, S1.2
**Files:** `vision/aruco_detect.py`, `vision/overlay.py`, `vision/worker.py`, `scripts/run_all.sh`
**Instructions:** implement 9.1 and 9.5 (motion and stability can be stubbed as always-stable in this step). Snapshot poster runs in a background thread with a size-1 queue; the camera loop never waits on HTTP. `run_all.sh` starts uvicorn and the worker, and kills both on Ctrl+C.
**Acceptance:**
- Overlay shows boxes and tag IDs at ≥ 15 FPS.
- `/api/health` `vision_age_ms` stays under 500.
- Removing a tagged unit from bay 0 updates the phone cart within ~1 s.

#### S2.3 Motion freeze + stability
**Feature:** F3 · **Owner:** VA · **Time:** 1 h · **Depends:** S2.2
**Files:** `vision/motion.py`, `vision/worker.py`
**Instructions:** implement 9.2 and 9.3; overlay turns a bay yellow while unstable.
**Acceptance (physical):**
- Hover a hand over bay 0 covering the tag for 3 s without lifting the item: cart does not change.
- Lift the item and hold it above the shelf: it enters the cart once, after the hand leaves.
- 10 picks and 10 put-backs on bay 0: 10/10 correct, no flicker. Repeat for all bays.

---

### PHASE 3: Identity and gates (Sat 9:00 AM → 1:00 PM)

#### S3.1 HTTPS tunnel
**Feature:** F14 · **Owner:** VA · **Time:** 30 min · **Depends:** S1.1
**Instructions:**
1. `ngrok http --url=<static-domain> 8000` (older ngrok versions use `--domain=`). Add the command to `run_all.sh` behind `features.https_tunnel`.
2. Set `PUBLIC_ORIGIN` and `RP_ID` in `.env` to that domain.
3. Set `SessionMiddleware(https_only=True)` when the tunnel is on.
4. Add `--proxy-headers --forwarded-allow-ips="*"` to uvicorn so FastAPI sees https.
**Acceptance:** phone on cellular data opens `https://<domain>/api/health` successfully.

#### S3.2 Signup + members + demo account
**Feature:** F6 · **Owner:** APP · **Time:** 1 h · **Depends:** S1.3
**Files:** `backend/members.py`, `web/index.html`, `web/js/pages/index.js`
**Instructions:** routes `signup`, `me`, `logout` (8.1). Budget slider $10 to $50 default $20, optional dietary select. If `stripe` on, call the payments helper from S4.2 when available (guard so signup works before S4.2 exists). If `passkeys` off, signup logs in and shows "Walk to the entry gate".
**Acceptance:** signing up on the phone creates a member row; `/api/me` returns it; reload keeps login.

#### S3.3 Passkeys (Face ID / fingerprint)
**Feature:** F7 · **Owner:** VA · **Time:** 2.5 h · **Depends:** S3.1, S3.2
**Files:** `backend/auth_passkeys.py`, `web/js/passkey.js`, updates to `index.js`
**Instructions:** implement 9.8 and 7.2. After signup, immediately run registration. Add "Already a member? Sign in with Face ID" on `index.html`. Every WebAuthn call must be triggered by a button tap (Safari requires a user gesture).
**Acceptance:**
- iPhone (Safari) and one Android phone (Chrome): signup → Face ID/fingerprint prompt → passkey saved.
- Log out, tap "Sign in with Face ID" → logged back in as the same member.
- Cancelling the prompt shows a friendly message and a retry button.

#### S3.4 Entry and exit gates + store lock + QR codes
**Feature:** F8 · **Owner:** APP + VA · **Time:** 1.5 h · **Depends:** S3.3 (or S3.2 if passkeys off)
**Files:** gate routes in `routes_api.py`, `web/enter.html`, `web/exit.html` (quote only for now), `web/js/pages/enter.js`, `scripts/gen_qr.py`
**Instructions:**
1. `enter.html?g=<token>`: button "Enter with Face ID" → passkey login (purpose `enter`) → `POST /api/gate/enter` → redirect to `store.html`. If not a member → link to signup. If occupied → "Someone is shopping right now. Try again in a minute." Send `GATE,OPEN` on success.
2. `exit.html?g=<token>`: calls `quote`, shows receipt preview. Approve comes in S4.1.
3. `gen_qr.py`: reads `PUBLIC_ORIGIN` and gate tokens; outputs `qr/1_join.png`, `qr/2_enter.png`, `qr/3_exit.png` and `qr/all.pdf` with huge labels "1 · JOIN", "2 · ENTER", "3 · EXIT".
**Acceptance:**
- Full path on a phone: scan 1 → signup + Face ID → scan 2 → Face ID → store page live → scan 3 → receipt preview.
- A second phone scanning 2 while occupied sees the occupied message.
- Admin reset frees the store.

---

### PHASE 4: Checkout and AI (Sat 1:00 → 4:00 PM)

#### S4.1 Instruction, mock payment, approve, receipt, loyalty
**Feature:** F9, F15 · **Owner:** APP · **Time:** 1.5 h · **Depends:** S3.4 (or S1.3 with gates off)
**Files:** `backend/payments.py`, approve/cancel/receipt routes, `web/exit.html`, `web/receipt.html`, their JS
**Instructions:** implement 8.6, 8.7, 9.9 mock path, 9.10. Exit page shows items, total, a collapsible "What the payment network sees" panel with the instruction JSON (agent token scope, user intent), and button "Approve $X.XX with Face ID". Enforce `over_scope`. Empty cart → close with "Nothing to pay". On AUTHORIZED: `SHELF,GREEN`, redirect to receipt.
**Acceptance:** full loop charges (mock) the correct total; `data/payments.log.jsonl` has the entry; receipt shows auth code and points; force-decline shows the declined state and allows retry.

#### S4.2 Stripe test mode
**Feature:** F10 · **Owner:** VA · **Time:** 1 h · **Depends:** S4.1
**Instructions:** implement 9.9 Stripe path; attach test Visa at signup; backfill demo member.
**Acceptance:** after approve, the PaymentIntent appears as succeeded in the Stripe test dashboard with matching amount and metadata; unplugging the network during approve falls back to mock without crashing.

#### S4.3 AI store agent
**Feature:** F11 · **Owner:** APP · **Time:** 1 h · **Depends:** S1.2
**Files:** `backend/agent.py`, `tests/test_agent.py`
**Instructions:** implement 7.4 and 9.6. Tests cover policy decisions only (no network).
**Acceptance:** picking electrolytes shows a suggestion for the recovery drink within ~2 s; going over budget shows a put-back line; with the API key removed, templates still appear.

**4:00 PM FREEZE.** After this, only fixes, reliability, optional F13, and demo prep. Apply the cut order from Section 2 if anything above is not green.

---

### PHASE 5: Optional upgrades (only if the full loop is green)

#### S5.1 YOLO data + training
**Feature:** F13 · **Owner:** VA · **Time:** 2.5 h (mostly waiting) · **Depends:** S2.3
Follow Section 14. **Acceptance:** `best.pt` validation mAP50 ≥ 0.9 on all classes; saved as `models/SpeedMart_yolo.pt`.

#### S5.2 YOLO inference + fusion
**Feature:** F13 · **Owner:** VA · **Time:** 1.5 h · **Depends:** S5.1
**Files:** `vision/yolo_detect.py`, `vision/fusion.py`, worker + backend changes
**Instructions:** implement 9.4. Run YOLO every `yolo_every_n_frames` frames on the full frame at `imgsz=640`; use `device="mps"` on Apple Silicon, `0` on NVIDIA, else CPU. Cache last result between runs.
**Acceptance:** with tags covered by tape, picks still register 9/10 or better; with YOLO flag off, behavior is identical to before.

#### S5.3 Load cells (default off)
**Feature:** F16 · **Owner:** HW · **Time:** 3 h · **Depends:** S1.4
**Instructions:** one load cell + HX711 per bay; firmware sends `W,<bay>,<grams>` at 10 Hz; backend converts grams to unit count using per-SKU unit weight (add `weight_g` to catalog); fused count = tag/YOLO count when bay stable, weight count as a tiebreaker when they disagree.
**Acceptance:** covering all tags with a hand does not change the cart; weight alone detects a removal within 1 s.

---

### PHASE 6: Hardening and demo (Sat 4:00 PM → Sun 8:00 AM)

#### S6.1 Failure drills
Run each and fix before moving on: lights dimmed; judge leaning over shelf; tag with glare; camera bumped (recalibrate in under 60 s); ESP32 unplugged; WiFi down (laptop hotspot); LLM key removed; Stripe key removed; backend restarted mid-session; two phones scanning entry.

#### S6.2 Demo polish
Large fonts, one-handed layout, loading states, clear error toasts, `scripts/reset_demo.sh`, "Demo Shopper" quick path from admin, overlay window positioned for judges.

#### S6.3 Docs and video
`README.md` (setup + run), `docs/DEMO.md` (Section 15), `docs/DEVPOST.md` (Section 18). Film the 90-second demo twice Saturday night; export 1080p; keep a local copy.

---

## 11. FRONTEND SCREENS (`web/`)

Global rules: mobile first (design for 390 px wide), no framework, one stylesheet, CSS variables for colors, minimum 48 px touch targets, 17 px base font. Every page reads `/api/config/public` and hides UI for disabled features. Do not use the Visa logo image; text like "Visa (sandbox)" is fine. Every page shows a small "Sandbox demo" footer.

### 11.1 `index.html` (QR 1 · JOIN)
- Title "SpeedMart Market", subtitle "Grab and go, with a yes you control."
- Form: first name, budget slider ($10 to $50, default $20), dietary select (none / vegetarian / vegan / gluten free).
- Button "Join with Face ID" (or "Join" if passkeys off).
- Success: "You're a member. Card linked: Visa •••• 4242 (test). Walk to the entry gate."
- Link "Already a member? Sign in with Face ID".

### 11.2 `enter.html?g=…` (QR 2 · ENTER)
- Big door icon, "Entry gate".
- Button "Enter with Face ID".
- States: not a member (link to join), verifying, welcome ("Welcome in, Maya"), occupied, error.

### 11.3 `store.html` (live cart)
- Header: "SpeedMart Market" + session last 4 chars + live dot (green = socket connected).
- Agent card at top: the one-sentence agent line, subtle fade on change.
- Cart rows: name, qty, line total; new rows slide in; removed rows fade out.
- Totals block: subtotal, tax, total; budget bar (green under 80%, amber 80 to 100%, red over).
- Warnings strip for misplaced items.
- Footer hint: "When you're done, scan the EXIT code." (gates on) or button "Checkout" (gates off).
- Permissions card (8.10), collapsed, tap to expand.

### 11.4 `exit.html?g=…` (QR 3 · EXIT)
- Itemized receipt preview and total.
- Collapsible "What the payment network sees": agent, token scope (merchant, max amount, single use, expiry), user intent sentence, label that it is a sandbox mock.
- Permissions card (8.10), expanded.
- Button "Approve $X.XX with Face ID". Secondary "Keep shopping" (cancel).
- Over scope: "This is over your $20 limit. Put something back to continue."
- Declined: red state + retry.

### 11.5 `receipt.html`
- Big green check, "Paid $8.64", card label, auth code, time, items, "+8 points · 23 total".
- Measured results: "In and out in 42 seconds", "1 tap to pay".
- Refund lines: "Refunded $8.64 for 1 Electrolyte tabs" with the refund id.
- "Changed your mind?" card with "Return an item" (only within 30 minutes of a Stripe or mock payment). Tap →
  Face ID → return panel: "Place the item back on its bay", detected returns listed live, "not from this
  purchase" rows, refund totals, time left, "Confirm refund $X" and "Cancel return". On success the agent line
  "Refund of $X is on its way to your Visa ending 4242." Store occupied → "Someone is shopping right now. Try
  again in a minute."
- Toggle "Show payment record" → payment JSON.
- Button "Done" (clears to index).

### 11.6 `admin.html` (team only)
- Password gate. Panels: store status and lock, results today (sessions, average time in store, average exit scan to approval, refunds), current session and baseline (a return in progress shows its detected items and refund), live shelf per bay (stable / motion), cart, overrides (+/− per SKU), reset, force-exit, demo-login, force-decline toggle, LED test buttons, health badges (vision age, serial, Stripe mode, LLM on/off), event log tail.

---

## 12. FIRMWARE (`firmware/shelf_esp32/`)

### 12.1 Wiring

| Part | Classic ESP32 (WROOM DevKit) | ESP32-S3 | Notes |
|---|---|---|---|
| WS2812 strip data | GPIO 25 | GPIO 4 | 330 Ω series resistor on data; 4 LEDs: bays 0,1,2 + gate |
| WS2812 5V / GND | 5V (VIN) / GND | 5V / GND | 4 LEDs are fine on USB power |
| Onboard button | GPIO 0 | GPIO 0 | Long press 1 s → `BTN,0` |
| HX711 DT/SCK (F16) | 32/33, 26/27, 13/14 | 5/6, 7/15, 16/17 | One HX711 per bay |

Do not use GPIO 6 to 11 (flash) or 12 (strapping) on a classic ESP32.

### 12.2 `platformio.ini`

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev          ; use esp32-s3-devkitc-1 for S3
framework = arduino
monitor_speed = 115200
lib_deps =
  adafruit/Adafruit NeoPixel@^1.12.0
```

### 12.3 `src/main.cpp`

```cpp
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

#define LED_PIN   25      // 4 on ESP32-S3
#define NUM_BAYS  3
#define GATE_IDX  3
#define NUM_LEDS  4
#define BTN_PIN   0

Adafruit_NeoPixel strip(NUM_LEDS, LED_PIN, NEO_GRB + NEO_KHZ800);

bool bayOn[NUM_BAYS] = {true, true, true};
enum ShelfMode { SHELF_IDLE, SHELF_GREEN, SHELF_RED };
enum GateMode  { GATE_IDLE, GATE_OPEN, GATE_CLOSED };
ShelfMode shelfMode = SHELF_IDLE;
GateMode gateMode = GATE_IDLE;
String line;
unsigned long btnDownAt = 0;
bool btnSent = false;

void handleCommand(const String& cmd) {
  if (cmd == "PING") { Serial.println("PONG"); return; }
  if (cmd.startsWith("LED,")) {
    int bay = cmd.substring(4, cmd.indexOf(',', 4)).toInt();
    bool on = cmd.endsWith(",ON");
    if (bay >= 0 && bay < NUM_BAYS) bayOn[bay] = on;
    return;
  }
  if (cmd == "SHELF,GREEN") shelfMode = SHELF_GREEN;
  else if (cmd == "SHELF,RED") shelfMode = SHELF_RED;
  else if (cmd == "SHELF,IDLE") shelfMode = SHELF_IDLE;
  else if (cmd == "GATE,OPEN") gateMode = GATE_OPEN;
  else if (cmd == "GATE,CLOSED") gateMode = GATE_CLOSED;
  else if (cmd == "GATE,IDLE") gateMode = GATE_IDLE;
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { line.trim(); if (line.length()) handleCommand(line); line = ""; }
    else if (line.length() < 64) line += c;
  }
}

void pollButton() {
  bool down = digitalRead(BTN_PIN) == LOW;
  if (down && btnDownAt == 0) { btnDownAt = millis(); btnSent = false; }
  if (!down) btnDownAt = 0;
  if (down && !btnSent && millis() - btnDownAt > 1000) { Serial.println("BTN,0"); btnSent = true; }
}

void render() {
  float breathe = (sin(millis() / 600.0) + 1.0) / 2.0;       // 0..1
  for (int i = 0; i < NUM_BAYS; i++) {
    uint32_t c;
    if (shelfMode == SHELF_GREEN)      c = strip.Color(0, 180, 40);
    else if (shelfMode == SHELF_RED)   c = strip.Color(200, 0, 0);
    else if (bayOn[i])                 c = strip.Color(120, 120, 120);
    else                               c = strip.Color(0, 0, 0);
    strip.setPixelColor(i, c);
  }
  uint8_t w = 20 + (uint8_t)(40 * breathe);
  uint32_t g = gateMode == GATE_OPEN   ? strip.Color(0, 200, 40)
             : gateMode == GATE_CLOSED ? strip.Color(200, 0, 0)
             : strip.Color(w, w, w);
  strip.setPixelColor(GATE_IDX, g);
  strip.show();
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  strip.begin();
  strip.setBrightness(80);
  strip.show();
  Serial.println("READY");
}

void loop() {
  pollSerial();
  pollButton();
  render();
  delay(10);
}
```

The MCU is a dumb display and sensor hat. No cart logic on the ESP32. Timed effects (green for 3 s) are controlled by the backend sending `SHELF,IDLE` afterward.

---

## 13. PRINTABLE ASSETS (`scripts/`)

### 13.1 `gen_aruco.py`
- Uses OpenCV + Pillow. Dictionary `DICT_4X4_50`, IDs from `catalog.json.units`.
- 300 DPI US Letter pages (2550 × 3300 px), 6 tags per page (2 × 3).
- Black marker exactly 6.0 cm (709 px) with a 1 cm white border; label under each: `ID 0 · Electrolyte tabs`.
- Save `tags/aruco_tags.pdf` with `resolution=300`. Print at 100% scale ("fit to page" OFF) and verify with a ruler.

### 13.2 `gen_qr.py`
- Reads `PUBLIC_ORIGIN`, `ENTRY_GATE_TOKEN`, `EXIT_GATE_TOKEN`.
- QR 1 → `{origin}/`, QR 2 → `{origin}/enter.html?g={entry}`, QR 3 → `{origin}/exit.html?g={exit}`.
- Error correction M, box size 20, border 4; label below in big bold text.
- Save individual PNGs and `qr/all.pdf`.

---

## 14. YOLO TRAINING GUIDE (F13, `training/TRAINING.md`)

1. **Capture** (`training/capture.py`): opens the overhead camera with the same config; `space` toggles auto-save every 0.5 s to `training/raw/`; `s` saves a single frame. Capture 250 to 400 frames covering: each product alone and together, different positions in bays, partly covered by a hand, held above the shelf, dim and bright light, with and without tags visible. Recapture ~50 frames at the venue lighting.
2. **Label** in Roboflow: new Object Detection project, class names exactly matching `catalog.json` `yolo_class` values. Draw boxes on every visible product. Split 80/10/10. Augment: brightness ±25%, exposure ±15%, blur up to 1 px, rotation ±10°. Export format "YOLOv8" (works with YOLO11). Download zip.
3. **Train** in Colab (GPU runtime):
   ```
   !pip install ultralytics
   !yolo detect train model=yolo11n.pt data=/content/dataset/data.yaml epochs=80 imgsz=640 batch=16 patience=20
   ```
4. **Check** `runs/detect/train/results.png` and val mAP50 (target ≥ 0.9). Download `runs/detect/train/weights/best.pt` → `models/SpeedMart_yolo.pt`.
5. **Inference** (`vision/yolo_detect.py`): `model = YOLO(path)`; `results = model.predict(frame, imgsz=640, conf=cfg.yolo_conf, verbose=False, device=...)`; map class names → SKU via catalog; assign each box to a bay by its center.

---

## 15. DEMO SCRIPT (`docs/DEMO.md`, 90 to 120 s)

Before each judge: admin reset, shelf full, LEDs on, overlay visible, phone for "Demo Shopper" ready in case the judge's phone fails.

1. **Hook (10 s):** "Stores already watch shelves with cameras. SpeedMart lets that camera build your cart, but only you can say yes to the charge."
2. **Join (20 s):** judge scans QR 1, types a name, Face ID. "That's a passkey. Your face never leaves your phone; your phone vouches for you."
3. **Enter (10 s):** scan QR 2, Face ID, gate LED turns green. "You're in. One shopper at a time, no one else is tracked."
4. **Pick (15 s):** judge grabs electrolytes. Point at the overlay, then the phone: row appears, bay LED goes dark, agent line suggests the recovery drink.
5. **Put back (10 s):** put it back, row disappears. "It reads the shelf, not a script."
6. **Real cart (10 s):** grab electrolytes and a protein bar. Show budget bar.
7. **Exit (15 s):** scan QR 3. Open "What the payment network sees": scoped single-use agent token, intent sentence. Face ID to approve. Shelf flashes green. Receipt with auth code and points.
8. **Close (10 s):** "The shelf agent assembled the cart. The shopper authorized it. Agentic commerce you can hold."

If vision misbehaves: a teammate uses the admin override, and if a judge asks, say plainly that it's the manual fallback.

### Judge Q&A

| Question | Answer |
|---|---|
| Do you store faces? | No. Face ID happens on the phone through a passkey (WebAuthn). We store only a public key. |
| Is this real Visa? | Payments run through Stripe test mode with a test Visa card. The authorization object is modeled on Visa Intelligent Commerce concepts: agent token scoped to merchant, amount and time, a user intent, and cardholder confirmation. It's labeled sandbox. |
| Why not Amazon Go? | Full-store person-to-item association is a research-scale problem. We scoped to shelf-level state and a one-shopper store so the trust and payment story is solid. |
| What if someone pockets an item? | It left the shelf, so it's in their cart and gets charged at exit. |
| Two shoppers? | Store lock: one shopper inside at a time. Multi-shopper would need per-person association, which is future work. |
| What does the AI actually do? | A deterministic policy decides (budget, complements, misplaced items) and the LLM phrases it for the shopper. If the LLM is down, templates keep it working. |
| How accurate is detection? | Tags give identity; motion freeze prevents hand flicker; (if on) YOLO covers occluded tags. Show the 10/10 test. |

---

## 16. FAILURE MODES

| Failure | Detect | Fix live |
|---|---|---|
| Camera bumped | Boxes misaligned in overlay | Run `calibrate.py` (under 60 s) |
| Glare on tag | Tag flickers in overlay | Tilt item or swap to spare matte tag |
| Judge blocks camera | Bays yellow | Ask them to step back 20 cm; state freezes, so nothing breaks |
| Vision worker crash | Admin vision badge red | `run_all.sh` restarts it; cart recomputes from baseline |
| Backend restart | Phone socket reconnects | Nothing; state is in SQLite + recomputed |
| Venue WiFi dies | Phone can't load | Tunnel works on cellular; else laptop hotspot + LAN http with passkeys off |
| Tunnel down | `https://domain` fails | Restart ngrok; same static domain keeps passkeys valid |
| Judge phone has no passkey support | Error on join | Use Demo Shopper on team phone |
| LLM slow or down | Template lines appear | Nothing needed |
| Stripe error | Payment provider shows mock | Nothing needed; say so if asked |
| ESP32 unplugged | Serial badge red | Replug; auto reconnect |

---

## 17. REMOVAL GUIDE (turning features off)

| Turn off | Set flag | What changes | Files you can skip |
|---|---|---|---|
| F3 motion freeze | `motion_freeze: false` | Bays count as stable after `stable_ms` regardless of motion; more flicker risk | `vision/motion.py` |
| F6 signup | `signup: false` | Everyone uses the Demo Shopper; QR 1 skipped | `members.py` signup route, `index.html` form |
| F7 passkeys | `passkeys: false` | "Confirm" buttons replace Face ID; HTTPS no longer required | `auth_passkeys.py`, `passkey.js` |
| F8 gates | `gates: false` | "Start shopping" and "Checkout" buttons in `store.html`; no QR 2/3 | `enter.html`, gate routes |
| F10 Stripe | `stripe: false` | Mock provider only | Stripe code in `payments.py` |
| F11 LLM | `llm: false` | Templates only | LLM part of `agent.py` |
| F12 LEDs | `hardware_leds: false` | No serial; overlay + phone only | `serial_bridge.py`, `firmware/` |
| F13 YOLO | `yolo: false` | Tags only | `yolo_detect.py`, `fusion.py`, `training/` |
| F14 tunnel | `https_tunnel: false` | LAN http only (requires F7 off) | ngrok setup |
| F15 loyalty | `loyalty: false` | No points on receipt | points code |
| F16 load cells | `load_cells: false` | Default | HX711 code |

---

## 18. DEVPOST DRAFT (`docs/DEVPOST.md`)

**Name:** SpeedMart
**Tagline:** The shelf builds your cart. You approve the charge.

**Inspiration:** Agentic commerce is arriving in chat apps, and stores already have cameras. Nobody connected "I picked this up" to "I approve this charge" in a way shoppers can trust. Silent walk-out checkout is a liability; confirmation is the product.

**What it does:** Shoppers join once with a passkey (Face ID). At the entry gate they verify and the store opens. An overhead camera watches a three-bay shelf; items leaving the shelf appear in the phone cart in about a second, and put-backs disappear. A store agent explains the cart and suggests complements within the shopper's budget. At the exit gate the shopper reviews the total and approves with Face ID against a scoped, single-use agent token, and the shelf lights green.

**How we built it:** OpenCV ArUco detection with per-bay motion freeze and a state-based cart (cart = baseline minus shelf), optional YOLO11 fusion, FastAPI + SQLite + WebSockets, WebAuthn passkeys, Stripe test mode, an authorization object modeled on Visa Intelligent Commerce, an LLM phrasing a deterministic policy, and an ESP32 driving shelf and gate LEDs.

**Challenges:** Hands covering tags, venue lighting, stable passkey domains, keeping scope honest.

**Accomplishments:** Reliable pick/put-back, sub-second phone updates, biometric entry and approval, real sandbox transactions.

**What we learned:** Computing state beats counting events. Human confirmation is what makes grab-and-go trustworthy.

**What's next:** Load-cell fusion, multi-shopper association, real issuer sandbox with Visa Intelligent Commerce APIs.

---

## 19. TIMELINE SUMMARY

| When | Steps | Must be true after |
|---|---|---|
| Fri 8–11 PM | S1.1 → S1.4 | Admin override updates phone cart live; LEDs respond |
| Fri 11 PM–2 AM | S2.1 → S2.3 | Real picks update the cart 10/10 per bay |
| Sat 9 AM–1 PM | S3.1 → S3.4 | Join → enter → shop → exit preview works on a phone |
| Sat 1–4 PM | S4.1 → S4.3 | Full loop with approval, receipt, agent line |
| Sat 4 PM | **Freeze** | Apply cut order if needed |
| Sat 4–9 PM | S5.x (optional), S6.1 | Drills pass |
| Sat 9 PM–12 AM | S6.2, S6.3 | Video filmed, Devpost drafted |
| Sun 6–8 AM | Recalibrate in expo light, final reset | No new features after 6:30 AM |

---

## 20. DEFINITION OF DONE

1. A stranger joins, enters, picks two items, and sees them on their phone without anyone touching a keyboard.
2. Putting one back removes it.
3. The exit approval charges the correct total (Stripe test or mock), lights the shelf green, and shows an auth code.
4. The system survives: a hand over a bay, a backend restart, and an unplugged ESP32.
5. A 2-minute video exists on disk and on Devpost.
6. Repo is public, `.env` is not committed.
7. Everyone on the team can give the close line and answer the Q&A table.
