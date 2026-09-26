# Overnight: entrance kiosk, e2e simulation, Windows reset

Branch `claude/entrance-kiosk-dell-venue-6f3926`. Unattended run, no human available, so every open
question below was decided rather than asked. Nothing outside the file list was touched.

Files added (all new, nothing existing edited):

| File | What it is |
|---|---|
| `backend/kiosk.py` | `APIRouter` serving `GET /api/kiosk/qr/{join\|enter\|exit}` as PNG |
| `web/kiosk.html` | Full-screen kiosk page for the Dell Venue 10" in landscape |
| `web/js/pages/kiosk.js` | Kiosk page script: QR cards, store status, clock |
| `scripts/e2e_sim.py` | End-to-end run of a live backend with no camera and no phone |
| `scripts/reset_demo.ps1` | Windows twin of `reset_demo.sh` |
| `tests/test_kiosk.py` | 23 tests, mounts the router on its own FastAPI app |
| `docs/overnight/kioskqa.md` | This file |

---

## Integration

**`backend/kiosk.py` is not wired into the app yet, because `backend/main.py` is not mine to edit.**
Until someone adds these two lines the page loads but every QR is a broken image (the page degrades to
"QR code unavailable. Use the printed JOIN code.", so nothing else breaks).

Lines another file needs, exact and complete:

1. `backend/main.py`, the import line near the top — add `kiosk` to the existing backend import:

   ```python
   from backend import admin, db, eventlog, kiosk, routes_api, serial_bridge, shelf_state, store, ws
   ```

2. `backend/main.py`, with the other `include_router` calls (**above** `app.mount("/", StaticFiles(...))`
   at the bottom of the file — the static mount matches every path, so a router added after it never
   receives a request):

   ```python
   app.include_router(kiosk.router)
   ```

Nothing else. No new dependency (`qrcode` is already in `requirements.txt` and installed in `.venv`), no
config key, no `.env` key, no change to Section 8, no change to any existing route, template or stylesheet.

Optional, not done by me because both files are owned elsewhere:

- `scripts/run_all.ps1` / `run_all.sh` could open `http://127.0.0.1:8000/kiosk.html` on the tablet.
- `docs/DEMO.md` could mention the kiosk instead of the printed QR sheet.
- `README.md` could list `scripts\e2e_sim.py` and `scripts\reset_demo.ps1`.

---

## Morning checklist

Windows commands, run from the repo root with `.venv` active
(`D:\SpeedMart\.venv\Scripts\Activate.ps1`).

1. **Wire the router in.** Apply the two lines under *Integration* to `backend/main.py`.

2. **Start the backend.**

   ```
   python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
   ```

3. **Check the QR endpoint.**

   ```
   curl.exe -s -o NUL -w "%{http_code} %{content_type}\n" http://127.0.0.1:8000/api/kiosk/qr/join
   ```

   Expect `200 image/png`. Repeat with `enter`, `exit`, and with `nope` (expect `404`).

4. **Open the kiosk on the Dell Venue**, landscape, in Edge or Chrome kiosk mode:

   ```
   start msedge --kiosk "http://<laptop-ip>:8000/kiosk.html" --edge-kiosk-type=fullscreen
   ```

   Entrance side: `http://<laptop-ip>:8000/kiosk.html`. Exit side: add `?side=exit`.

5. **Run the end-to-end simulation** (backend running, no camera needed):

   ```
   python scripts\e2e_sim.py
   ```

   Every line should be `PASS` except `approve payment`, which is `SKIP` while `features.passkeys` is
   `true`. Exit code 0.

6. **Run the reset script** between judges:

   ```
   powershell -ExecutionPolicy Bypass -File scripts\reset_demo.ps1
   ```

7. **Run the tests.**

   ```
   python -m pytest -q
   ```

### Needs a human, physically

- **Scan each code with a real phone.** I decoded all three PNGs with OpenCV and they match the URLs
  exactly, but only a phone camera proves the on-screen size, brightness and glare are good enough.
  Stand at the distance a judge will stand at.
- **Tablet brightness and sleep.** Set the Venue to never sleep and brightness high (Settings → System →
  Power & battery → Screen and sleep → Never on both). A dimmed screen kills QR scanning.
- **Landscape lock.** Rotate the Venue to landscape and lock rotation, or the layout will fight the
  tablet's auto-rotate.
- **Read the status banner from 2 m.** "Open" / "Someone is shopping" is set in `clamp(22px, 3.4vh, 42px)`,
  which is ~42 px tall on the Venue's 1280x800 panel. Confirm it reads from where the judges queue.
- **The NFC hint is a hint only.** "Tap your phone here" is printed text with an animated glyph; there is
  no NFC hardware or Web NFC code anywhere in this change. If you want it to do something, put an NFC tag
  behind the tablet bezel programmed with the same URL the ENTER QR carries. Decide before the demo
  whether to keep the text, because right now tapping does nothing.
- **Exit-side tablet.** If only one tablet exists, the entrance page is the one to run; the exit variant
  assumes a second screen at the exit gate.

---

## Decisions I made

**`web/css/app.css` does not exist.** The brief said to link it "if present". The repo's one stylesheet is
`web/css/styles.css` (Section 4 and Section 11), so `kiosk.html` links that and takes its `--brand`, `--ok`
and `--warn` tokens from it. Everything else is in a `<style>` block inside `kiosk.html`, so the kiosk skin
cannot shift the phone pages. If `app.css` ever appears, change the one `<link>`.

**Store status comes only from the `store_status` WebSocket message.** That message is `{"occupied": bool}`
and nothing else (8.4), and `backend/ws.py` sends it to every socket including one with no cookie. The page
never calls `/api/me`, never reads a name from any message, and never renders `occupant_first_name`. Three
states: `Open` (green), `Someone is shopping` (amber), `Reconnecting…` (grey, while the socket is down).
The QR codes stay on screen through a disconnect — a dropped socket must not stop a judge scanning.

