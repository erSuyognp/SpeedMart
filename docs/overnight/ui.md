# Overnight UI pass · SpeedMart phone app and admin dashboard

Branch: `claude/speedmart-mobile-design-system-2ab605` · Night of Fri 25 → Sat 26 Sep 2026 · Unattended run.

Files this run owns (nothing else in `web/` was touched):

| File | What |
|---|---|
| `web/css/app.css` | Design system layer. Loaded **after** `styles.css`, never edits it. |
| `web/assets/` | `logo.svg`, `icons.svg` sprite, PNG icons, `make_icons.py` |
| `web/manifest.webmanifest` | Installable app manifest |
| `web/admin.html`, `web/js/pages/admin.js` | Dark dashboard redesign (every id, endpoint and behaviour kept) |
| `web/design.html` | Living style guide, light and dark side by side |
| `docs/overnight/ui.md` | This file |

Quick look: start the backend, then open `http://localhost:8000/design.html` (every component, both themes, phone-frame screens) and `http://localhost:8000/admin.html`.

## Morning checklist

1. `git pull`, then `pytest -q` (62 passed at the end of the night, see Verification).
2. Start the backend and open `/design.html`. Use the Both / Light / Dark switch top right. Every example has a **Markup** disclosure you can copy from.
3. **Link the design system on each customer page** (`index`, `enter`, `store`, `exit`, `receipt`): paste the head lines below. `store.html` improves immediately with no body changes.
4. **New pages**: build from the “Screen examples” section. Keep the ids the page scripts expect; the class names are the contract with `app.css`.
5. `store.html` optional polish (5 min): swap the “Loading…” notice for the skeleton card; when gates are off, move the Checkout button into an `.action-bar` and add `class="has-action-bar"` to `<body>`.
6. **Real iPhone over the tunnel**: check the notch and home-indicator padding (header, action bar, toast, footer), Add to Home Screen (name SpeedMart, icon, opens standalone), dark mode, that the Face ID sheet does not fight the action bar.
7. **Android Chrome**: install prompt shows the maskable icon; status bar matches the page background.
8. Turn on **Reduce Motion** on a phone once: animations collapse, layout must stay identical.
9. **Team laptop**: open `/admin.html` at the resolution you will use during judging. Log in, Demo login, start a session from `/store.html`, tap `+`/`−` overrides, toggle Force decline, Reset. The event log is newest-first.
10. Merge note: this branch never touched `styles.css`, the customer pages or their scripts, so it should merge cleanly with the customer-pages branch. Resolve `admin.*` in favour of this branch if the other branch touched them.
11. Regenerate icons only if `logo.svg` changes: `.venv\Scripts\python.exe web\assets\make_icons.py`.

## Head lines every page needs (index, enter, store, exit, receipt)

Paste these into `<head>` in this order. Keep `styles.css` first; `app.css` must come after it.

```html
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f2f4f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0d12" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="SpeedMart">
<title>SpeedMart · Cart</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/assets/logo.svg" type="image/svg+xml">
<link rel="icon" href="/assets/icon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<link rel="stylesheet" href="/css/styles.css">
<link rel="stylesheet" href="/css/app.css">
```

Notes
- `store.html` currently has a single `<meta name="theme-color" content="#1a56db">`; replace it with the two `media=` lines above so the status bar matches the page background in both schemes.
- `viewport-fit=cover` is required for `env(safe-area-inset-*)` to be non-zero on iPhone. Without it the header and action bar still work, just without the notch/home-indicator padding.
- `apple-mobile-web-app-status-bar-style="default"` keeps a readable status bar. `black-translucent` lets the page extend under the status bar; the sticky header already pads for `--safe-t`, so it is safe to switch to it if the team wants the full-bleed look.
- Admin (`admin.html`) is always dark: it uses `<html data-theme="dark">` and a single `theme-color` of `#0b0d12`. Already applied.
- The manifest is served by FastAPI's `StaticFiles` as `application/manifest+json` (Python's `mimetypes` knows `.webmanifest`). Verified with `curl -I http://127.0.0.1:8765/manifest.webmanifest` on the dev laptop; re-check on the demo laptop, Windows reads MIME types from the registry.

