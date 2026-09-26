// Page script for store.html: live cart while shopping (11.3).
// Renders only CartSnapshots from the backend. It never adds up prices or quantities itself.

(function () {
  const $ = (id) => document.getElementById(id);
  const ACTIVE = ["IN_STORE", "CHECKOUT_PENDING"];
  let gates = true;
  let disputes = false; // F20: "Not mine?" on every cart row
  let current = null; // last CartSnapshot rendered
  let paidSession = null; // session id once PAID, so CLOSED afterwards still says "Paid"
  let shelf = null; // compact ShelfMap; glows while this shopper has a plan
  let bank = null; // demo bank (8.15): the compact balance line in the header
  let edge = null; // thin screen edge glow: one flash per shelf change (js/glow.js)
  const fresh = Glow.combine(500); // changes within 500 ms of each other play as one
  const recent = new Set(); // SKUs whose row already lit up in the current burst of changes

  function show(which) {
    $("loading").hidden = which !== "loading";
    $("outside").hidden = which !== "outside";
    $("shopping").hidden = which !== "shopping";
  }

  function setLive(live) {
    const dot = $("live-dot");
    dot.classList.toggle("live", live);
    dot.title = live ? "Live" : "Reconnecting…";
    dot.setAttribute("aria-label", dot.title);
  }

  function renderOutside(title, text, receiptFor) {
    current = null;
    $("session-id").textContent = "";
    $("outside-title").textContent = title;
    $("outside-text").textContent = text;
    $("start-wrap").hidden = gates || !!receiptFor; // gates-off fallback only
    $("receipt-link").hidden = !receiptFor;
    if (receiptFor) $("receipt-link").href = "/receipt.html?s=" + encodeURIComponent(receiptFor);
    show("outside");
  }

  function renderNoSession() {
    renderOutside("You're not in the store",
      gates ? "Scan the ENTRY code at the door to start shopping." : "Tap Start shopping to begin.");
  }

  function renderEnded(state, sessionId) {
    if (state === "PAID") paidSession = sessionId || paidSession;
    if (paidSession && (state === "PAID" || (state === "CLOSED" && sessionId === paidSession))) {
      renderOutside("Session ended", "Paid. Thanks for shopping!", paidSession);
      return;
    }
    const text = {
      PAID: "Paid. Thanks for shopping!",
      CLOSED: "Nothing to pay, see you soon.",
      CANCELLED: "Your session was ended by staff.",
      RETURNING: "A return is in progress. Finish it on your receipt.",
    }[state] || "Your session has ended.";
    renderOutside("Session ended", text);
  }

  function renderAgent(line) {
    const el = $("agent");
    if (!line) { el.hidden = true; el.textContent = ""; return; }
    if (el.textContent === line && !el.hidden) return;
    el.hidden = false;
    el.classList.add("fading");
    setTimeout(() => { el.textContent = line; el.classList.remove("fading"); }, 150);
  }

  // Returns the rows by SKU. putBack: a row leaving the cart pulses red before it slides out.
  function renderRows(items, putBack) {
    const list = $("cart");
    const existing = new Map();
    const rows = new Map();
    const keep = new Set(items.map((item) => item.sku));
    let placed = null; // the row just put in place
    list.querySelectorAll(".cart-row:not(.leaving)").forEach((li) => existing.set(li.dataset.sku, li));

    items.forEach((item) => {
      let li = existing.get(item.sku);
      if (!li) {
        li = document.createElement("li");
        li.className = "cart-row";
        li.dataset.sku = item.sku;
        li.innerHTML = '<span class="name"></span><span class="qty"></span><span class="line"></span>';
        if (disputes) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "ghost dispute-btn";
          btn.textContent = "Not mine?";
          btn.addEventListener("click", () => Dispute.open({
            sku: li.dataset.sku, name: li.querySelector(".name").textContent,
            onChange: (res) => { if (res.cart) render(res.cart); },
          }));
          li.classList.add("disputable");
          li.appendChild(btn);
        }
      }
      li.querySelector(".name").textContent = item.name;
      li.querySelector(".qty").textContent = "× " + item.qty;
      li.querySelector(".line").textContent = api.money(item.line_total_usd);
      // Keeps catalog order. Only a row that is out of place moves: moving a node replays its slide-in and
      // highlight, so re-appending every row would flash the whole cart on every snapshot.
      let at = placed ? placed.nextElementSibling : list.firstElementChild;
      while (at && (at.classList.contains("leaving") || !keep.has(at.dataset.sku))) at = at.nextElementSibling;
      if (li !== at) list.insertBefore(li, at);
      placed = li;
      rows.set(item.sku, li);
      existing.delete(item.sku);
    });

    existing.forEach((li) => {
      li.classList.add("leaving");
      if (putBack) li.classList.add("pulse-remove");
      setTimeout(() => li.remove(), putBack ? 750 : 400);
    });

    $("cart-empty").hidden = items.length > 0;
    return rows;
  }

  // An item taken or put back (this snapshot against the last one): the row sweeps and settles green, or pulses
  // red; the total glows and the budget bar shines; the screen edge flashes green (added) or amber (removed).
  // Several changes within 500 ms play as one: a single edge flash and total glow, each row once.
  function showChange(change, rows) {
    if (fresh()) {
      recent.clear();
      edge.flash(change.added.length ? "add" : "remove");
      Glow.once($("total"), 1500);
      Glow.play($("budget-bar"), "glow-sheen", 1000);
    }
    change.added.forEach((sku) => {
      const li = rows.get(sku);
      if (!li || recent.has(sku)) return;
      recent.add(sku);
      Glow.once(li, 1500);
      Glow.pulse(li, "add");
    });
    change.removed.forEach((sku) => {
      const li = rows.get(sku); // still in the cart with fewer units; a row that left pulses in renderRows
      if (li) Glow.pulse(li, "remove");
    });
  }

  function renderBudget(snap) {
    const budget = Number(snap.budget_usd) || 0;
    const ratio = budget > 0 ? snap.total_usd / budget : 0;
    const bar = $("budget-bar");
    bar.querySelector("span").style.width = Math.min(100, ratio * 100) + "%";
    bar.classList.toggle("amber", !snap.over_budget && ratio >= 0.8);
    bar.classList.toggle("red", !!snap.over_budget);
    bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    $("budget-text").textContent = api.money(snap.total_usd) + " of " + api.money(budget);
  }

  function render(snap) {
    if (!snap) { renderNoSession(); return; }
    if (!ACTIVE.includes(snap.state)) { renderEnded(snap.state, snap.session_id); return; }
    if (!current) Guardrails.mount($("guardrails"), { open: false }); // entering: this member's limits
    const prev = current;
    current = snap;
    show("shopping");
    $("session-id").textContent = "#" + snap.session_id.slice(-4);

    renderAgent(snap.agent_line);

    const warnings = snap.warnings || [];
    $("warnings").hidden = warnings.length === 0;
    $("warnings").innerHTML = "";
    warnings.forEach((w) => {
      const p = document.createElement("p");
      const card = shelf && shelf.cardForSku(w.sku);
      p.textContent = w.message + ". Please return it to " + (card ? "bay " + card : "its own bay") + ".";
      $("warnings").appendChild(p);
    });

    const change = prev && prev.session_id === snap.session_id ? Glow.diff(prev.items, snap.items) : null;
    const changed = !!change && change.added.length + change.removed.length > 0;
    const rows = renderRows(snap.items, changed);
    if (shelf) shelf.refresh(); // a cart change is a shelf change: update the unit counts
    $("subtotal").textContent = api.money(snap.subtotal_usd);
    $("tax").textContent = api.money(snap.tax_usd);
    Glow.countTo($("total"), snap.total_usd, api.money, changed); // counts up or down to the snapshot's total
    renderBudget(snap);
    if (changed) showChange(change, rows);

    const pending = snap.state === "CHECKOUT_PENDING";
    $("checkout-state").hidden = !pending;
    $("checkout-state").textContent = gates
      ? "Checking out. Finish at the exit gate."
      : "Checkout started. Your cart is frozen.";
    $("exit-hint").hidden = !gates || pending;
    $("checkout-btn").hidden = gates;
    $("checkout-btn").textContent = pending ? "Finish checkout" : "Checkout";
  }

  async function resync(res) {
    try {
      const cfg = await api.config();
      gates = !!cfg.features.gates;
      disputes = Dispute.enabled(cfg);
    } catch (e) { /* keep the last known value */ }
    renderCurrent(res);
    loadPlan();
    bank.load();
  }

  // Glow the bays of this shopper's own plan. plan_bays on the socket is only the cue to re-read it.
  async function loadPlan() {
    let bays = [];
    try {
      const plan = await api.get("/api/intent/current");
      if (plan && plan.items.length) bays = plan.bays;
    } catch (e) { /* signed out or offline: nothing glows */ }
    shelf.setGlow(bays);
    $("find-note").textContent = ShelfMap.lookFor(bays);
    $("find-note").hidden = bays.length === 0;
  }

  function renderCurrent(res) {
    if (res && res.session) render(res.cart);
    else if (current) renderEnded(null);
    else renderNoSession();
  }

  async function action(button, path) {
    button.disabled = true;
    try {
      const res = await api.post(path);
      if (res.cart) render(res.cart);
    } catch (e) {
      api.toast(e.message);
    } finally {
      button.disabled = false;
    }
  }

  function onMessage(msg) {
    if (msg.type === "cart") {
      render(msg.data);
    } else if (msg.type === "agent" && current) {
      renderAgent(msg.data.line); // agent lines arrive in S4.3
    } else if (msg.type === "gate" && msg.data.event === "paid" && current) {
      paidSession = current.session_id;
    } else if (msg.type === "gate" && msg.data.event === "cancelled") {
      api.toast("Your session was ended by staff.");
    } else if (msg.type === "plan_bays") {
      loadPlan();
    } else if (msg.type === "bank") {
      bank.update(msg.data); // the balance animates when a charge, refund or top up lands
    }
  }

  async function init() {
    $("start-btn").addEventListener("click", (e) => action(e.currentTarget, "/api/dev/start"));
    // Gates off: the exit page does the quote (/api/dev/checkout), approval and receipt.
    $("checkout-btn").addEventListener("click", () => { location.href = "/exit.html"; });
    shelf = ShelfMap.create($("shelf-map"), { size: "compact" });
    bank = Bank.mount($("bank-mini"), { compact: true });
    edge = Glow.screen({ variant: "thin" });
    try {
      const cfg = await api.config();
      gates = !!cfg.features.gates;
      disputes = Dispute.enabled(cfg);
      renderCurrent(await api.get("/api/store/current"));
      bank.load();
    } catch (e) {
      renderOutside("Can't reach the store", e.message);
    }
    connectSocket({ onMessage, onStatus: setLive, onResync: resync });
  }

  init();
})();
