// Glow: the flowing four colour light from app.css section 13, for checkout, the voice agent and shelf changes.
// Pages call it from state changes and socket messages they already handle. It only adds and removes classes
// (plus --glow-level while the agent speaks): it never changes what a page shows, never calls the backend, and
// never delays anything a page does, except the short approval burst before the receipt opens.
//
//   const edge = Glow.screen({ variant: "kiosk" | "thin", root });  // one edge glow per page (or design frame)
//   edge.set("listening");    // off | connecting | listening | speaking | verifying | processing
//   edge.follow(() => conversation.getOutputVolume());  // while speaking, the glow follows the agent's audio
//   edge.flash("add" | "remove" | "error");            // one edge flash: green, amber, red
//   edge.collapse();          // approved: the edge glow draws in while the burst takes over
//   await Glow.burst({ root });                         // green burst + check; resolves when the page may move on
//   Glow.once(el);            // .glow--once: one sweep, then fade (replays when it is already running)
//   Glow.flash(el, "error" | "warn");  Glow.shake(el);  Glow.pulse(el, "add" | "remove");
//   Glow.play(el, "glow-sheen", 1000);                  // any one-shot class, restarted if it is on already
//   Glow.countTo(el, 12.34, api.money, animate);        // the shown number runs to the backend's value
//   Glow.diff(prevItems, nextItems);                    // {added: [sku], removed: [sku]} by quantity
//   const fresh = Glow.combine(500);  fresh();          // true for the first change of a burst of changes

