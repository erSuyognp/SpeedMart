# Toggle layout fix and the demo bank balance (spec 8.15)

Built 26 Sep 2026 in one unattended session. Section 8 changes were pre-approved and are documented in the
spec (8.1, 8.3, 8.4 and the new 8.15), plus Sections 4, 5.2, 6 and 11.

## Part 1: toggle switches

`button.toggle` in `web/css/app.css` drew the track and knob as two absolutely positioned pseudo-elements and
relied on `padding-left: 66px` to keep the label clear. The review queue's `.toggle.small` override
(`padding: 0 10px`) removed that padding, so the knob sat on top of "Sound: on" ("S…on"). The Force decline
toggle only looked right because nothing overrode its padding.

Fix: the switch is now **one** pseudo-element (track with the knob drawn by a radial gradient) laid out as a
flex item beside the label with a 12 px gap. It has a fixed size (42 × 26, `.small` 34 × 20), can never overlap
the text, long labels wrap next to it (`white-space: normal`, `overflow-wrap: anywhere`), and padding
overrides cannot push the label under it. On/off colours use the theme tokens (`--line-strong` / `--red`,
label `--bad` when on), so both states read in light and dark; at 360 px the admin grid's `.toggle.full` wraps
its label instead of clipping. Ids, `aria-pressed`, `.on` and every handler are unchanged.
`web/design.html` → Forms shows off, on, `.small`, a long wrapping label and the admin grid at 328 px.

## Part 2: demo bank

| Piece | Where |
|---|---|
| Ledger + balances + hooks | `backend/bank.py` (new), `bank_ledger` table in `backend/db.py` |
| Config | `config.json` `demo_bank` `{opening_balance_usd: 50, top_up_usd: 20, max_top_ups: 3}`, validated in `backend/settings.py`, defaults when absent |
| Charge / refund rows | `payments.record` (AUTHORIZED → `charge`, "SpeedMart #01 · 2 items") and `payments.record_refund` (SUCCEEDED → `refund`, "Refund · 1 Hydration drink"): returns, disputes, AI auto refunds and staff refunds all land here |
| Insufficient funds | `routes_api.gate_exit_approve`: 402 `insufficient_funds` before Face ID is used up, after `over_scope`; gate event `declined`; logged `bank_insufficient_funds` |
| Routes | `GET /api/bank`, `POST /api/bank/topup` (409 `topup_limit`), `POST /admin/bank/reset`; `/admin/state.bank`; receipt `bank.balance_after_usd` |
| Socket | `{"type":"bank","data":BankSummary}` to the member + admins on every ledger change; a signed-in member's socket gets it among its first messages |
| Backfill | `bank.backfill()` in the app lifespan after `seed_demo_member()`; `open_account()` at signup; the ledger also self-heals (any read for a member without an `opening` row creates it) |
| UI | `web/js/bank.js`: full card on `index.html` (available large, current smaller, transactions with pending badges and green refunds, "Add $20 demo funds", tweened + bumped number on change, reduced motion respected); compact header line on `store.html`; exit page shows the available balance and handles the decline; receipt shows "Balance after this purchase"; admin "Reset demo balances" + the shopper's balance in the session panel |
| Tests | `tests/test_bank.py`: ledger math (current vs available, pending hold → charge + release), charge and refund rows through the real flows, insufficient funds (402, nothing charged, Face ID kept, retry after a top up), holds counted, exact balance ok, top up limit, admin reset, backfill (function + lifespan), signup, receipt line, WebSocket push (member + admin), disabled flags, config validation |
| e2e | `scripts/e2e_sim.py`: balance before → charge posted → refund posted → receipt line → top up |

### Decisions taken without asking

- **Holds.** Nothing in the codebase pre-authorizes (charges are immediate PaymentIntents), so no hold is ever
  written. The ledger and the math still carry `hold` / `hold_release`: a pending hold with a payment's id is
  posted and released when that charge lands, and pending holds reduce the available balance (tested).
- **Insufficient funds is a 402 refusal, not a DECLINED payment row.** Like `over_scope` it happens before the
  fresh verification is consumed, so the shopper can add funds and approve again with the same Face ID.
  The gate screen still gets `declined`.
- **The check uses the available balance** (current minus pending holds); a hold cannot be double spent.
- **Refund descriptions** say "(reported problem)" for dispute / AI / staff refunds, matching the receipt.
- **The demo card label on the bank card is "Demo Visa •••• 4242"** as asked; the members table keeps the
  longer honesty label for the card line.
- **Top up limit counts `top_up` rows**, so "Reset demo balances" (which rewrites the ledger) also gives the
  three top ups back.
- **No new feature flag.** The bank is core to the demo card story and never blocks a disabled feature
  (tested with gates, passkeys, stripe, loyalty, disputes and llm off).

## Morning checklist

- [ ] `pytest -q` (see the commit message for the count) with `vision.mode` / `features.yolo` as committed.
      The working tree had `yolo: true` + `mode: "yolo"` uncommitted; in that mode the tag based tests fail
      (empty baseline) regardless of this change, so run the suite on the committed config.
- [ ] Phone, light and dark: `/` after sign in shows the bank card at the top; "Add $20 demo funds" bumps the
      number; the third top up disables the button.
- [ ] Shop and pay: the store header line drops by the charge live; the receipt shows "Balance after this
      purchase"; a return raises it live (green row).
- [ ] Insufficient funds: admin → "Reset demo balances", then top nothing up and buy more than $50 (or lower
      `demo_bank.opening_balance_usd` in `config.json` and restart) → the exit page shows the friendly decline
      with the "Add demo funds" link; approve again after a top up.
- [ ] Admin at 360 px wide: "Force decline" and "Sound" toggles show the label beside the switch; the review
      queue toggle no longer reads "S…on".
- [ ] `design.html` → Forms → "Toggle states" and the "Demo bank" section render in both themes.
