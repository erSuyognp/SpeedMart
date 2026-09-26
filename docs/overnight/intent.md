# F18 "Tell the store what you need": overnight notes

Branch: `claude/intent-f18-overnight-160024`. Built unattended; nothing outside the five owned files was edited.

| File | What it is |
|---|---|
| `backend/intent.py` | APIRouter: `POST /api/intent`, `GET /api/intent/current`, `DELETE /api/intent`; LLM planner + strict validator + rules fallback; shelf highlights; `compare_cart_to_plan()` |
| `web/intent.html` + `web/js/pages/intent.js` | Mobile page: text box, 3 example chips, mic (Web Speech API only where supported), plan cards, total vs budget, "Your items are blinking on the shelf", Start over / Go to my cart, loading + error states, "Sandbox demo" footer |
| `tests/test_intent.py` | 33 tests, own FastAPI test app (router + SessionMiddleware + a test-only login route) |

Result: `pytest -q` → **95 passed** (33 new, 62 existing), 0 failed.

---

## Morning checklist

1. **Approve the contract change.** CLAUDE.md says Section 8 of the spec is the contract and must not change without asking. F18 adds three routes and the Plan object below, which are **not in Section 8 yet**. Decide whether to accept them, then add them to Section 8 (see "Proposed Section 8 addition").
2. **Merge the branch** and apply the lines in "Integration" below (main.py is the only one required for the page to work).
3. **Run the tests** (PowerShell, from the repo root):
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   ```
4. **Try it on the laptop** (uvicorn as usual, then):
   - Open `http://localhost:8000/admin.html`, log in, tap demo-login.
   - Open `http://localhost:8000/intent.html`, tap "Post run recovery under $15".
   - Expect: Electrolyte tabs + Recovery drink, est. total $12.96, budget $15.00, "Planned by the store AI…" (or "…by keyword match" if the LLM is off/slow).
5. **Physical checks (human only):**
   - **Phone mic:** on Android Chrome the mic button appears; tap it, say "something to drink", and the plan should appear when you stop talking. On iPhone Safari the mic works only on iOS 14.5+ and needs HTTPS (the tunnel). If it's missing, the button hides itself; that's expected.
   - **Blinking bays:** needs the other agent's `serial_bridge.highlight(bay, on)` / `clear_highlights()` plus firmware support. With them merged and `hardware_leds: true`, the planned bays should blink after a plan is made, and stop on "Start over" and when the session ends (PAID / CLOSED / CANCELLED). If the helpers are missing, nothing blinks and nothing breaks.
   - **LLM latency:** watch `data\events.log.jsonl` for `intent_plan` lines. `"source": "llm"` means the AI planned it; `"fallback_reason"` says why rules were used instead (`llm_off`, `llm_error:ReadTimeout`, `invalid:over_budget`, ...).
     ```powershell
     Get-Content data\events.log.jsonl -Tail 20 | Select-String intent_plan
     ```
6. **Verified overnight in a browser** (phone viewport, 375 px, throwaway server serving only this router + `web/`): signed-out state, form, loading spinner, a plan made by the **real LLM from `.env`** (xAI grok via the OpenAI-compatible path, passed validation, budget override $15 applied), Start over, empty-input error, and a no-match request ("caviar and champagne" → empty plan, no blink note). The mic button appeared in Chromium; speech itself was **not** tested (no microphone).

---

## Integration

Every line other files need. None of these were applied tonight.

### Required

`backend/main.py`: register the router **before** the static mount (the mount must stay last):
```python
from backend import admin, db, eventlog, intent, routes_api, serial_bridge, shelf_state, store, ws  # add intent
...
app.include_router(intent.router)   # next to the other include_router lines
```
Importing `backend.intent` also subscribes it to eventlog (for "session ended → clear highlights"). Nothing to call in `lifespan`.

### For the blinking bays (serial_bridge agent)