Regenerate the PNG icons after editing `logo.svg` (mirror the geometry in the script):

```powershell
.venv\Scripts\python.exe web\assets\make_icons.py
```

## Class name reference

Everything lives in `web/css/app.css`. Section numbers refer to the comment banners in that file.

### Tokens (§1)

| Group | Variables |
|---|---|
| Surfaces | `--bg`, `--bg-2`, `--surface`, `--surface-2`, `--surface-glass` (translucent bars), `--card-border`, `--card-shadow` |
| Text | `--text`, `--text-2` (secondary, also `--muted`), `--text-3` (tertiary) |
| Lines | `--line`, `--line-strong` |
| Brand | `--brand` (fills), `--brand-strong` (pressed), `--brand-ink` (text/links), `--brand-soft` (tint), `--brand-text`, `--brand-shadow` |
| Semantic text on tints | `--ok`/`--ok-bg`, `--warn`/`--warn-bg`, `--bad`/`--bad-bg`, `--info`/`--info-bg` |
| Graphics | `--green`, `--amber`, `--red`, `--violet`, `--idle` (bars, dots, icons) |
| Misc colour | `--focus`, `--overlay`, `--toast-bg`, `--toast-text`, `--skeleton-a/-b`, `--shadow-1/2/3` |
| Spacing | `--space-1` 4 · `-2` 8 · `-3` 12 · `-4` 16 (= `--gap`) · `-5` 20 · `-6` 24 · `-8` 32 · `-10` 40 |
| Radius | `--radius-sm` 12 · `--radius` 18 · `--radius-lg` 22 · `--radius-xl` 28 · `--radius-pill` |
| Type | `--font-sans`, `--font-mono`, `--text-xs` 13 · `-sm` 15 · `-base` 17 · `-lg` 20 · `-xl` 24 · `-2xl` 28 · `-3xl` 34 · `-4xl` 42 |
| Sizes | `--tap` 48 · `--btn-lg` 56 · `--page-max` 560 |
| Safe areas | `--safe-t/r/b/l`, `--page-pt/pr/pb/pl` (main padding incl. insets), `--action-bar-space` |
| Motion | `--ease`, `--ease-out`, `--dur-fast` 150 · `--dur` 250 · `--dur-slow` 400 |

Theme switching: default light; dark via `prefers-color-scheme`; force with `data-theme="dark"` or `"light"` on `<html>` or any container.

### Layout and header (§3)

| Class | Use |
|---|---|
| `main` | Page column, 560 px max, safe-area padding, 16 px gap. `main.wide` for admin (1440 px). |
| `.app-header`, `.topbar` | Sticky translucent header, notch padding. Put `h1` (screen title) or `.brand` left and a `.pill`/`.session-tag` right. |
| `.brand` + `.brand-logo` | Logo lockup: `<a class="brand"><img class="brand-logo" src="/assets/logo.svg" alt="">SpeedMart</a>` |
| `.icon-btn` | 48 px round icon button (back, close). |
| `.action-bar` | Fixed bottom bar, blur, safe-area bottom padding. Direct child `button`s are full width and 56 px. `.action-bar .row` for two side-by-side buttons. `.action-note` for a small line under the button. |
| `body.has-action-bar` | Adds bottom padding so content and footer clear the bar (`body:has(> .action-bar)` also works in current browsers). |
| `.hero`, `.page-title`, `.subtitle`, `.display`, `.eyebrow` | Big type for landing/receipt. |

### Buttons and forms (§4)