**The kiosk reuses `web/js/ws.js`**, so reconnection is the spec's 0.5 s → 4 s backoff (8.4) with no new
code. Its resync calls `GET /api/store/current`, which answers `{"session": null}` for this cookie-less
page; harmless, and the socket's own first message carries the status.

**Gate tokens are never returned as text.** They exist in the response only as QR pixels. There is no route
that echoes a URL, and the 404 body deliberately does not repeat the name that was asked for, so the route
cannot reflect caller input. `tests/test_kiosk.py` asserts this.

**PNGs are cached by URL, not by name.** The cache key is the built URL, so if `PUBLIC_ORIGIN` changes the
new code is generated rather than served stale. The page also appends `?v=<page load time>` to each image,
so a kiosk left running for hours picks up a new code after a backend restart and a reload.

**`box_size=12`, `border=4`, error correction M** — the same settings `scripts/gen_qr.py` is specified to
use (13.2), so a scanned screen behaves like a scanned print. The join code renders 492x492, the gate codes
540x540; the page scales them up with `image-rendering: pixelated` so module edges stay hard.

**`e2e_sim.py` computes its own totals.** Prices come from `catalog.json` and the rate from
`config.json.store.tax_rate`, in integer cents with `ROUND_HALF_UP`, exactly as `backend/cart.py` does. It
asserts the backend's numbers against those, so a pricing bug in the backend cannot make the check pass.

**`e2e_sim.py` uses `/api/dev/start` and `/api/dev/checkout`.** `features.gates` is `false` in the current
`config.json`, so those are the live routes (S1.3), and admins may call them either way. If gates are
turned back on, the script switches to `POST /api/gate/exit/quote` with `EXIT_GATE_TOKEN` from `.env`
automatically.

**The approve step is coded against Section 8.1 as written** — `POST /api/gate/exit/approve`, no body,
`{"payment": {...}}` back, `status == "AUTHORIZED"`, `amount_usd` checked against the frozen cart total.
Another agent is writing that endpoint tonight. Until it exists the call 404s and the step prints
`SKIP`, not `FAIL`. It also prints `SKIP` while `features.passkeys` is `true`, which it is right now,
because approval then needs a real Face ID prompt on a phone — that is the case you will see in the
morning. To exercise the payment path end to end, set `passkeys: false` in `config.json`, restart, rerun.

**A `FAIL` blocks the rest of the run.** Once a step fails the later steps print `SKIP (an earlier step
failed)` rather than a cascade of false failures. The final `admin reset` still runs either way so the
store is free for the next attempt. Exit code is 1 if anything failed, 0 otherwise.

**`reset_demo.ps1` does not read `reset_demo.sh`** (that file is an empty stub). It parses `ADMIN_PASSWORD`
out of `.env` itself, the way `python-dotenv` does, including one layer of surrounding quotes. `-Password`
and `$env:ADMIN_PASSWORD` override it, in that order. It needs Windows PowerShell 5.1, which is what ships
on this machine.

---

## What I ran, and what happened

Against a scratch app that is `backend.main:app` with `kiosk.router` spliced in ahead of the static mount
(the same two lines from *Integration*), on port 8011:

| Check | Result |
|---|---|
| `pytest -q tests/test_kiosk.py` | 23 passed |
| `pytest -q` (whole suite) | 85 passed, 0 failed (62 already on the branch + 23 new) |
| `GET /api/kiosk/qr/{join,enter,exit}` | `200 image/png`, valid PNG magic |
| `GET /api/kiosk/qr/nope` | `404 {"error":"unknown_qr"}` |
| Decode all three PNGs with `cv2.QRCodeDetector` | each decodes to exactly `kiosk.url_for(which)` |
| `kiosk.html` at 1280x800 | renders, no scrolling, JOIN + ENTER side by side |
| `kiosk.html?side=exit` at 1280x800 | renders, single centred EXIT code |
| Store occupied (session started) | banner goes amber, "Someone is shopping" |
| Backend killed | banner goes grey, "Reconnecting…", QR codes stay on screen |
| Backend restarted | banner returns to green "Open" with no reload |
| `python scripts\e2e_sim.py` | 11 PASS, 1 SKIP (approve, passkeys on), exit 0 |
| `python scripts\e2e_sim.py --url <dead port>` | 1 FAIL, rest SKIP, exit 1 |
| `reset_demo.ps1`, no session | "No session was active", exit 0 |
| `reset_demo.ps1`, session active | "Cancelled session ses_…", exit 0 |
| `reset_demo.ps1 -Password nope` | 401 reported in plain English, exit 1 |
| `reset_demo.ps1` against a dead port | "Unable to connect", exit 1 |

The `e2e_sim` run reproduces the Section S1.2 acceptance number: one Electrolyte tabs at 8% tax is
`$8.00 + $0.64 = $8.64`.

## Known limits

- The QR PNGs are cached for the life of the process. Change `PUBLIC_ORIGIN` or a gate token in `.env` and
  you must restart the backend, exactly as with the printed codes.
- `Cache-Control: public, max-age=3600` is set on the PNG. A kiosk that has been open for less than an hour
  will not re-fetch a changed code without a reload; the `?v=` on the page's image URL covers the reload.
- The page assumes landscape. In portrait it still works but the two codes get narrow; the exit variant is
  fine in portrait.
- `vision_age_ms` in the `health` step is whatever the backend last saw; the step only asserts `ok: true`.
  The freshness assertion is in the `shelf full` step, which waits for `vision_age_ms < 1000`.
