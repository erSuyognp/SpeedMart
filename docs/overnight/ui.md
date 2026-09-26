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

## Morning checklist

_(filled in as tasks complete; see the bottom of this file for the final list)_

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
- The manifest is served by FastAPI's `StaticFiles` as `application/manifest+json` (Python's `mimetypes` knows `.webmanifest`). Verified with `curl -I http://127.0.0.1:8000/manifest.webmanifest`.

Regenerate the PNG icons after editing `logo.svg` (mirror the geometry in the script):

```powershell
.venv\Scripts\python.exe web\assets\make_icons.py
```

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
