# SpeedMart — morning checklist

Everything a human still has to verify with real hardware, merged from the seven overnight notes in
`docs/overnight/` and ordered so each section only depends on the ones above it.

All commands are **Windows PowerShell, run from `D:\SpeedMart`**, with the venv Python:
`.venv\Scripts\python.exe`. Activate once with `.venv\Scripts\Activate.ps1` if you prefer bare `python`.

**Do sections 0–7 before the demo.** Section 8 (YOLO training) takes hours and the demo works without it —
`features.yolo` is `false` and the shelf runs on ArUco tags. Section 9 is sign-off on decisions made
unattended.

Rough timings: 0–2 about 45 min · 3–4 about 45 min · 5–7 about 60 min · 8 half a day.

---

## 0. Housekeeping (10 min, no hardware)

- [ ] **Refresh the CRLF shell scripts once.** `core.autocrlf=true` leaves `scripts\*.sh` with CRLF endings
      that Git Bash cannot run (`command not found` on every line).
      ```powershell
      Remove-Item scripts\*.sh; git checkout -- scripts
      ```
- [ ] **Tests pass.** `.venv\Scripts\python.exe -m pytest -q` → expect **633 passed, 1 skipped** with the committed
      `config.json`. With a local YOLO-only config (`"yolo": true` and `vision.mode` `"yolo"`), tag-based tests fail
      and `tests\test_live.py` waits forever for a cart change, because tag snapshots no longer move the cart; run
      the suite with the committed config (`git stash push config.json`, run, `git stash pop`).
- [ ] **Try the new Windows runner** (it replaces the two-window fallback the overnight notes described).
      It starts uvicorn, ngrok and the vision worker, prefixes their output, and stops all three on Ctrl+C.
      ```powershell
      powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1
      ```
      Expect `[run_all] api pid …`, `[run_all] backend ready on http://localhost:8000`, a `[tunnel]` line
      (or a clear reason it was skipped), and `[vision] …`. Press **Ctrl+C**: expect `[run_all] stopping...`
      then `[run_all] stopped`, and **no leftover `python.exe` / `ngrok.exe`** in Task Manager.
      Use `-NoTunnel` to skip ngrok, and `.\scripts\run_all.ps1 --no-window` to pass flags to the worker.
- [ ] **Decide who fills in the docs.** `README.md` still has placeholder team names and roles;
      `docs/DEVPOST.md` needs the demo video and repo links.

---

## 1. HTTPS tunnel (15 min — needed for passkeys on phones)

- [ ] `ngrok version` prints `ngrok version 3.x`. If not: `winget install ngrok.ngrok`, open a **new**
      terminal, then once `ngrok config add-authtoken <token from dashboard.ngrok.com>`.
- [ ] `.env` has `PUBLIC_ORIGIN=https://stump-isotope-glorious.ngrok-free.dev` and
      `RP_ID=stump-isotope-glorious.ngrok-free.dev` (host only, no scheme, no trailing slash).
      **The domain must never change or every registered passkey breaks.**
- [ ] Start everything: `powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1`.
      Expect ngrok to report `Forwarding https://stump-isotope-glorious.ngrok-free.dev -> http://localhost:8000`.
- [ ] **Phone on cellular data, WiFi off**: open `https://stump-isotope-glorious.ngrok-free.dev/api/health`.
      Expect `{"ok":true,...}`. The free ngrok domain may show a "Visit Site" interstitial once — tap it.
- [ ] Desktop Chrome DevTools on the **tunnel URL** → Application → Cookies: after any login the `session`
      cookie shows **Secure** ticked.
- [ ] Same DevTools check on **`http://localhost:8000`**: the `session` cookie is **not** Secure and the
      admin page stays logged in. (This is the integration fix described in section 9 — if the admin page
      logs you straight back out, that fix is not in.)

---

## 2. Gate tokens and QR codes (15 min)

- [ ] **Replace the gate tokens.** `.env` currently holds the spec's example values (`entry-7d2f`,
      `exit-91ac`), which are printed in the public spec. Run twice and paste one value into each line:
      ```powershell
      .venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(6))"
      ```
      Set `ENTRY_GATE_TOKEN=entry-<value>` and `EXIT_GATE_TOKEN=exit-<value>`, then restart the backend.
- [ ] Turn the gates on: `config.json` → `"gates": true`. Restart the backend.
- [ ] `.venv\Scripts\python.exe scripts\gen_qr.py` → four `wrote qr\...` lines.
- [ ] Open `qr\all.pdf`: three pages labelled **1 · JOIN**, **2 · ENTER**, **3 · EXIT**, URLs on the tunnel
      domain. Print at 100% or show on a tablet.
- [ ] **Scan each code with a real phone camera, standing where a judge will stand.** 1 opens the join page,
      2 the entry gate, 3 the exit page ("You're not in the store" until you have entered). This is the only
      way to prove on-screen size, brightness and glare are good enough — the codes were only decoded in
      software overnight.

---

## 3. Gate screen and bay number cards (30 min)