| Class | Use |
|---|---|
| `button`, `.button` (anchor), `.btn` | 48 px, radius 14, press scale. |
| `.primary` · `.secondary` (tinted) · `.ghost` · `.danger` · `.danger.solid` / `.on` (red fill) · `.success` | Variants. |
| `.lg` (56 px) · `.sm` (40 px) · `.block` (full width) · `.pill-btn` | Sizes and shapes. |
| `.busy` | Hides label, shows spinner. JS: `btn.classList.add("busy"); btn.disabled = true`. |
| `input`, `select`, `textarea` | 52 px, 17 px text, focus ring. `.field` wraps `label` + control + `.help`; `.field-head` puts a `.value` right of the label. |
| `input[type=range]` | Budget slider. Set `style="--range-pct: 25%"` from JS for the filled track (`(v-min)/(max-min)*100`). |
| `button.toggle` + `aria-pressed` / `.on` | Switch look (force decline). |
| `.stepper` → `button`, `.count`, `button` | Quantity stepper (admin overrides). |

### Cards, lists, disclosure (§5)

`.card` (`> h2` title, `.card-head` for title + pill, `.inset`, `.brand`, `.tight`, `.flat`) · `.list` / `.list-item` (grouped rows, `.value` right) · `.kv` (`dt`/`dd` grid; `.kv.right`, `.kv.rows`) · `details.disclosure` / `details.card` (chevron summary) · `pre.json`, `.code`.

### Cart, totals, budget (§6)

| Class | Use |
|---|---|
| `.cart-list`, `.receipt-list` | `<ul>` reset. |
| `.cart-row` → `.name`, `.qty`, `.line` (or `.price`) | Exactly what `store.js` renders. New rows animate in; add `.leaving` then remove after 400 ms. |
| `.cart-row.rich` → adds `.thumb` and `.sub` | Two-line row with a 44 px thumb. |
| `.empty-cart` | Empty state line. |
| `.totals` → label/value pairs, `.grand` on both cells of the total row | Subtotal, tax, total with a rule above the total. |
| `.budget` → `.budget-label`, `.bar > span` | Bar colours: default green, `.bar.amber` (80 to 100 %), `.bar.red` (over). `.bar.lg` thicker. |

### Messages and states (§7)

| Class | Use |
|---|---|
| `.agent-card` (existing) / `.agent-msg` (+ `.who` label, `p`) | Agent bubble with avatar. `.fading` for the 150 ms swap. |
| `.strip` · `.warn` · `.bad` · `.ok` | Inline alert with CSS icon. Bare text or `<p>` children. |
| `#toast.toast` · `.hide` · `.ok` · `.bad` | Created by `api.toast`. Sits above the action bar automatically. |
| `.state` (+ `.ok` `.warn` `.bad`), `.state-icon`, `.error-state`, `.notice` (existing) | Centered icon/title/text screens. `.spinner`, `.spinner.lg` for verifying. |
| `.success` → `.checkmark` svg (`.checkmark-circle`, `.checkmark-check`), `.title`, `.amount`, `.meta`, `.points` | Paid screen with drawn checkmark. Replay by re-inserting the svg. |
| `.skeleton` (+ `.title` `.text` `.text.short/.long` `.row` `.circle` `.button`), `.skeleton-rows` | Loading placeholders with shimmer. |

### Sheet, pill, chips, footer (§8, §9)

`.sheet-backdrop` + `.sheet` (toggle `.open` on both; `.sheet-handle`, `.sheet .actions`) · `.pill` (`.live` `.ok` `.warn` `.bad`/`.off` `.brand`), `.session-tag` (existing, same look) · `.dot` (`.live` pulses, `.warn`, `.bad`) · `.badge` / `.chip` (`.ok` `.warn` `.bad` `.brand`) · `.icon` (`.lg` 32, `.xl` 44) with `<use href="/assets/icons.svg#name">` · `.hint` · `.site-footer` (amber dot + uppercase “Sandbox demo”).

Icons in the sprite: `bolt bag door exit scan face-id check check-circle x x-circle alert info arrow-left chevron-down chevron-right receipt refresh clock user shield-check sparkles wifi-off plus minus card`.

