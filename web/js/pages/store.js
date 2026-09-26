// Page script for store.html: live cart while shopping (11.3).
// Renders only CartSnapshots from the backend. It never adds up prices or quantities itself.

(function () {
  const $ = (id) => document.getElementById(id);
  const ACTIVE = ["IN_STORE", "CHECKOUT_PENDING"];
  let gates = true;
  let current = null; // last CartSnapshot rendered

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

  function renderOutside(title, text) {
    current = null;
    $("session-id").textContent = "";
    $("outside-title").textContent = title;
    $("outside-text").textContent = text;
    $("start-wrap").hidden = gates; // gates-off fallback only
    show("outside");
  }

  function renderNoSession() {
    renderOutside("You're not in the store",
      gates ? "Scan the ENTRY code at the door to start shopping." : "Tap Start shopping to begin.");
  }

  function renderEnded(state) {
    const text = {
      PAID: "Paid. Thanks for shopping!",
      CLOSED: "Nothing to pay, see you soon.",
      CANCELLED: "Your session was ended by staff.",
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

  function renderRows(items) {
    const list = $("cart");
    const existing = new Map();
    list.querySelectorAll(".cart-row:not(.leaving)").forEach((li) => existing.set(li.dataset.sku, li));

    items.forEach((item) => {
      let li = existing.get(item.sku);
      if (!li) {
        li = document.createElement("li");
        li.className = "cart-row";
        li.dataset.sku = item.sku;
        li.innerHTML = '<span class="name"></span><span class="qty"></span><span class="line"></span>';
      }
      li.querySelector(".name").textContent = item.name;
      li.querySelector(".qty").textContent = "× " + item.qty;
      li.querySelector(".line").textContent = api.money(item.line_total_usd);
      list.appendChild(li); // keeps catalog order; moving an existing node does not replay the animation
      existing.delete(item.sku);
    });

    existing.forEach((li) => {
      li.classList.add("leaving");
      setTimeout(() => li.remove(), 400);
    });

    $("cart-empty").hidden = items.length > 0;
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
    if (!ACTIVE.includes(snap.state)) { renderEnded(snap.state); return; }
    current = snap;
    show("shopping");
    $("session-id").textContent = "#" + snap.session_id.slice(-4);

    renderAgent(snap.agent_line);

    const warnings = snap.warnings || [];
    $("warnings").hidden = warnings.length === 0;
    $("warnings").innerHTML = "";
    warnings.forEach((w) => {
      const p = document.createElement("p");
      p.textContent = w.message + ". Please return it to its lit slot.";
      $("warnings").appendChild(p);
    });

    renderRows(snap.items);
    $("subtotal").textContent = api.money(snap.subtotal_usd);
    $("tax").textContent = api.money(snap.tax_usd);
    $("total").textContent = api.money(snap.total_usd);
    renderBudget(snap);

    const pending = snap.state === "CHECKOUT_PENDING";
    $("checkout-state").hidden = !pending;
    $("checkout-state").textContent = gates
      ? "Checking out. Finish at the exit gate."
      : "Checkout started. Your cart is frozen.";
    $("exit-hint").hidden = !gates || pending;
    $("checkout-btn").hidden = gates || pending;
  }

  async function resync(res) {
    try { gates = !!(await api.config()).features.gates; } catch (e) { /* keep the last known value */ }
    renderCurrent(res);
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
    } else if (msg.type === "gate" && msg.data.event === "cancelled") {
      api.toast("Your session was ended by staff.");
    }
  }

  async function init() {
    $("start-btn").addEventListener("click", (e) => action(e.currentTarget, "/api/dev/start"));
    $("checkout-btn").addEventListener("click", (e) => action(e.currentTarget, "/api/dev/checkout"));
    try {
      const cfg = await api.config();
      gates = !!cfg.features.gates;
      renderCurrent(await api.get("/api/store/current"));
    } catch (e) {
      renderOutside("Can't reach the store", e.message);
    }
    connectSocket({ onMessage, onStatus: setLive, onResync: resync });
  }

  init();
})();