`backend/intent.py` calls these only if they exist (`getattr`), only when `features.hardware_leds` is true, and swallows any exception (logged as `intent_highlight_error`):
```python
serial_bridge.highlight(bay: int, on: bool) -> None   # called as highlight(bay_id, True) for each planned bay
serial_bridge.clear_highlights() -> None              # called before new highlights, on DELETE, on session end
```
Highlights are skipped while a **different** member is in the store, so a plan made outside never blinks bays at the current shopper.

### Exit screen: cart vs plan (routes_api / payments agent + exit.js)

In the exit quote route (`POST /api/gate/exit/quote`, and `POST /api/dev/checkout` for gates-off), after the cart is frozen:
```python
from backend import intent
plan = intent.current_plan(member_id)
plan_check = intent.compare_cart_to_plan(snapshot, plan) if plan else None
return {"cart": snapshot, "instruction": instruction, "plan_check": plan_check}   # plan_check is new → Section 8
```
`web/js/pages/exit.js`, above the receipt preview:
```js
if (data.plan_check) {
  const el = document.createElement("section");
  el.className = "card agent-card";
  el.textContent = data.plan_check.summary;   // "You asked for run recovery under $15: you have both items, $12.96."
  receiptEl.before(el);                        // receiptEl = whatever element holds the receipt preview
}
```
`compare_cart_to_plan(cart_snapshot, plan)` returns `{"matches": bool, "missing": [{"sku","name","qty"}], "extra": [{"sku","name","qty"}], "summary": str}`. `qty` is the shortfall (missing) or surplus (extra). It is pure and reads only `items[].sku/name/qty` and `total_usd` from the CartSnapshot.

### Entry points to the page (optional, pick one)

