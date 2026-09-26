# Overnight agent notes (S4.3 + docs)

Unattended run, 2026-09-26. Branch `claude/speedmart-overnight-agent-ef0492`.
Files touched: `backend/agent.py`, `tests/test_agent.py`, `backend/store.py` (hook only), `README.md`,
`docs/DEMO.md`, `docs/DEVPOST.md`, this file.

## Task 1: S4.3 AI store agent

### How it is wired

- `store.compute_live_cart()` now returns `agent.on_cart(cart.compute_cart(...), member)`. Every snapshot
  (shelf change, override, state change, WebSocket connect, freeze) passes through it.
- `on_cart()` never blocks. It copies the last line generated for that session into `snapshot["agent_line"]`
  and, only if the cart content changed (session, items + qty, warnings, budget), schedules a new line.
- One daemon thread (`agent`) waits out the 1.2 s debounce. A newer cart pushes the deadline forward and
  replaces the pending cart (latest cart wins). If a cart changes while the LLM is phrasing, the slower,
  older result is dropped and the newer cart is generated instead.
- `generate()`: `policy()` (7.4) → `template()` (always) → LLM only if `features.llm` and a key is set
  → `validate()` → template on any failure. Never raises.
- Delivery: line stored per session, `{"type":"agent","data":{"line":...}}` published to the shopper and
  admin sockets, and an `agent_line` event logged (decision, source `llm`/`template`, line).
  LLM failures log `agent_llm_error`; rejected LLM output logs `agent_llm_rejected` with the raw text.

### Decisions made without a human (please review)

1. **store.py edit is two lines, not one.** The hook is one call wrapping `cart.compute_cart(...)` in
   `compute_live_cart`, plus `agent` added to the existing `from backend import ...` line. The import is
   required for the call to work. Nothing else in store.py changed.
2. **Hook placement:** `compute_live_cart` rather than each broadcast site, so the frozen checkout cart also
   carries the line and every snapshot gets `agent_line` from one place.
3. **Extra fields in decisions.** Spec shapes are kept and names are added so the template and the LLM have
   the words: `misplaced` + `name`; `over_budget` + `put_back_name`; `suggest` + `sku_name`, `cart_item`.
4. **"with tax" for suggestions** is computed exactly: `new_subtotal = subtotal + price`,
   `new_total = new_subtotal + tax(new_subtotal)`, suggest only if `new_total <= budget`.