### Utilities (§11)

`.row` (`.between`, `.nowrap`, `> .grow`) · `.stack` (`.tight`, `.loose`) · `.center` · `.muted`/`.text-2` · `.text-3` · `.small` · `.mono` · `.tabular` · `.money` · `.mt-1/2/3/4/6` · `.mb-0` · `.divider` · `.visually-hidden` · `.fade-up` / `.fade-in` (+ `.delay-1/2/3`).

### Motion (§12)

Rows: `row-in` + `row-flash` on insert, `.leaving` transition on remove. Buttons: `:active` scale 0.97. Checkmark: `draw` → `fill-in` → `pop`. Live dot: `ping`. Skeleton: `shimmer`. Agent: opacity/translate on `.fading`. All collapse under `prefers-reduced-motion: reduce`.

### Admin only (§10, scoped to `body.admin`)

`.health` → `.stat` (+ `.ok` `.warn` `.bad`) with `.k` label and `.v` value · `.dash` grid (`.span-2`, `.tall`) · `.session-hero` (`.who`, `.sub`) · `.actions-grid` (`.full`) · `.bays` → `.bay` (+ `.stable` `.motion` `.unstable` `.nodata` `.empty`; `.bay-head`, `.bay-id`, `.bay-count`, `.bay-tags`) · `.cart-state` · `.override-row` (`.meta` → `.name`, `.ov`, `.ov.active`) · `.leds` chips · `.log` → `div` rows with `.t`, `.ty` (+ `.admin` `.cart` `.gate` `.pay` `.bad` `.ws`), `.d`; `.log.empty` · `.login-card`.

## Screen examples

Structure only; wire ids to whatever the page script expects. `<body>` gets `class="has-action-bar"` on screens with a bottom bar. Every page ends with the sandbox footer. All five are rendered live (light and dark) in `/design.html` → Screens.

### index.html · JOIN

```html
<body class="has-action-bar">
  <main>
    <header class="app-header">
      <a class="brand" href="/"><img class="brand-logo" src="/assets/logo.svg" alt="">SpeedMart</a>
      <span class="pill brand">Sandbox</span>
    </header>

    <section class="hero">
      <img class="brand-logo" src="/assets/logo.svg" alt="">
      <h1 class="page-title">SpeedMart</h1>
      <p class="subtitle">Grab and go, with a yes you control.</p>
    </section>

    <form id="signup" class="card stack">
      <div class="field">
        <label for="name">First name</label>
        <input id="name" type="text" autocomplete="given-name" placeholder="Maya" required>
      </div>
      <div class="field">
        <div class="field-head"><label for="budget">Budget</label><span id="budget-value" class="value">$20</span></div>
        <input id="budget" type="range" min="10" max="50" step="5" value="20" style="--range-pct: 25%">
        <p class="help">Your store agent can never approve more than this.</p>
      </div>
      <div class="field">
        <label for="dietary">Dietary</label>
        <select id="dietary"><option value="">None</option><option>Vegetarian</option><option>Vegan</option><option>Gluten free</option></select>
      </div>
    </form>

    <!-- success state, hidden until signup completes -->
    <section id="joined" class="card state ok" hidden>
      <div class="state-icon"><svg class="icon"><use href="/assets/icons.svg#card"/></svg></div>
      <h2>You're a member.</h2>
      <p>Card linked: Visa •••• 4242 (test). Walk to the entry gate.</p>
    </section>

    <p class="hint">Already a member? <a id="signin" href="#">Sign in with Face ID</a></p>
  </main>

  <div class="action-bar">
    <button id="join-btn" class="primary" type="submit" form="signup">
      <svg class="icon"><use href="/assets/icons.svg#face-id"/></svg>Join with Face ID
    </button>
    <p class="action-note">A Visa test card is linked automatically</p>
  </div>
  <footer class="site-footer">Sandbox demo · no real payments</footer>
</body>
```