There are **no bay LEDs and no status RGB LED** (no soldering, no breadboard). The only board is the LilyGO
T-Display-S3 at the gate: USB powered, **nothing wired to it**. Shoppers find their bays by the printed number
cards and the glowing shelf map on their phone and the kiosk. The screen firmware was rebuilt overnight as an
animated payment-terminal UI (full-frame canvas in PSRAM, u8g2 fonts, brand palette from app.css); it compiled
clean but **nothing here was run on hardware**.

- [ ] **Print the bay number cards:**
      ```powershell
      .venv\Scripts\python.exe scripts\gen_bay_cards.py --out tags\speedmart_bay_cards.pdf
      ```
      Open `tags\speedmart_bay_cards.pdf`, print at 100% ("fit to page" off). Cut out cards **1 to 5** (big number, product
      name underneath) and **tape them to the shelf front**, left to right, each under its product: card 1 under
      bay id 0 (Hydration drink), then Energy drink, Chips, Water, and card 5 under bay id 4 (Vegan snack). The same numbers show on every
      shelf map, so a mismatch sends shoppers to the wrong bay. Keep the cards out of the camera's bay ROIs.
- [ ] Find the port: `.venv\Scripts\python.exe -m backend.serial_bridge --list-ports`.
- [ ] Flash (`pio` is not on PATH on this machine; the first build downloads the U8g2 font library, ~2 min):
      ```powershell
      cd firmware\shelf_esp32
      & "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -t upload --upload-port COM4
      cd ..\..
      ```
      If the upload cannot open the port, hold **BOOT** (GPIO 0), tap **RST**, release BOOT, and run it again.
- [ ] Close anything holding COM4 (the backend!), then open the monitor:
      ```powershell
      & "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" device monitor -p COM4 -b 115200
      ```
- [ ] `READY` appears and the **LCD lights up in landscape** with the idle screen: the SpeedMart logo (blue
      rounded square with the bag and bolt) next to "SpeedMart", a slow **breathing blue glow** behind them, and
      "Tap or scan to enter" with a small NFC wave (three arcs lighting up outward) at the bottom. Check:
      - [ ] the picture is **not shifted or wrapped** (column offset 35), and the 3 px blue line sits at the very top,
      - [ ] the text is **not mirrored** — if it is upside down, change rotation `1` to `3` in `src/main.cpp`,
      - [ ] "Mart" and the logo are **blue, not orange** — if red and blue are swapped the panel needs BGR,
      - [ ] the backlight is on (GPIO 38). If serial works but the screen is black, confirm GPIO 15 goes HIGH,
      - [ ] the animation is smooth with **no flicker or tearing** (every frame is drawn off screen and flushed at once).
- [ ] Type `PING` → `PONG`.
- [ ] **Demo mode, the quickest visual check:** type `DISP,DEMO`. The screen cycles every 4 s through
      idle → "Welcome, Maya" → $12.96 / 3 items → Find bay 2 and 4 → $18.45 / 4 items (the number **counts up**
      from 12.96 and pulses) → APPROVED → DECLINED → "Maya is shopping" → idle, forever. Every change should
      **slide and fade** (about a quarter of a second), never a hard cut. Any other `DISP,...` line ends the demo.
      This is the mode to film the demo video with.