5. **`over_budget.put_back`** = SKU in the cart with the highest unit price; ties go to catalog order.
6. **Money format in templates:** whole dollars drop the cents (`$4`, as in the spec's example line),
   others show two decimals (`$3.50`, `$1.06`).
7. **Validation is stricter than the minimum** (anything that fails falls back to the template, so this can
   only make the line safer):
   - one line after trimming whitespace and wrapping quotes; at most 25 words;
   - no emoji (Unicode "Symbol, other" category, emoji ranges, ZWJ / variation selectors) and no `#`;
   - "only catalog names": a capitalised word that is not at a sentence start must be a word from a catalog
     product name, the store name, the shopper's first name, or `I`/`OK`/`USD`. So "Gatorade" is rejected.
     Lower-case made-up products cannot be detected; the prompt forbids them.
   - every `$` amount must be one the LLM was given (catalog prices, cart totals, line totals, budget,
     decision amounts). The LLM cannot invent a price.
8. **Provider selection:** `LLM_PROVIDER=openai` needs both `OPENAI_API_KEY` and `OPENAI_MODEL`;
   anything else uses Anthropic and needs `ANTHROPIC_API_KEY`. No key → templates, no error, no log noise.
9. **Timeout** is `httpx` `timeout=2.5` (per connect/read phase, httpx semantics). It runs on the agent
   thread, never on a request thread or the event loop.
10. **Line storage is in memory**, one session at a time. After a backend restart the first recompute
    schedules a fresh line, so the phone gets one ~1.2 s later.
11. **System prompt** is the spec's text with "Aisle" replaced by "SpeedMart".

### Tests (`tests/test_agent.py`, 42 tests, no network)

- An autouse fixture replaces `httpx.post`. Any call a test didn't expect fails the test.
- Policy: empty, empty beats misplaced, misplaced, misplaced beats over_budget, over_budget (put back the
  most expensive), suggest, no suggest when the complement plus tax breaks the budget, no suggest when the
  complement is already in the cart, ok, the member's budget is respected.
- All five templates, rendered from real `compute_cart` snapshots.
- Validation rejects: multi-line, 26 words, emoji (two kinds), non-catalog brand, invented price, hashtag,
  empty, None, non-string. Accepts a good line (quotes stripped) and exactly 25 words.
- LLM: Anthropic request shape (URL, headers, `timeout=2.5`, `max_tokens=80`, system prompt, user JSON keys);
  OpenAI-compatible shape; invalid output → template; `ReadTimeout` → template; HTTP 529 and bad JSON shape →
  template; flag off / no key / OpenAI without a model → templates and zero HTTP calls.
- Debounce: waits before generating; burst of 3 carts → only the last is delivered; an unchanged cart is not
  regenerated; the stored line shows up on the next snapshot; a stale LLM result is dropped when the cart
  changed mid-call; `on_cart` returns immediately even when the LLM is slow.
- Integration: demo member enters, electrolytes leave the shelf, the phone gets the recovery drink suggestion
  and `store.current_cart()["agent_line"]` carries it.

### Acceptance (S4.3)

| Check | Result |
|---|---|
| `pytest -q` | PASS (104 passed) |
| Picking electrolytes shows a recovery drink suggestion within ~2 s | PASS in test (fake shelf, templates). Physical check needed, see below |
| Going over budget shows a put-back line | PASS in tests (policy + template). Physical check needed |
| With the API key removed, templates still appear | PASS in tests (no key, flag off). Physical check needed |

### Known limitations

- Other test files (e.g. `test_cart.py`) also run the agent thread. Nothing calls the network there unless a
  real `ANTHROPIC_API_KEY` is set in `.env` **and** `features.llm` is true. `conftest.py` is outside what this
  run was allowed to change. A one-line `os.environ["ANTHROPIC_API_KEY"] = ""` in `conftest.py` would
  close that gap.
- With a slow LLM the worst case is 1.2 s + 2.5 s ≈ 3.7 s before the line changes. The template-only path is
  about 1.2 s. The spec's "~2 s" target assumes a fast model (Haiku).

## Task 2: Docs

- README.md, docs/DEMO.md, docs/DEVPOST.md rewritten for SpeedMart (spec Sections 15 and 18, "Aisle" → "SpeedMart").
- The README documents only what exists in the repo today. Steps not built yet (passkeys, gates, payments,
  QR generation, reset script) are listed as planned and not described as working.
- `scripts/run_all.ps1` does not exist yet (CLAUDE.md asks for it) and `scripts/reset_demo.sh` is still a
  stub. Both are outside this run's files. The README gives Windows commands that work today: run
  `run_all.sh` from Git Bash, or start uvicorn and the worker in two PowerShell windows. Admin reset is done
  from the admin page or with `Invoke-RestMethod`.

## Morning checklist (a human must verify)

1. [ ] Review the two-line `backend/store.py` diff (hook + import) and approve it.
2. [ ] With a real `ANTHROPIC_API_KEY` in `.env` and `features.llm: true`: enter the store, pick
   electrolytes, and see a recovery drink suggestion on the phone within ~2 s. Check
   `data/events.log.jsonl` for `agent_line` with `"source": "llm"`.
3. [ ] Pick 2 electrolytes + 1 protein bar (budget $20): the phone says you're $1.06 over and suggests
   putting back the Electrolyte tabs.
4. [ ] Put a protein bar in bay 0: the misplaced line appears.
5. [ ] Blank `ANTHROPIC_API_KEY`, restart the backend: template lines still appear, no errors in the log.
6. [ ] Watch `agent_llm_rejected` events during testing. If good LLM lines are being rejected (e.g. a
   capitalised word we didn't expect), add the word to `_ALWAYS_OK` in `backend/agent.py`.
7. [ ] Decide whether to add `os.environ["ANTHROPIC_API_KEY"] = ""` to `tests/conftest.py` (see limitations).
8. [ ] Create `scripts/run_all.ps1` and a real `scripts/reset_demo.sh` (+ `.ps1`), then update the README
   "Run" and "Reset the demo" sections to use them.
9. [ ] Fill in team names and roles in README.md, and the demo video and repo links in docs/DEVPOST.md.
10. [ ] Read docs/DEVPOST.md "How we built it" and confirm the wording about Claude Code matches how the team
    wants to describe it.