Passkeys off → button text “Join”. Slider JS: `budgetValue.textContent = "$" + el.value; el.style.setProperty("--range-pct", ((el.value-10)/40*100) + "%")`.

### enter.html · ENTER

```html
<body class="has-action-bar">
  <main>
    <header class="app-header">
      <a class="brand" href="/"><img class="brand-logo" src="/assets/logo.svg" alt="">SpeedMart</a>
      <span class="pill">Entry gate</span>
    </header>

    <!-- one .state card per state; show one at a time -->
    <section id="s-idle" class="card state">
      <div class="state-icon"><svg class="icon"><use href="/assets/icons.svg#door"/></svg></div>
      <h2>Entry gate</h2>
      <p>Confirm it's you and the door opens. One shopper at a time.</p>
    </section>
    <section id="s-verifying" class="card state" hidden>
      <div class="state-icon"><span class="spinner lg"></span></div>
      <h2>Verifying…</h2><p>Use Face ID when your phone asks.</p>
    </section>
    <section id="s-welcome" class="card state ok" hidden>
      <div class="state-icon"><svg class="icon"><use href="/assets/icons.svg#check"/></svg></div>
      <h2>Welcome in, <span id="welcome-name">Maya</span></h2>
      <p>The door is open. Your cart is live on this phone.</p>
      <div class="stack mt-4"><a class="button primary" href="/store.html">Open my cart</a></div>
    </section>
    <section id="s-occupied" class="card state warn" hidden>
      <div class="state-icon"><svg class="icon"><use href="/assets/icons.svg#user"/></svg></div>
      <h2>Someone is inside</h2><p>The store takes one shopper at a time. Try again in a moment.</p>
    </section>
    <section id="s-error" class="card error-state" hidden>
      <div class="state-icon"><svg class="icon"><use href="/assets/icons.svg#x-circle"/></svg></div>
      <h2 id="error-title">Something went wrong</h2><p id="error-text"></p>
      <div class="stack mt-4"><button id="retry-btn" class="primary">Try again</button></div>
    </section>
  </main>

  <div class="action-bar">
    <button id="enter-btn" class="primary"><svg class="icon"><use href="/assets/icons.svg#face-id"/></svg>Enter with Face ID</button>
    <p class="action-note">Not a member? <a href="/">Join first</a></p>
  </div>
  <footer class="site-footer">Sandbox demo · no real payments</footer>
</body>
```

### store.html · LIVE CART (already matches; nothing to restructure)

```html
<main>
  <header class="topbar">
    <h1>SpeedMart</h1>
    <div class="session-tag"><span id="session-id" class="mono">#3xY9</span><span id="live-dot" class="dot live"></span></div>
  </header>

  <!-- optional: replace the "Loading…" notice with this skeleton card -->
  <section id="loading" class="card">
    <div class="skeleton title"></div>
    <div class="skeleton-rows"><div class="skeleton row"></div><div class="skeleton row"></div></div>
  </section>

  <section id="agent" class="card agent-card" aria-live="polite">Electrolytes added. A recovery drink is $4 and keeps you under $20.</section>
  <section id="warnings" class="strip warn" aria-live="polite" hidden></section>
  <section id="checkout-state" class="strip warn" hidden></section>

  <section class="card">
    <h2>Your cart</h2>
    <ul id="cart" class="cart-list">
      <li class="cart-row" data-sku="elx"><span class="name">Electrolyte tabs</span><span class="qty">× 1</span><span class="line">$8.00</span></li>
      <li class="cart-row" data-sku="rec"><span class="name">Recovery drink</span><span class="qty">× 1</span><span class="line">$4.00</span></li>
    </ul>
    <p id="cart-empty" class="empty-cart" hidden>Nothing yet. Pick something off the shelf.</p>
  </section>

  <section class="card">
    <div class="totals">
      <span class="muted">Subtotal</span><span id="subtotal">$12.00</span>
      <span class="muted">Tax</span><span id="tax">$0.96</span>
      <span class="grand">Total</span><span id="total" class="grand">$12.96</span>
    </div>
    <div class="budget">
      <div class="budget-label"><span>Budget</span><span id="budget-text">$12.96 of $20.00</span></div>
      <div id="budget-bar" class="bar" role="progressbar" aria-label="Budget used"><span style="width:64.8%"></span></div>
    </div>
  </section>

  <p id="exit-hint" class="hint">When you're done, scan the EXIT code.</p>
</main>
<footer class="site-footer">Sandbox demo · no real payments</footer>
```