- [ ] Type each screen and check the look. Sending the same line twice must **not** restart its animation:
      - `DISP,WELCOME,Maya` → small "Welcome," then **Maya** large in light blue, sliding up into place; a thin
        blue progress line runs along the bottom edge for 3 s (the backend sends TOTAL when it reaches the end).
      - `DISP,TOTAL,$12.96,3` → "TOTAL" label top left, a pulsing green live dot top right, the amount big
        in the tall numeric font, "3 items" below. `DISP,TOTAL,$8.64,1` → the amount **counts down** to 8.64 over
        0.4 s and pulses, "1 item".
      - `DISP,FIND,2 and 4` → "Find bay" and two big rounded blue **badges "2" and "4"** that pop in with a
        little bounce and glow. After **6 s** it returns to the total on its own. `DISP,FIND,1 2 3 4 and 5` → five
        smaller badges. From `DISP,IDLE`, a FIND returns to the idle screen.
      - `DISP,PAID,$12.96,A1B2C3` → a **green sweep** from the left, a green disc in which a **checkmark draws
        itself**, "APPROVED", the total, "Auth A1B2C3". After **5 s** the screen goes idle by itself (the
        backend's own IDLE, 3 s after the session closes, arrives earlier in a real flow).
      - `DISP,DECLINED` → red tint, a **short horizontal shake**, a red disc with an **X drawing itself**,
        "DECLINED", "Try again on your phone". After **4 s** it returns to the last total (or idle).
      - `DISP,OCCUPIED,Maya` → amber tint, "**Maya** is shopping" (name in amber), "Please wait a moment", a
        highlight gliding back and forth on a thin bar. After **3 s** it returns to the screen it covered.
      - `DISP,WELCOME,Bartholomew Jones` → the name shrinks to fit; `DISP,WELCOME,Maximilian Featherstonehaugh`
        → shrinks, then ends in "..." (DISP arguments are cut to 20 characters by the backend anyway).
      - `DISP,IDLE` → back to the logo.
- [ ] **Backlight dimming:** leave the idle screen alone for 60 s → the backlight fades to about 30 % over a
      second. Type any command (even `PING`) → it is back to full **instantly**.
- [ ] While the screen animates, confirm the **button** still works (hold the lower side button, GPIO 14, for
      1 s → prints `BTN,0`) and `PING` still answers at once mid-transition.
- [ ] End to end: close the monitor, start the backend, admin demo-login, start a session. The LCD shows
      "Welcome, Demo", then "$0.00 / 0 items" after 3 s. Pick an item → the total counts up in about 1 s.
      Admin reset → the logo idle screen after 3 s.
- [ ] With a session running, make a plan on the phone (`intent.html`, chip "Rehydrate after a run") → the LCD
      shows the "Find bay" badges 1 and 4 for 6 s, then the total again.
- [ ] **Unplug and replug USB**: the screen comes back to the current state (resync on `READY`).
- [ ] If the display stays black but `READY` and `PONG` work, the panel did not answer at boot (GPIO 15 power
      enable, or a bad flash): reflash once. The firmware keeps serial and the button alive when the LCD fails,
      and falls back to internal RAM if PSRAM is missing, so a black screen is never a PSRAM problem.

---

## 4. Camera, shelf and motion freeze (30 min — camera, shelf, one hand)

Two windows, or just `scripts\run_all.ps1`. Open `http://localhost:8000/admin.html`, log in, demo-login,
and start a session so the cart is visible.

- [ ] **Recalibrate first — the shelf now has five bays.** The ROIs in `config.json` are placeholders spread
      evenly left to right; they will not match the real shelf. Stop the worker, then:
      ```powershell
      .venv\Scripts\python.exe -m vision.calibrate
      ```
      Drag one rectangle per bay in order **bay 0 to bay 4** (the prompt names the bay and its SKU), `u` undo,
      `r` redo all, `s` save, `q` quit. Saving rewrites only `bays[].roi`, so check with `git diff config.json`.
      Restart the worker afterwards.
- [ ] **Print the new tags:** `.venv\Scripts\python.exe scripts\gen_aruco.py` now writes **10 tags on 2 pages**
      (IDs 0 to 9; 6 and 7 are water, 8 and 9 vegan snack). Print at 100% scale with "fit to page"
      **off**, then measure a black square: 6.0 cm.

| # | Do | Expect |
|---|---|---|
| 4.1 | Nobody near the shelf for 5 s | All bays green, `stable`, `chg` below 0.01. Top line `motion thr 0.02  settle 300 ms  margin 40px` |
| 4.2 | A bay stays yellow / `MOTION` with nobody moving | Press `]` in the worker window until it stays green (+0.005 each), then `s` to save. Console prints `saved motion_threshold ... to config.json` |
| 4.3 | Wave a hand **above** bay 0 without touching | Bay 0 yellow `MOTION` → `settling N ms` → green. **Cart unchanged.** Bay 1 may also go yellow when the hand is in the gap (margins overlap by design) |
| 4.4 | Hover a hand over bay 0 covering the tag for 3 s, **do not lift** | Cart does not change. Overlay shows `pending [..]` while covered; the `N units [..]` line keeps the old units |
| 4.5 | Lift the item, hold it above the shelf, take the hand away | Item enters the cart **once**, about 1 s after the hand leaves (300 ms settle + 700 ms removal hold) |
| 4.6 | Put it back | Leaves the cart about 0.7 s after the hand leaves (400 ms addition hold) |
| 4.7 | **10 picks + 10 put-backs on each bay** | 10/10 correct per bay, no flicker in the cart. Write down any miss and what the overlay showed |
| 4.8 | `"motion_freeze": false`, restart the worker | Top line `motion freeze OFF`, bays still confirm, more flicker risk. **Set it back to `true`** and restart |

Tuning: 4.4 fails (a still hand counts as a pick) → raise `vision.stable_remove_ms` to 900–1200, or lower
the threshold with `[`. Picks feel slow → `stable_remove_ms` 500. Neighbour-bay freezing annoying →
`vision.motion_margin_px` 20. `stable_ms`, `stable_remove_ms` and `motion_margin_px` need a worker restart;
threshold and settle are live (`[ ] - =`, `s` saves).

- [ ] **Eyeball the gray rectangles** (the motion area = ROI + margin). They must not reach into where people
      stand or where the laptop screen is visible. If they do, lower `motion_margin_px`.
- [ ] After pressing `s`, `git diff config.json` shows **only** `motion_threshold` / `motion_settle_ms` changed.

---

## 5. Passkeys on real phones (20 min — needs section 1)

`passkeys: true`, `https_tunnel: true`, backend + ngrok running. Entry is refused ("The shelf camera is
offline") unless a shelf snapshot arrived in the last 2 s, so keep the worker running — or fake it:
```powershell
.venv\Scripts\python.exe scripts\fake_shelf.py loop
```
(type a tag id + Enter to take that unit off the shelf, again to put it back).

### iPhone (Safari) — needs a passcode, Face ID, and iCloud Keychain on

- [ ] Safari → the tunnel URL (or scan QR 1). Enter a first name, move the budget slider, tap **Join with Face ID**.
      Expect the iOS sheet "Save a passkey for stump-isotope-glorious.ngrok-free.dev?" → Continue → Face ID →
      "You're a member. Card linked: Demo card · Visa test •••• 4242 · not your card. Walk to the entry gate."
- [ ] **If you see "Face ID was cancelled" without cancelling**, Safari dropped the tap gesture during signup.
      Tap **Try Face ID again** — it should work first time. **Write down which happened**; if it always needs
      the second tap, tell the team before the demo.
- [ ] Tap **Not you? Sign out** → **Already a member? Sign in with Face ID** → press **Cancel** on the iOS sheet.
      Expect a red strip "Face ID was cancelled. Tap the button to try again." and the button still there.
      Tap again and complete Face ID → signed in as the same name.
- [ ] Reload the page → still signed in.

### Android (Chrome) — needs a screen lock and a Google account

- [ ] Join with a different first name → "Create a passkey" sheet → fingerprint → member screen with the card line.
- [ ] Sign out → **Sign in with Face ID** → fingerprint → back in as the same name.
- [ ] Dismiss the sheet mid-prompt → "Face ID was cancelled…" and the retry works.
- [ ] *(Optional, spare phone with no screen lock)* Join → expect "This phone has no screen lock set up, use
      the demo account."

- [ ] Desktop `http://localhost:8000/admin.html` → the event log shows `passkey_registered` and `passkey_login`.

---

## 6. The full loop on a phone (30 min — the demo itself)

`gates: true`, `passkeys: true`, shelf feed running. Watch the **LCD** during this section too.

- [ ] Scan **QR 2** → **Enter with Face ID** → Face ID. Expect "Welcome in, `<name>`", the LCD showing "Welcome, `<name>`" then the total, and the cart page with a green live dot.
- [ ] **Second phone** (or a desktop signed in as another member) scans QR 2 while the first shopper is inside.
      Expect "Someone is shopping right now. Try again in a minute." and `<name> is shopping` on the LCD for 3 s.
- [ ] Take one hydration drink off the shelf → row "Hydration drink × 1 $3.50", total **$3.78**, LCD total updates.
- [ ] Scan **QR 3**. Expect the itemized preview, total $3.78, "Paying with Demo card · Visa test •••• 4242 · not your card", and the
      collapsible **"What the payment network sees"** (agent, merchant SpeedMart #01, max $20.00, single use,
      expiry, intent "Pay $3.78 to SpeedMart #01 for 1 item", sandbox label). Button **Approve $3.78 with Face ID**.
- [ ] Tap **Keep shopping** → back on the cart page, cart live again. Scan QR 3 again.
- [ ] Admin page → turn **force-decline on**. Tap Approve → Face ID. Expect red "Declined: Card declined
      (sandbox: forced by staff). You can try again.", **LCD `DECLINED` for 4 s then
      back to the total**, button "Try again with Face ID".
- [ ] Turn force-decline **off** → **Try again with Face ID** → Face ID. Expect **LCD
      `APPROVED` with the auth code**, and the receipt page: green check, "Paid $3.78", card label, auth code,
      "+3 points · 3 total", "Show payment record" JSON. `data\payments.log.jsonl` has a DECLINED and an
      AUTHORIZED line.
- [ ] **Over budget:** sign up with budget $10, enter, take the hydration drink, the energy drink and the vegan
      snack ($11.34; the phone says "$1.34 over budget", put back the Vegan snack), scan QR 3. Expect amber
      "This is over your $10.00 limit. Put something back to continue." and **no approve button**. Keep shopping,
      put one back, scan QR 3 again → approve works.
- [ ] **Empty cart:** enter, take nothing, scan QR 3 → "Nothing to pay, see you soon" and the store is free.
- [ ] **Admin reset while someone is inside** frees the store (QR 2 works for the next person).
- [ ] **Passkeys off:** set `"passkeys": false`, restart. Join shows **Join**, entry **Enter**, exit
      **Confirm $X.XX**; no Face ID prompts anywhere. Set it back to `true`.

---

## 7. Payments, the AI agent and "Tell us what you need" (30 min)

### 7a. Stripe test mode

- [ ] `.env` `STRIPE_SECRET_KEY` starts with `sk_test_`. Anything else makes the backend refuse to start with
      a clear message — that is intended.
- [ ] *(Opt-in)* One real **test-mode** $1.00 charge:
      ```powershell
      .venv\Scripts\python.exe scripts\stripe_smoke.py
      ```
      Type `yes`. Expect `status succeeded`, `amount $1.00 USD` and a dashboard link. Check
      https://dashboard.stripe.com/test/payments for "SpeedMart smoke test", Succeeded.
- [ ] After a real app charge (section 6), the Stripe **test** dashboard shows $3.78, description
      "SpeedMart #01", metadata `session_id` and `instruction_id` matching the receipt, and a customer named
      after the member with the Visa 4242 test card attached. The receipt meta says "Stripe test mode" and the
      auth code is the last 6 characters of the PaymentIntent id.
- [ ] **Demo member backfill:** admin demo-login on a team phone, shop, scan QR 3. A customer "Demo Shopper"
      with a test Visa appears in the dashboard **at that moment** (created at first checkout, not at startup).
- [ ] **Network-down fallback:** on the exit page (after the quote), disconnect the laptop's uplink, then
      approve. Within ~20 s expect the receipt with "sandbox mock" and a `MOCK####` auth code, and
      `stripe_fallback` in the event log. No crash.

> Note: `scripts\e2e_sim.py` was run during integration with `passkeys: false` and **did make one Stripe
> test-mode charge of $12.42**. You will see it in the test dashboard; it is not a real payment.

### 7b. The AI store agent (S4.3)

With a real `ANTHROPIC_API_KEY` in `.env` and `features.llm: true`:

- [ ] Enter the store, pick chips → an **energy drink suggestion that says it has caffeine** on the phone within
      ~2 s. Check `data\events.log.jsonl` for `agent_line` with `"source": "llm"` (a reply that leaves out
      caffeine is rejected and the template line shows instead, which also says caffeine).
- [ ] Pick a water → a **hydration drink suggestion** at $3.50 (no caffeine mention).
- [ ] Pick 2 vegan snacks + 2 hydration drinks + 2 energy drinks on a $20 budget ($22.68) → the phone says
      you're **$2.68 over** and suggests putting back the Vegan snack.
- [ ] Put a bag of chips in bay 0 → the **misplaced** line appears ("Chips is in the wrong bay. Please return
      it to bay 3.").
- [ ] Blank `ANTHROPIC_API_KEY`, restart → **template lines still appear**, no errors in the log.
- [ ] Watch for `agent_llm_rejected` events. If good lines are being rejected over a capitalised word we did
      not expect, add it to `_ALWAYS_OK` in `backend/agent.py`.

### 7c. "Tell us what you need" (F18)

- [ ] On the cart page, tap **Tell us what you need** → `intent.html`. Tap the chip "Rehydrate after a run".
      Expect Hydration drink + Water, est. total $5.40, budget $20.00, and "Planned by the store AI…"
      (or "…by keyword match" if the LLM is off or slow).
- [ ] The other chips: "Study session fuel" → Energy drink + Chips, $5.94, and the Energy drink's reason
      mentions caffeine. "Vegan snack and a drink" → Water + Vegan snack, $5.94 (never the chips or the energy
      drink).
- [ ] **Glowing bays:** under the plan cards a shelf map shows bays 1 to 5 with product names and counts; the
      planned bays glow and pulse, and the strip says "Look for bay 1 and bay 4, they're glowing on your
      screen." The cart page's small shelf map glows the same bays. They stop on **Start over** and when the
      session ends. With **Reduce motion** on (iPhone: Settings → Accessibility → Motion) the glow stays but
      does not pulse. With `hardware_leds: true` the gate screen also shows "Find bay 1 and 4" (section 3).
- [ ] **Phone mic.** Android Chrome: the mic button appears; tap it, say "something to drink", and the plan
      appears when you stop talking. iPhone Safari: works only on iOS 14.5+ **over the tunnel**. If the button
      is missing it hides itself — that is expected, not a bug. *(Speech was never tested overnight — no microphone.)*
- [ ] **With a plan active, pick items and watch the agent line** — it should now reference your goal
      ("…right on track for rehydrate after a run"). This is new at integration; if the lines ignore the goal,
      check `agent_llm_rejected` in the event log.
- [ ] **Exit screen comparison:** with a plan active, scan QR 3. Above the receipt preview expect a line like
      "You asked for rehydrate after a run under $20: you have both items, $5.40." With no plan, nothing shows.
- [ ] LLM latency: `Get-Content data\events.log.jsonl -Tail 20 | Select-String intent_plan` —
      `"source": "llm"` means the AI planned it; `"fallback_reason"` says why rules answered instead.

### 7d. Disputes and the auto refund line (F20)

- [ ] `config.json` has `"disputes": {"auto_refund_max_usd": 2.00}`. Only the Water ($1.62 with tax) is under it.
- [ ] **Instant AI refund:** while shopping, stick a blank note over one Water's tag and leave the bottle in
      bay 4 (the camera now charges for it). Pay, then on the receipt tap **Report a problem** → Water. Within
      about 20 s: "Refunded $1.62 after a review of the shelf photos", and the admin card says "Auto approved by
      policy". Peel the note off.
- [ ] **Admin review queue:** take an Energy drink for real, pay, **Report a problem** → Energy drink ($3.24).
      The laptop chimes, the **Review queue** badge turns red, the card shows the verdict and the clip. **Keep
      the charge** with a note → the receipt says "Charge confirmed" with the note. The AI never keeps or denies
      a charge on its own.
- [ ] Set `auto_refund_max_usd` to `0`, restart → every dispute waits for a person. Set it back to `2.00`.

### 7e. The kiosk tablet (Dell Venue, 10", landscape): the talking guide

The entrance tablet is now a guide (docs\voice.md, "Kiosk agent"): a step tracker, a spoken 30 s tour, the
shopper's panel, spoken cart lines and (with voice on) a conversation with the ElevenLabs agent. Without the token
it is the old public page.

**Setup on the laptop**
- [ ] `.env`: set `KIOSK_TOKEN` to a long random value and `ELEVENLABS_VOICE_ID` to the voice to speak with
      (ElevenLabs → Voices → pick a warm, clear voice → copy its ID). `ELEVENLABS_API_KEY` is the one voice already uses.
      ```powershell
      .venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(24))"
      ```
- [ ] Restart the stack, then `.venv\Scripts\python.exe scripts\check_env.py` → `PASS KIOSK_TOKEN` and
      `PASS ELEVENLABS_VOICE_ID  voice '<name>'`.
- [ ] **Agent dashboard** (docs\voice.md, "Dashboard setup"): paste the new **first message**
      ("Hi {{first_name}}, you have ${{remaining_usd}} to spend. …"), the new system prompt (it has a "Kiosk mode"
      block at the end), add the dynamic variables `remaining_usd`, `is_first_visit`, `mode`, and set **Max
      conversation duration to 300 s**. Without the new variables the agent can't start from either device.

**Open the kiosk (on the tablet)**
- [ ] The microphone only works on a secure page, so use the **tunnel URL, not `http://<laptop-ip>:8000`**:
      ```powershell
      Start-Process msedge -ArgumentList '--kiosk', 'https://stump-isotope-glorious.ngrok-free.dev/kiosk.html?k=<KIOSK_TOKEN>', '--edge-kiosk-type=fullscreen'
      ```
      (exit side, second tablet only: `https://…/kiosk.html?side=exit`, no token, it stays public.) If ngrok's
      "Visit Site" page shows first, tap it once.
- [ ] Expect a full screen **Start kiosk** button. **Tap it once.** Edge asks for the microphone: tap **Allow**.
      The step tracker (1 Join, 2 Enter, 3 Grab items, 4 Scan exit) appears with step 1 pulsing, and the guide
      panel says "Scan a code with your phone camera to start." The Start button comes back on every reload
      (browsers only allow sound after a tap); tap it again each time.
- [ ] If Edge asks for the microphone again after every restart, kiosk mode is running a private session. Tap
      Allow each time, or pre-allow the tunnel origin with the Edge policy `AudioCaptureAllowedUrls` (an IT
      setting on the tablet; only if you want it).
- [ ] **Wrong or missing token check:** open the same URL with `?k=wrong`. Expect the old page (QR codes, status,
      shelf map), no Start button, no steps, no voice.

**Sound**
- [ ] Tablet volume about 70 to 80 % (Windows: Settings → System → Sound; not muted, output = the tablet's
      speakers or the USB speaker you'll use). Tap **New here? Tap to learn how SpeedMart works**: the tour
      speaks for about 30 s and highlights steps 1 to 4 in turn. **Listen from where a shopper stands in front
      of the shelf** and from the queue; raise the volume or add a small USB speaker if the venue is loud.
- [ ] First tour after a restart: the lines may appear as captions only for a few seconds while the fixed
      phrases are generated once; they are then cached in `data\tts_cache\`. Run the tour once before judging.
- [ ] Captions only (no sound at all) means speech is off: check `ELEVENLABS_VOICE_ID`, the key and
      `features.voice`, and look for `tts_error` in `data\events.log.jsonl`.

**Walk it through as a first timer** (docs\DEMO.md has the full walkthrough)
- [ ] Store empty, stand at the shelf and wave a hand over a bay: within a few seconds the kiosk says "Hi! New
      here? Tap the screen, and I'll show you how SpeedMart works." and the tour button pulses (at most once
      every 2 minutes; needs `motion_freeze` on).
- [ ] Sign up a **new** member and enter: steps 1 and 2 get checks, step 3 pulses, the JOIN/ENTER codes make way
      for "Hi, <name>" (budget left, cart total, items) and the EXIT code, and the kiosk says "Welcome to your
      first visit, <name>. Just grab what you want; …". Then the agent: "Hi <name>, you have $10 to spend. What
      are you looking for today?"
- [ ] Say "I'm thirsty after a run": the agent plans (Hydration drink and Water), says bay 1 and bay 4, and those
      bays glow on the kiosk's shelf map. On the phone, `intent.html` now shows **Talk to the kiosk**, disabled.
- [ ] Take the chips: about 1.5 s after the cart settles the agent mentions it (a contextual update, it does not
      cut you off). Stop talking for 2 minutes: the conversation ends and the kiosk itself then says cart lines
      ("See, the Chips are already in your cart. You're at $2.70."). The phone's button is back.
- [ ] Leave items in the cart and do nothing for 60 s: "When you're ready, scan the exit code to review and pay."
- [ ] Scan the exit: step 4 pulses, the conversation stops. Pay: all four steps get checks, and the name and
      totals disappear from the screen at once.
- [ ] Enter again as the same member: "Welcome back, <name>. You have $… to spend." (shown as a caption when the
      agent greets, spoken when voice is off).
- [ ] Reload the kiosk mid-visit (tap Start again): it comes back on the right step with the panel, and says no
      greeting a second time.

**Screen basics (unchanged from before)**
- [ ] **Set the Venue to never sleep and brightness high** (Settings → System → Power & battery → Screen and
      sleep → **Never** on both). A dimmed screen kills QR scanning.
- [ ] **Lock rotation in landscape**, or the layout fights auto-rotate.
- [ ] **Read the status banner and the step tracker from where the judges will queue** (about 2 m).
- [ ] Scan the on-screen codes with a phone from that same distance.
- [ ] Kill the backend → grey "Reconnecting…" **and the QR codes stay on screen**. Restart → green "Open" with
      no reload.
- [ ] **Decide about the NFC hint.** "Tap your phone here" is printed text with an animated glyph — there is
      **no NFC hardware and no Web NFC code anywhere**. Either put an NFC tag behind the bezel programmed with
      the same URL the ENTER QR carries, or remove the text. **Decide before the demo.**
- [ ] If there is only one tablet, run the **entrance** page with the token: during a visit it shows the EXIT
      code itself (gates on).

### 7f. The phone UI on real devices

- [ ] **iPhone over the tunnel:** notch and home-indicator padding on the header, bottom action bar, toast and
      footer; **Add to Home Screen** (name SpeedMart, right icon, opens standalone); dark mode; and that the
      **Face ID sheet does not fight the action bar**.
- [ ] **Android Chrome:** the install prompt shows the maskable icon and the status bar matches the page
      background.
- [ ] Turn on **Reduce Motion** on a phone once: animations collapse and the layout stays identical.
- [ ] **Team laptop at the resolution you will use for judging:** open `/admin.html`. Log in, Demo login, start
      a session from `/store.html`, tap the `+`/`−` overrides, toggle Force decline, Reset. The event log is
      newest-first. Confirm it is legible from where the operator will sit.
- [ ] Optional tour of every component: `http://localhost:8000/design.html` (light and dark side by side).

---

## 8. YOLO: capture, train, verify (half a day — do only if you want tag-free detection)

`features.yolo` is `false` and everything works on ArUco tags. Follow `training/TRAINING.md`; checkpoints:

| # | Do | Expect |
|---|---|---|
| 8.1 | Close the worker. `.venv\Scripts\python.exe -m training.capture` | Window with green bay boxes, `saved 0 (target 250-400)`, `tags visible now: N` |
| 8.2 | `space`, move products around, `space` again | Red AUTO dot; count rises ~2/s; files in `training\raw\<date-time>\`. **Saved images have no boxes or text on them** |
| 8.3 | Capture with tags covered or removed | `no tags visible` share reaches **at least 50%** (the line turns green) |
| 8.4 | `(Get-ChildItem training\raw -Recurse -Filter *.jpg).Count` | **250 to 400** |
| 8.5 | Label + export in Roboflow (YOLOv8), unzip to `training\dataset`, then `.venv\Scripts\python.exe -m training.check_dataset training\dataset` | `RESULT: OK`, no class under 100 boxes (else capture and label more) |
| 8.6 | Colab: upload `training\train_colab.ipynb`, GPU runtime, Run all, upload the zip | Cell 7 ends `PASS: every class mAP50 >= 0.90`; the browser downloads `speedmart_yolo.pt` |
| 8.7 | `Move-Item $HOME\Downloads\speedmart_yolo.pt models\speedmart_yolo.pt -Force` | File exists at `models\speedmart_yolo.pt` |
| 8.8 | `.venv\Scripts\pip.exe install -r requirements-yolo.txt` (large — pulls PyTorch; for an NVIDIA GPU install the CUDA build from pytorch.org first) | Installs without errors |
| 8.9 | `.venv\Scripts\python.exe -m vision.yolo_detect training\raw\<run>\<file>.jpg` | Prints `device cpu` (or `0` / `mps`), each box with class, conf and bay, and `per bay: {...}` matching the photo |
| 8.10 | `.venv\Scripts\python.exe -m vision.yolo_detect` (live) | Magenta boxes on the products, per-bay counts; `q` quits |
| 8.11 | `"yolo": true`, restart backend **and** worker | Worker log `YOLO loaded: speedmart_yolo.pt on device ...`; overlay top line `YOLO on (cpu, every 3)`; bay lines show `yolo elx:2`. **FPS at least 15** — else set `yolo_every_n_frames` to 5 or 6 |
| 8.12 | Admin page shelf state | `yolo_counts` filled per bay |
| 8.13 | **Cover every tag with tape; 10 picks + put-backs per bay** | **At least 9/10 register correctly** (the S5.2 acceptance number) |
| 8.14 | Bays keep flipping yellow with `hold` restarting while nothing moves | The YOLO count is flickering: raise `yolo_conf` to 0.6–0.7, or retrain with more frames of that product |
| 8.15 | `"yolo": false`, restart both | Behaves exactly as section 4; no YOLO line or boxes |
| 8.16 | Rename `models\speedmart_yolo.pt` away, `"yolo": true`, restart the worker | One `WARNING: features.yolo is true but the model file ... is missing`, top line `YOLO unavailable (see log), tags only`, everything else works. **Rename it back** |

Fusion check without a camera (backend running, worker stopped):
```powershell
.venv\Scripts\python.exe scripts\fake_shelf.py --yolo loop
```
Type `c1` + Enter (cover tag 1): with `"yolo": true` the cart does **not** change; with `"yolo": false`
a Hydration drink enters the cart. Type `1` + Enter (really remove unit 1): it enters the cart either way.

---

## 9. Decisions made without a human — please sign off

- [ ] **Section 8 of the spec now needs an addition.** F18 added three routes (`POST /api/intent`,
      `GET /api/intent/current`, `DELETE /api/intent`) and the `Plan` object, and the exit quote
      (`POST /api/gate/exit/quote` and `POST /api/dev/checkout`) now returns a third key **`plan_check`**
      alongside `cart` and `instruction`. CLAUDE.md says Section 8 must not change without asking — **this is
      the ask.** The shapes are in `docs/overnight/intent.md` → "Proposed Section 8 addition".
- [ ] **Plain-http session cookies.** `backend/main.py` gained `PlainHttpCookieFix`: when `https_only` is on
      (tunnel + https `PUBLIC_ORIGIN`), the `Secure` flag is dropped from the session cookie **only for
      requests that arrived over plain http**. Without it, the admin page on `http://localhost:8000`, the
      kiosk tablet over the LAN and `scripts\e2e_sim.py` log in and are immediately logged out. The tunnel's
      cookie is still always Secure. Verify with the two DevTools checks in section 1.
- [ ] **The two-line `backend/store.py` diff** (the `agent` import and the `agent.on_cart(...)` hook in
      `compute_live_cart`) — review and approve.
- [ ] **The intent plan now reaches the AI agent.** `backend/agent.py` sends the plan's `goal_summary` and
      item **names** to the LLM (never its prices, which the validator would reject) and allows the goal's own
      words in the reply. No plan means no `goal` key, exactly as before.
- [ ] **Gate tokens in `.env` are still the spec's public example values** until you do section 2.
- [ ] Decide whether the kiosk's **NFC hint** stays (section 7e).
- [ ] **Kiosk agent: Section 8 changed (pre-approved).** New 8.16 (every `/api/kiosk/*` route except the QR
      codes needs `?k=<KIOSK_TOKEN>`), new socket messages in 8.4 (`shelf_activity` public, `kiosk_visit`,
      `kiosk_cart`, `kiosk_say` for the kiosk, `kiosk_voice` for the shopper's phone), and `GET /api/voice/session`
      now also returns `remaining_usd`, `is_first_visit`, `mode` and can answer 409 `kiosk_conversation_active`.
      Two new `.env` keys (5.1). Review the spec diff.
- [ ] **Kiosk agent decisions to sign off:**
      - One ElevenLabs agent serves phone and kiosk. Its **first message changed** to "Hi {{first_name}}, you have
        ${{remaining_usd}} to spend. What are you looking for today?" for both, and the phone now sends the same
        six dynamic variables. Max duration 300 s (the phone still stops itself at 2 minutes).
      - During a visit the entrance kiosk shows the **EXIT QR code** (gates on), so a single tablet covers step 4.
      - A **cancelled visit does not count** toward "welcome back", so a first timer whose visit was reset is still
        greeted as new. Returns never count.
      - Kiosk speech is ElevenLabs too, so it follows `features.voice`: voice off = captions only, no conversation.
      - Captions of spoken lines name products and prices (the shopper's own picks); the panel itself only shows
        first name, budget left, cart total and item count, and clears when the visit ends.
      - With a conversation, a returning shopper's short greeting is only a caption (the agent's first line says it);
        a first timer hears the kiosk's fuller welcome first, then the agent.

---

## 10. Right before judging

- [ ] `powershell -ExecutionPolicy Bypass -File scripts\reset_demo.ps1` between judges (it frees the store
      and clears overrides). `-Password` or `$env:ADMIN_PASSWORD` override the value it reads from `.env`.
- [ ] Every product back on its own bay, tags facing the camera.
- [ ] Phones charged, on cellular, already signed in.
- [ ] `data\payments.log.jsonl` and `data\events.log.jsonl` are the story for judges — have the admin page open.
