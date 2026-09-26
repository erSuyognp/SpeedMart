# Overnight: gate display + bay highlight (F12)

> **Superseded (design change): no bay LEDs, no status LED.** The board is now only the gate screen, USB
> powered with nothing wired to it. The backend no longer sends `LED`, `SHELF`, `GATE` or `HILITE` (the firmware
> still accepts them); resync on connect / `READY` is just the current `DISP` line; `highlight()`,
> `clear_highlights()`, `send_bay_leds()` and `send_timed()` are gone. A new plan shows `DISP,FIND,<cards>`
> ("Find bay 2 and 4", cards = bay id + 1) via `serial_bridge.show_find()` and the shelf maps glow via the
> `plan_bays` WebSocket message. The mapping table below is kept current; the rest is the overnight record.

Branch `claude/speedmart-display-bay-highlight-5d8c21`. Files touched: `firmware/shelf_esp32/platformio.ini`,
`firmware/shelf_esp32/src/main.cpp`, `backend/serial_bridge.py`, `tests/test_serial.py` (one test updated),
`tests/test_serial_display.py` (new), this file. Nothing else was edited.

## Results

| Check | Result |
|---|---|
| `pio run` in `firmware\shelf_esp32` (PlatformIO 6.2.0, espressif32 platform, arduino-esp32 2.0.17) | PASS, no warnings in `src/main.cpp`. RAM 5.7 %, flash 4.6 % |
| `pytest -q` (whole suite, from the project `.venv`) | PASS, 75 passed |
| `tests/test_serial.py` + `tests/test_serial_display.py`, run 3 times for timing flakiness | PASS 22/22 each run |
| Anything on the physical board | NOT RUN: no board attached overnight. See the morning checklist |

## Task 1: onboard LCD as the gate display

- Library: `moononournation/GFX Library for Arduino@~1.4.9`. The 1.5+/1.6 releases target arduino-esp32 core 3.x; the
  installed espressif32 platform ships core 2.0.17, and 1.4.9 builds cleanly on it.