- `web/store.html`, above the cart card: `<a class="button block" href="/intent.html">Tell the store what you need</a>`
- or `web/js/pages/enter.js`: after a successful entry, redirect to `/intent.html` instead of `/store.html` (the page's "Go to my cart" button leads to `store.html`).

### Docs (optional)

- `docs/DEMO.md` step 3.5: "Type or say 'post run recovery under $15'. The AI builds a plan from the shelf and the bays blink."
- `docs/DEVPOST.md` → "What it does": one sentence on the intent planner (LLM proposes, the store validates against stock and budget).
- Judge Q&A: "What does the AI do?" → "It turns what you say into a plan from our real catalog. Every plan is checked: real SKUs, in-stock quantities, and your budget with tax. If the AI is wrong or slow, a keyword planner answers instead."

### No changes needed

`.env.example` (reuses `LLM_PROVIDER`, `OPENAI_*`, `ANTHROPIC_*`), `requirements.txt` (only `httpx`, already listed), `config.json`, `catalog.json`, `store.py`, `agent.py`.

---

## Proposed Section 8 addition (needs human approval)

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| POST | `/api/intent` | `{"text":str}` (≤ 300 chars) | `Plan` | Logged-in member. 401 `not_logged_in`, 400 `empty_text` / `text_too_long`, 404 `unknown_member` |
| GET | `/api/intent/current` | — | `Plan` or `null` | 401 if not logged in |
| DELETE | `/api/intent` | — | `{"ok":true}` | Clears the plan and the highlights |

```json
{
  "plan_id": "pln_Ab3xY9...",
  "goal_summary": "run recovery",
  "items": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1, "unit_price_usd": 8.00, "reason": "Replaces salts lost on your run"}],
  "est_total_usd": 12.96,
  "budget_usd": 15.00,
  "fits_budget": true,
  "bays": [0, 1],
  "source": "llm",
  "created_at": "2026-09-26T02:14:03Z"
}
```

---

## Decisions made overnight

1. **The `llm` feature flag gates the AI planner too.** With `features.llm: false` (or no key/model for the chosen provider) the LLM is never called and the rules planner answers. `source: "rules"`, `fallback_reason: "llm_off"`.
2. **Timeout 3 s** (as asked for F18), not the 2.5 s the spec uses for the agent line. It is httpx's per-phase timeout, so the worst case is a little over 3 s. The page shows a spinner meanwhile.
3. **`LLM_PROVIDER` is cleaned before use** (`split("#")[0].strip().lower()`), because the real `.env` has an inline comment on that line.
4. **Budget = the lowest of:** the member's budget, any dollar amount in the text ("under $15", "$9", "10 bucks", "budget of 7.50"), and the LLM's `budget_override_usd`. The override can only **lower** the budget, never raise it. The text is parsed independently, so "under $10" holds even if the LLM ignores it. A non-numeric override (string, bool) makes the reply invalid.
5. **Strict validation → any failure falls back to rules for the whole plan** (no partial repair): not JSON, not an object, missing/empty `goal_summary` or more than 12 words, empty `items`, unknown SKU, duplicate SKU, `qty` not an int in `1..units in catalog.json`, missing reason or more than 12 words, total with tax over budget.
6. **An empty LLM `items` list is invalid** (falls back to rules). **An empty rules plan is allowed**: when nothing matches or nothing fits the budget, the plan has no items, `est_total_usd: 0`, `bays: []`. The page says "Nothing on our shelf matches that yet". This is never an invalid plan.
7. **Rules planner:** words in the text are matched loosely (plurals, "recovering" ~ "recovery") against SKU name words and tags, plus a small synonym list per tag (`recovery`: run, workout, gym, hydration…; `drink`: thirsty, beverage…; `snack`: hungry, protein…). Tag hits score 2, name hits 1. SKUs whose `complements:<sku>` tag points at a matched SKU get added ("Pairs well with Electrolyte tabs"). "for two" / "couple" / "pair" asks for qty 2, capped by stock. Items are added greedily, highest score first, only while the total with tax fits the budget. The dietary tag is sent to the LLM but ignored by rules, since no catalog SKU carries dietary tags.
8. **Plans live in memory only**, one per member, and are lost on a backend restart (as asked). A member's old plan is dropped when they start a **new** store session (`session_state` event with `from: null, to: IN_STORE`), so last visit's plan never shows at this visit's exit. It is **kept** when the session ends, so the exit/receipt screen can still compare.
9. **Highlights** clear on DELETE and on `session_state` → PAID / CLOSED / CANCELLED, not on CHECKOUT_PENDING (the shopper may cancel checkout and keep shopping).
10. **Money:** all sums in integer cents with `backend.cart.to_cents` / `tax_cents` (same rounding as the cart). `est_total_usd` includes tax, like the CartSnapshot `total_usd`, so plan and cart compare like for like.
11. **`compare_cart_to_plan` wording:** "You asked for {goal} under ${budget}: {status}, ${cart total}." Status is one of: "you have it" / "you have both items" / "you have all N items" / "you still need X" / "you have everything, plus X" / "you're missing X and also picked Y" / "you haven't picked anything up yet". Adds ", over your $15 budget" when the cart total is over. Leaves out "under $X" if the goal already contains a "$". With no plan, `matches: false` and "No shopping plan for this visit."
12. **Errors use the house shape** `{"error","message"}` but come back as `JSONResponse`s built inside the router, so the router works on its own test app without main.py's exception handlers.
13. **Page styles:** a small `<style>` block in `intent.html` for chips and plan cards, since `styles.css` is not mine to edit. It uses only the existing CSS variables, so dark mode works. It links `/css/app.css` as asked (404 until that file exists; harmless).
14. **"Start over"** uses a plain `fetch(..., {method: "DELETE"})`, because `web/js/api.js` has no DELETE helper and I may not edit it.
15. **Tests switch flags** by swapping `intent.settings` for a `dataclasses.replace` copy (settings are frozen), the same pattern `tests/test_serial.py` uses. The LLM is mocked by monkeypatching `httpx.post` as seen from `backend.intent`.