Gates off: move `#checkout-btn` into `<div class="action-bar">` after `main` and add `has-action-bar` to `body`.

### exit.html · APPROVE (over-budget variant shown)

```html
<body class="has-action-bar">
  <main>
    <header class="app-header">
      <a class="brand" href="/"><img class="brand-logo" src="/assets/logo.svg" alt="">SpeedMart</a>
      <span class="pill warn"><span class="dot warn"></span>Exit gate</span>
    </header>

    <section class="card">
      <h2>Your receipt</h2>
      <ul id="items" class="cart-list receipt-list">
        <li class="cart-row"><span class="name">Electrolyte tabs</span><span class="qty">× 2</span><span class="line">$16.00</span></li>
        <li class="cart-row"><span class="name">Recovery drink</span><span class="qty">× 1</span><span class="line">$4.00</span></li>
      </ul>
      <div class="totals mt-3">
        <span class="muted">Subtotal</span><span id="subtotal">$20.00</span>
        <span class="muted">Tax</span><span id="tax">$1.60</span>
        <span class="grand">Total</span><span id="total" class="grand">$21.60</span>
      </div>
      <div class="budget">
        <div class="budget-label"><span>Budget</span><span id="budget-text">$21.60 of $20.00</span></div>
        <div id="budget-bar" class="bar red" role="progressbar"><span style="width:100%"></span></div>
      </div>
    </section>

    <div id="over-scope" class="strip bad">This is over your $20 limit. Put something back to continue.</div>
    <div id="declined" class="strip bad" hidden>Declined by the sandbox card. Nothing was charged. Try again.</div>

    <details class="card disclosure">
      <summary>What the payment network sees</summary>
      <dl id="instruction" class="kv">
        <dt>Agent</dt><dd>SpeedMart Store Agent</dd>
        <dt>Merchant</dt><dd>SpeedMart #01</dd>
        <dt>Max amount</dt><dd>$20.00 · single use</dd>
        <dt>Expires</dt><dd>02:29</dd>
        <dt>Intent</dt><dd>Pay $21.60 to SpeedMart #01 for 3 items</dd>
      </dl>
      <p class="small text-3">SANDBOX: modeled on Visa Intelligent Commerce concepts. Not a Visa API call.</p>
    </details>
  </main>

  <div class="action-bar">
    <button id="approve-btn" class="primary" disabled>Approve $21.60 with Face ID</button>
    <button id="cancel-btn" class="ghost">Keep shopping</button>
  </div>
  <footer class="site-footer">Sandbox demo · no real payments</footer>
</body>
```

Within budget: `#budget-bar` without `.red`, `#over-scope` hidden, approve enabled with the live total. While charging: `approve-btn.classList.add("busy")`. Declined: show `#declined`, approve says “Try again”.

### receipt.html · PAID