(function () {
  const reduced = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const timers = new WeakMap(); // el -> {class name: timer}

  // Adds cls for ms, restarting its animation when the element already has it.
  function play(el, cls, ms) {
    if (!el) return;
    let t = timers.get(el);
    if (!t) { t = {}; timers.set(el, t); }
    clearTimeout(t[cls]);
    el.classList.remove(cls);
    void el.offsetWidth; // flush styles so the class starts its animation from 0 again
    el.classList.add(cls);
    t[cls] = setTimeout(() => el.classList.remove(cls), ms);
  }

  const once = (el, ms) => play(el, "glow--once", ms || 1500);
  const flash = (el, kind) => play(el, kind === "warn" ? "flash-warn" : "flash-error", 850);
  const shake = (el) => play(el, "shake", 480);
  const pulse = (el, kind) => (kind === "remove" ? play(el, "pulse-remove", 750) : play(el, "pulse-add", 2500));

  // --- the screen edge glow ---

  const STATES = {
    off: [],
    connecting: ["is-on", "screen-glow--faint", "glow--slow"],
    listening: ["is-on", "screen-glow--breathe", "glow--slow"],
    speaking: ["is-on", "screen-glow--speaking"],
    verifying: ["is-on", "screen-glow--breathe", "glow--slow"],
    processing: ["is-on", "screen-glow--bright", "glow--fast"],
  };
  const STATE_CLASSES = ["is-on", "is-collapsing", "has-level", "screen-glow--faint", "screen-glow--breathe",
    "screen-glow--speaking", "screen-glow--bright", "glow--slow", "glow--fast"];
  const FLASHES = { add: "pulse-add", remove: "pulse-remove", error: "flash-error" };
  const LEVEL_GAIN = 2.5; // the SDK's output volume is the mean of its voice band (0..1); speech sits near 0.1-0.4

  function screen(opts) {
    const o = opts || {};
    const root = o.root || document.body;
    let el = root.querySelector(":scope > .screen-glow");
    if (!el) {
      el = document.createElement("div");
      el.className = "screen-glow" + (o.variant ? " screen-glow--" + o.variant : "");
      el.setAttribute("aria-hidden", "true");
      el.hidden = true;
      root.appendChild(el);
    }
    let state = "off";
    let flashing = false;
    let hideTimer = null;
    let flashTimer = null;
    let getLevel = null;
    let raf = 0;
    let level = 0.5;
    let shown = "";

    function show() {
      clearTimeout(hideTimer);
      if (el.hidden) {
        el.hidden = false;
        void el.offsetWidth; // start the fade in from opacity 0
      }
    }

    // Idle: display:none once the 400 ms fade is over, so nothing keeps animating.
    function hideLater() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => { if (state === "off" && !flashing) el.hidden = true; }, 450);
    }

    function stopLevel() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      el.classList.remove("has-level");
    }

    function tick() {
      raf = 0;
      if (state !== "speaking" || !getLevel) return;
      let v = null;
      try { v = getLevel(); } catch (e) { v = null; }
      if (typeof v === "number" && isFinite(v)) {
        level += (Math.max(0, Math.min(1, v * LEVEL_GAIN)) - level) * 0.35;
        const text = level.toFixed(2);
        if (text !== shown) { shown = text; el.style.setProperty("--glow-level", text); }
        el.classList.add("has-level");
      } else {
        el.classList.remove("has-level"); // no level from the SDK: the steady pulse
      }
      raf = requestAnimationFrame(tick);
    }

    function startLevel() {
      if (!raf && getLevel && state === "speaking" && !reduced()) raf = requestAnimationFrame(tick);
    }

    function set(next) {
      if (!STATES[next]) next = "off";
      if (next === state) return;
      state = next;
      stopLevel();
      el.classList.remove(...STATE_CLASSES);
      if (next === "off") { hideLater(); return; }
      show();
      el.classList.add(...STATES[next]);
      startLevel();
    }

    function follow(fn) {
      getLevel = fn;
      startLevel();
    }

    function flashEdge(kind) {
      const cls = FLASHES[kind] || FLASHES.add;
      show();
      flashing = true;
      el.classList.remove(...Object.values(FLASHES));
      void el.offsetWidth; // replay when the same flash is still running
      el.classList.add(cls);
      clearTimeout(flashTimer);
      flashTimer = setTimeout(() => {
        el.classList.remove(cls);
        flashing = false;
        if (state === "off") hideLater();
      }, 850);
    }

    function collapse() {
      stopLevel();
      show();
      el.classList.add("is-collapsing");
    }

    return { el, set, follow, flash: flashEdge, collapse, get state() { return state; } };
  }

  // --- the approval burst ---

  const CHECK = '<svg class="checkmark" viewBox="0 0 64 64" aria-hidden="true">' +
    '<circle class="checkmark-circle" cx="32" cy="32" r="29"/><path class="checkmark-check" d="M19 33.5l8.5 8.5L45 24"/></svg>';

  // Resolves once the check has drawn: the moment the exit page moves on to the receipt.
  function burst(opts) {
    const root = (opts && opts.root) || document.body;
    endBurst(root);
    const el = document.createElement("div");
    el.className = "burst-success";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = CHECK;
    root.appendChild(el);
    return new Promise((resolve) => setTimeout(resolve, reduced() ? 450 : 1150));
  }

  function endBurst(root) {
    const old = (root || document.body).querySelector(":scope > .burst-success");
    if (old) old.remove();
  }

  // --- numbers and cart changes ---

  const counting = new WeakMap(); // el -> {shown, raf}

  // Shows value through format(). animate: count from the number on screen (0.45 s); otherwise set it at once.
  // The last frame is always format(value) exactly, so the page ends on the backend's own figure.
  function countTo(el, value, format, animate) {
    if (!el) return;
    const target = Number(value);
    const run = counting.get(el);
    if (run) cancelAnimationFrame(run.raf);
    const from = run ? run.shown : parseFloat(String(el.textContent).replace(/[^0-9.-]/g, ""));
    counting.delete(el);
    if (!animate || reduced() || !isFinite(from) || !isFinite(target) || from === target) {
      el.textContent = format(value);
      return;
    }
    const t0 = performance.now();
    const state = { shown: from, raf: 0 };
    counting.set(el, state);
    const step = (now) => {
      const p = Math.min(1, (now - t0) / 450);
      state.shown = from + (target - from) * (1 - Math.pow(1 - p, 3));
      if (p < 1) {
        el.textContent = format(state.shown);
        state.raf = requestAnimationFrame(step);
      } else {
        el.textContent = format(value);
        counting.delete(el);
      }
    };
    state.raf = requestAnimationFrame(step);
  }

  // Which SKUs went up or down between two snapshots' items (a SKU that left the cart counts as removed).
  function diff(prevItems, nextItems) {
    const before = new Map((prevItems || []).map((i) => [i.sku, Number(i.qty) || 0]));
    const out = { added: [], removed: [] };
    (nextItems || []).forEach((i) => {
      const was = before.get(i.sku) || 0;
      const now = Number(i.qty) || 0;
      if (now > was) out.added.push(i.sku);
      else if (now < was) out.removed.push(i.sku);
      before.delete(i.sku);
    });
    before.forEach((qty, sku) => { if (qty > 0) out.removed.push(sku); });
    return out;
  }

  // Changes that arrive within ms of the previous one belong to one burst: fresh() is true only for the first,
  // so the screen flash and the total's glow play once while every row still shows its own change.
  function combine(ms) {
    let until = 0;
    return function fresh() {
      const now = Date.now();
      const first = now >= until;
      until = now + (ms || 500);
      return first;
    };
  }

  // Decoration must never stop a page (a payment, a cart, a conversation): any failure in here is logged and
  // swallowed. countTo still shows the value, burst still resolves, diff reports no change.
  function safe(fn, fallback) {
    return function () {
      try { return fn.apply(null, arguments); } catch (e) {
        console.warn("glow", e);
        return typeof fallback === "function" ? fallback.apply(null, arguments) : fallback;
      }
    };
  }
  const NOOP_EDGE = { el: null, set() {}, follow() {}, flash() {}, collapse() {}, state: "off" };

  function safeScreen(opts) {
    const edge = safe(screen, NOOP_EDGE)(opts);
    if (edge === NOOP_EDGE) return edge;
    ["set", "follow", "flash", "collapse"].forEach((name) => { edge[name] = safe(edge[name]); });
    return edge;
  }

  window.Glow = {
    screen: safeScreen,
    burst: safe(burst, () => Promise.resolve()),
    endBurst: safe(endBurst),
    once: safe(once),
    flash: safe(flash),
    shake: safe(shake),
    pulse: safe(pulse),
    play: safe(play),
    countTo: safe(countTo, (el, value, format) => { if (el) el.textContent = format(value); }),
    diff: safe(diff, () => ({ added: [], removed: [] })),
    combine,
    reduced,
  };
})();