- Bus: `Arduino_ESP32PAR8Q` (the S3's 8-bit parallel/i80 bus), `Arduino_ST7789` 170x320, IPS, column offset 35,
  rotation 1 (landscape, 320 wide x 170 tall).
- Pins from the official `Xinyuan-LilyGO/T-Display-S3` repo, `examples/factory/pin_config.h` (cited in `main.cpp`):
  BL 38, D0..D7 = 39 40 41 42 45 46 47 48, RES 5, CS 6, DC 7, WR 8, RD 9, POWER_ON 15. They match Arduino_GFX's
  own `LILYGO_T_DISPLAY_S3` config (PDQgraphicstest `Arduino_GFX_dev_device.h`), which also uses col offset 35.
- None of these pins collide with our LEDs (bays 1, 2, 18, 17, 21; status RGB 10, 11, 12) or the button (14).
  GPIO 18/17 are SDA/SCL and 21 is Touch RES only on the touch variant, which we do not use.
- Drawing is only `fillScreen`, `fillRect`, `fillCircle`, `drawLine` and the built-in 6x8 font scaled up.
  Long names shrink to fit the width.
- No flicker: the firmware remembers the last `DISP,...` line and ignores the same line again. A new screen is one
  `fillScreen` plus draws, done once. The IDLE animation is five dots with a glow wave. Each 40 ms it repaints only
  the dots whose color changed, and it never clears the screen. The backend also skips resending an unchanged screen.
- Boot order: power pin HIGH, LEDs, `gfx->begin()`, backlight on, `READY`, then `DISP,IDLE`. If `begin()` fails,
  drawing is skipped and LEDs, button and serial still work.
- The serial line buffer grew from 64 to 96 chars so that `DISP,PAID,<20 chars>,<20 chars>` always fits.

### Screen reference (newline terminated)

| Command | Shows |
|---|---|
| `DISP,IDLE` | "Speed" in white and "Mart" in orange, "Tap or scan to enter", animated dots |
| `DISP,WELCOME,<first name>` | green top bar, "Welcome,", the name in large green text, "Happy shopping" |
| `DISP,TOTAL,<total>,<count>` | "Your cart", the total in large text exactly as sent (the backend sends `$12.96`), then "`<count>` item(s)" |
| `DISP,PAID,<total>,<auth>` | green circle with a check mark, "APPROVED", the total, "Auth `<auth>`" (left out when auth is empty) |
| `DISP,DECLINED` | red bars, "DECLINED", "Try again on your phone" |
| `DISP,OCCUPIED,<first name>` | amber bar, "`<name>` is shopping", "Please wait" |

The board ignores unknown screens and keeps showing the current one.

## Task 2: HILITE

- `HILITE,<bay>,ON|OFF` works for bays 0..2. While ON, the bay LED blinks at 2 Hz (250 ms on, 250 ms off, from
  `millis()`) whatever its `LED,<bay>` state is. `LED,<bay>` commands still update the underlying state during
  the blink, and OFF returns to that state.
- `HILITE,ALL,OFF` clears every highlight. The spec does not define `HILITE,ALL,ON`, so the board ignores it.
- Bad input such as `HILITE,x,ON`, `HILITE,5,ON` or `HILITE,0,MAYBE` is ignored.

## Task 3: backend (`backend/serial_bridge.py`)

New API. Every function is a no-op that returns `False` while `features.hardware_leds` is false, because no bridge exists then:

- `display(screen, *args)`: sanitizes each argument with `sanitize_arg`. That converts to ASCII (NFKD, so "José"
  becomes "Jose", since the board font is ASCII), strips commas, turns CR/LF into spaces, collapses whitespace and
  caps the result at 20 chars. It then sends `DISP,...`. An unknown screen raises `ValueError`.
- `highlight(bay, on)`, `clear_highlights()`.
- The bridge tracks the current `DISP` line and the set of highlighted bays.
- Resync: on every (re)connect and on `READY` the bridge sends `LED,*` lines, then the current `DISP`, then `HILITE,ALL,OFF`,
  then `HILITE,<b>,ON` for each highlighted bay.
- `send_bay_leds()`, called after each shelf change, now sends only the LED lines. It used to send the full resync.
- `start()` subscribes `DisplayDirector` to `backend.eventlog` once (the same pattern as `ws.py`). If a session is
  already active (backend restarted mid-session), it primes the screen with that session's TOTAL.

### Event → screen mapping

> Resolved at integration: `backend/payments.py` logs `payment` with `status`, `amount_usd` and
> `auth_code`, so the GUESS rows below were replaced by the real event. `PAYMENT_EVENTS` is now
> `("payment",)` and `_on_payment` reads those fields. `cart_changed` and `store_occupied` already
> matched `backend/store.py`.

| Event (`type`) | Condition | Screen |
|---|---|---|
| `session_state` | `to=IN_STORE`, `from` null | `WELCOME,<first name of member_id>` for 3 s, then `TOTAL` with the latest cart |
| `session_state` | `to=IN_STORE`, `from=CHECKOUT_PENDING` (exit cancelled) | `TOTAL` |
| `session_state` | `to=PAID` and PAID not already shown | `PAID,<last total>,` (empty auth; fallback if no payment event arrives) |
| `session_state` | `to=CLOSED` or `CANCELLED` | `IDLE` after 3 s (cancelled if another screen comes first) |
| `cart_changed` (store.py: `session_id, state, items{sku:qty}, total_usd, warnings, source`) | `state` IN_STORE or CHECKOUT_PENDING | `TOTAL,$<total_usd 2dp>,<sum of qty>`. While WELCOME/OCCUPIED/DECLINED/PAID is up the numbers are only stored; that screen hands over to TOTAL |
| `store_occupied` (store.py: `member_id, occupant_session_id`) | someone tries to enter while occupied | `OCCUPIED,<occupant first name>` for 3 s, then back to `TOTAL` |
| `payment` (payments.py: `status`, `amount_usd`, `auth_code`) | `status=AUTHORIZED` | `PAID,<amount_usd>,<auth_code>` |
| `payment` | `status=DECLINED` | `DECLINED` for 4 s, then `TOTAL` (the session stays CHECKOUT_PENDING per 7.1) |
| payment with `status=ERROR` or any other status | | ignored |
| (direct call) `show_find(bays)` from `backend/intent.py` | a new plan with bays, and nobody else in the store | `FIND,<cards>` for 6 s (`FIND_HOLD_S`), then `TOTAL` (or the screen it covered, e.g. `IDLE`). Cart changes meanwhile only update the stored numbers |

The amount is `amount_usd`, falling back to the last cart total; the auth code comes from `auth_code`.
Any other status (including `ERROR`) is ignored, and so is any other event name.

Timings live in module constants: `WELCOME_HOLD_S=3`, `IDLE_AFTER_S=3`, `OCCUPIED_HOLD_S=3`, `DECLINED_HOLD_S=4`.
A generation counter makes every newer screen cancel any pending timed step, so a stale timer never overwrites the screen.

### Decisions made without a human

1. Highlights are cleared on CLOSED and CANCELLED, so a blinking bay never outlives the shopper. The intent agent
   can still call `highlight()` at any time.
2. The backend sends the total pre-formatted (`$12.96`) and the count as an integer. The firmware writes "item"
   or "items". That makes the screen currency-agnostic in the firmware.
3. A DECLINED screen returns to TOTAL after 4 s so the gate does not stay red while the shopper retries.
4. I changed `tests/test_serial.py::test_ready_resyncs_bay_leds`. READY now also resends `DISP,IDLE` and
   `HILITE,ALL,OFF`, so the test expects those lines after the LED lines.
5. I did not add GATE/SHELF changes for payment; that belongs to the payments agent (9.7).

## Morning checklist (needs the physical board, Windows)

1. Flash: in PowerShell from the repo root:
   ```
   cd firmware\shelf_esp32
   & "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -t upload --upload-port COM4
   ```
   (`pio` is not on PATH on this machine; use the full path above or add it. Use the COM port from
   `python -m backend.serial_bridge --list-ports`.)
2. Close anything holding COM4 (the backend), then run `& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" device monitor -p COM4 -b 115200`.
   - You should see `READY`. The LCD should light up in landscape with "SpeedMart" and the animated dots. **Check:** the
     picture is not shifted or wrapped (col offset 35), the text is not mirrored, and the colors are right (orange "Mart"
     and not blue; if red and blue are swapped, the panel needs BGR). If the image is upside down, change rotation `1` to `3`
     in `main.cpp`.
   - Type `PING`: you should get `PONG`.
3. Type each screen and check the look. Nothing should flicker when you send the same line twice:
   `DISP,WELCOME,Maya`, `DISP,TOTAL,$12.96,3`, `DISP,TOTAL,$8.64,1` (should read "1 item"), `DISP,PAID,$12.96,A1B2C3`,
   `DISP,DECLINED`, `DISP,OCCUPIED,Maya`, `DISP,WELCOME,Bartholomew Jones` (should shrink to fit), `DISP,IDLE`.
4. Highlight: `LED,1,ON` then `HILITE,1,ON`: bay 1 blinks about twice a second. `HILITE,1,OFF`: steady on again.
   `LED,1,OFF`, `HILITE,1,ON`: blinks. `HILITE,ALL,OFF`: stays off. Check that the other LEDs, the RGB breathing and
   the button (hold GPIO 14 for 1 s, which prints `BTN,0`) still work while the screen animates.
5. Check the backlight is on (GPIO 38). If the screen stays black but serial works, confirm GPIO 15 goes HIGH.
6. End to end with the backend: run `.venv\Scripts\python -m uvicorn backend.main:app`, admin demo-login, start a session.
   The LCD should show "Welcome, Demo", then "$0.00 / 0 items" after 3 s. Pick an item: the total updates in about 1 s.
   Admin reset: "SpeedMart" idle after 3 s. Unplug and replug USB: the screen and LEDs come back to the current state.
7. ~~When payments land: check the real payment event name and fields and fix the GUESS rows above.~~
   Done at integration. On the board, confirm a real approval shows `APPROVED` with the auth code and a
   decline shows `DECLINED` for 4 s before returning to the total.