```html
<body class="has-action-bar">
  <main>
    <header class="app-header">
      <a class="brand" href="/"><img class="brand-logo" src="/assets/logo.svg" alt="">SpeedMart</a>
      <span class="pill live"><span class="dot live"></span>Paid</span>
    </header>

    <section class="card success">
      <svg class="checkmark" viewBox="0 0 64 64" aria-hidden="true">
        <circle class="checkmark-circle" cx="32" cy="32" r="29"/>
        <path class="checkmark-check" d="M19 33.5l8.5 8.5L45 24"/>
      </svg>
      <p class="title">Paid</p>
      <p id="amount" class="amount">$8.64</p>
      <p id="meta" class="meta">Visa •••• 4242 (test) · auth A1B2C3 · 02:14</p>
      <span id="points" class="points">+8 points · 23 total</span>
    </section>

    <section class="card">
      <h2>Items</h2>
      <ul id="items" class="cart-list receipt-list">
        <li class="cart-row"><span class="name">Electrolyte tabs</span><span class="qty">× 1</span><span class="line">$8.00</span></li>
      </ul>
      <div class="totals mt-3">
        <span class="muted">Subtotal</span><span>$8.00</span>
        <span class="muted">Tax</span><span>$0.64</span>
        <span class="grand">Total</span><span class="grand">$8.64</span>
      </div>
    </section>

    <details class="card disclosure">
      <summary>Show payment record</summary>
      <pre id="payment-json" class="json">{ "payment_id": "pay_91Kd", … }</pre>
    </details>
  </main>

  <div class="action-bar"><a id="done-btn" class="button primary" href="/">Done</a></div>
  <footer class="site-footer">Sandbox demo · no real payments</footer>
</body>
```

Loyalty off → drop `#points`.

## Admin panel notes

- `admin.html` keeps every id (`live-dot`, `login`, `password`, `panel`, `b-vision`, `b-serial`, `b-stripe`, `b-llm`, `b-lock`, `session`, `demo-login`, `force-exit`, `reset`, `force-decline`, `cart`, `overrides`, `bays`, `loose`, `leds`, `log`, `state-json`) and every endpoint call (`/admin/login`, `/admin/state`, `/api/health`, `/admin/override`, `/admin/led`, `/admin/reset`, `/admin/force-exit`, `/admin/demo-login`, `/admin/force-decline`, `/api/store/current`, `/ws?role=admin`). New ids added for display only: `live-pill`, `live-label`, `session-hero`.
- Health tiles are `.stat` elements; `setBadge(id, text, kind)` writes the value and the kind (`ok`/`warn`/`bad`). Vision age is shown in ms under 10 s, then seconds, then minutes, with “· stale” beyond 2 s.
- Override rows are steppers: `−` / count in cart / `+`. Same `/admin/override` calls with `delta ±1`; disabled unless the session is `IN_STORE` (hint shown).
- Force decline is a `button.toggle` with `aria-pressed`; `.on` still marks the active state.
- Event log rows are `time · type · payload`, colour-coded by type family (admin, cart/shelf, gate/session, payment, ws, failures in red). Newest first, capped at 50 like before.
- Layout: 1 column under 820 px, 2 columns to 1180 px, 3 columns above (log takes the wider third column and two rows).

## Decisions

1. **Layering, not replacing.** `app.css` redefines the variables and selectors that `styles.css` already uses (`--bg`, `--brand`, `.card`, `.cart-row`, `.bar`, `button.primary`, `.toast`, …) so every page improves the moment the link is added, before any HTML changes. Same specificity, later in the cascade, so it wins without `!important`.
2. **Brand colour stays `#1a56db`.** It is already in `styles.css` and in the `theme-color` meta of `store.html`, so nothing clashes before the morning edits. Dark mode uses a lighter fill (`#3d7bf0`) for buttons and a lighter ink (`#7fa6f8`) for links so contrast holds on near-black.
3. **Dark tokens are written twice** (once under `prefers-color-scheme: dark`, once under `[data-theme="dark"]`). `light-dark()` would remove the duplication but still fails on older Android Chrome; a judge's phone must not get an unstyled page. Keep the two blocks identical when editing.
4. **`data-theme` works on any element**, not just `<html>`. That is how `design.html` shows light and dark side by side, and how `admin.html` forces dark (`<html data-theme="dark">`).
5. **Safe areas via tokens.** `--safe-t/r/b/l` wrap `env(safe-area-inset-*)`; `main`, the sticky header, the bottom action bar, the sheet, the toast and the footer all consume them. Pages must declare `viewport-fit=cover` for the values to be non-zero.
6. **Sticky app header, fixed action bar.** The existing `.topbar` becomes a sticky translucent bar (negative margins bleed it to the edges of `main`). `.action-bar` is `position: fixed` with a blur backdrop; `body.has-action-bar` (or `body:has(> .action-bar)`) adds bottom padding so content never hides behind it.
7. **Motion is opt-out safe.** Row slide-in and highlight, row fade-out and collapse, button press scale, checkmark draw, live-dot ping and skeleton shimmer all collapse to ~0 ms under `prefers-reduced-motion: reduce`.
8. **Cart row animation has no fill mode** on purpose: a `forwards` fill would pin `opacity: 1` and break the `.leaving` fade that `store.js` relies on.
9. **Vivid graphics tokens vs. semantic text tokens.** `--green/--amber/--red` are for bars, dots and icons; `--ok/--warn/--bad` (+ `-bg`) are for text on tinted strips, where the darker/lighter shades keep contrast.
10. **Logo is a bag, not a lock.** First draft read as a padlock; the body was widened and the handle shrunk so it reads as a shopping bag with speed lines and a bolt (instant cart). Original geometry, mirrored exactly between `logo.svg` and `make_icons.py`.
11. **Manifest `theme_color` = page background (`#f2f4f8`)**, not brand blue. The header is translucent light, so a blue title bar on Android would look bolted on. Pages set `theme-color` per colour scheme with `media=` so the status bar blends in dark mode too.
12. **Minimum sizes.** 17 px body (`html { font-size: 17px }`), 48 px touch targets on every `button`, 52 px inputs (17 px text stops iOS zooming into fields), 56 px primary action in the bottom bar.
13. **Admin stays one page, same script shape.** `admin.js` keeps its functions (`renderState`, `renderCart`, `renderOverrides`, `renderShelf`, `renderLog`, `act`, `refresh`, `pollHealth`, socket wiring) and only changes what markup they emit. No new endpoints, no new features; the count inside each stepper is the cart quantity already in the state payload.
14. **Icons are a same-origin SVG sprite**, referenced with `<use href="/assets/icons.svg#name">`. No icon font, no dependency; colour follows `currentColor`.
15. **Sample data in the guide uses SpeedMart naming** (“SpeedMart #01”, “SpeedMart Store Agent”) even where spec 8.6 still says Aisle, matching the repo's rename. Numbers are the spec's: $8.00 + $4.00 → $12.96 at 8 % tax; over-budget example is 2 × $8.00 + $4.00 → $21.60 vs $20; receipt is $8.64, auth A1B2C3, +8 points.
16. **Firefox-only range rules are expected to be dropped by Chromium** (`::-moz-range-*`); the stylesheet parses to 370 rules in Chrome with exactly those 3 missing. Not a bug.

## Verification

Checks run at the end of the night (Windows, `.venv` Python 3.11):

| Check | Result |
|---|---|
| `pytest -q` | 62 passed (see the final line in the terminal recap) |
| `app.css` parses in Chromium | 370 rules, no dropped selectors other than the 3 `::-moz-range-*` rules |
| `/manifest.webmanifest` served | `application/manifest+json` |
| `store.html` + `app.css` injected, 375×812, light and dark | Header, cart rows, totals, budget bar and footer render as designed with the current markup |
| `admin.html` at 1366×900 | Login card, tiles, session hero, bays, cart, overrides, LED chips, log, raw JSON all render; override `+`/`−`, force-decline toggle and log streaming exercised in the browser |
| `design.html` | 23 examples rendered in 35 frames + 10 phones, no console errors from the page |

Needs a human with hardware:
- iPhone (Safari, over the HTTPS tunnel): safe-area padding, Add to Home Screen icon and standalone launch, Face ID sheet over the action bar, dark mode, reduced motion.
- Android Chrome: install banner icon (maskable) and theme colour.
- Team laptop at demo resolution: admin dashboard legibility from where the operator sits.
