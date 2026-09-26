// Page script for admin.html: team-only control panel (11.6).
// Everything shown comes from GET /admin/state, /api/health and admin WebSocket messages.
// Same ids, endpoints and behaviour as before; only the rendered markup changed for the dashboard look.

(function () {
  const $ = (id) => document.getElementById(id);
  const VISION_MAX_AGE_MS = 2000;
  const LED_COMMANDS = ["LED,0,OFF", "LED,0,ON", "SHELF,GREEN", "SHELF,RED", "SHELF,IDLE", "GATE,OPEN", "GATE,CLOSED", "GATE,IDLE", "PING"];
  const STRIPE_LABELS = { test: "Test mode", no_key: "No key", off: "Off" };
  let state = null;
  let events = [];
  let refreshTimer = null;
  let socketStarted = false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Health tile: <span id class="stat kind"><span class="k">Label</span><span class="v">value</span></span>
  function setBadge(id, text, kind) {
    const tile = $(id);
    tile.className = "stat" + (kind ? " " + kind : "");
    let v = tile.querySelector(".v");
    if (!v) {
      v = el("span", "v");
      tile.append(v);
    }
    v.textContent = text;
  }

  function formatAge(ms) {
    if (ms < 10000) return ms + " ms";
    if (ms < 120000) return Math.round(ms / 1000) + " s";
    return Math.round(ms / 60000) + " min";
  }

  function renderHealth(h) {
    if (h.vision_age_ms < 0) setBadge("b-vision", "No snapshot", "bad");
    else setBadge("b-vision", formatAge(h.vision_age_ms) + (h.vision_age_ms > VISION_MAX_AGE_MS ? " · stale" : ""), h.vision_age_ms > VISION_MAX_AGE_MS ? "bad" : "ok");
    setBadge("b-serial", h.serial ? "Connected" : "Off", h.serial ? "ok" : "bad");
  }

  function kv(pairs) {
    const frag = document.createDocumentFragment();
    pairs.forEach(([k, v]) => {
      frag.append(el("dt", null, k), el("dd", null, v));
    });
    return frag;
  }

  function ago(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return s + " s ago";
    const m = Math.floor(s / 60);
    return m + " min " + (s % 60) + " s ago";
  }

  function statePill(sessionState) {
    const kind = { IN_STORE: "live", CHECKOUT_PENDING: "warn", PAID: "ok", CANCELLED: "bad", CLOSED: "" }[sessionState];
    return el("span", "pill" + (kind ? " " + kind : ""), sessionState ? sessionState.toLowerCase().replace("_", " ") : "idle");
  }

  function renderSessionHero(s) {
    const hero = $("session-hero");
    hero.replaceChildren();
    const text = el("div");
    if (s.session) {
      text.append(el("div", "who", s.member ? s.member.name : "Unknown shopper"));
      text.append(el("div", "sub mono", s.session.id + " · started " + ago(s.session.started_at)));
    } else {
      text.append(el("div", "who", "No shopper"));
      text.append(el("div", "sub", "Store is free. Demo login, then start a session from the cart page."));
    }
    hero.append(text, statePill(s.session ? s.session.state : null));
  }

  function renderState(s) {
    state = s;
    renderHealth(s.health);
    setBadge("b-stripe", STRIPE_LABELS[s.health.stripe] || s.health.stripe, s.health.stripe === "test" ? "ok" : "warn");
    setBadge("b-llm", s.health.llm ? "On" : "Templates only", s.health.llm ? "ok" : "warn");
    setBadge("b-lock", s.lock.occupied ? "Occupied" : "Free", s.lock.occupied ? "warn" : "ok");

    renderSessionHero(s);
    const sess = s.session;
    const baseline = s.baseline ? Object.entries(s.baseline).map(([k, v]) => k + " " + v).join(", ") : "–";
    $("session").replaceChildren(kv([
      ["Session", sess ? sess.id : "none"],
      ["State", sess ? sess.state : "–"],
      ["Shopper", s.member ? s.member.name : "–"],
      ["Started", sess ? sess.started_at : "–"],
      ["Baseline", baseline],
      ["Sockets", "admin " + s.sockets.admin + " · member " + s.sockets.member + " · other " + s.sockets.anonymous],
    ]));

    const fd = $("force-decline");
    fd.textContent = "Force decline: " + (s.force_decline ? "ON" : "off");
    fd.classList.toggle("on", s.force_decline);
    fd.setAttribute("aria-pressed", String(s.force_decline));

    renderCart(s.cart);
    renderOverrides(s);
    renderShelf(s.shelf);
    events = s.events.slice();
    renderLog();
    $("state-json").textContent = JSON.stringify(s, null, 2);
  }

  function renderCart(cart) {
    const box = $("cart");
    box.replaceChildren();
    if (!cart) {
      box.className = "stack";
      box.append(el("p", "empty-cart", "No shopper in the store."));
      return;
    }
    box.className = "stack";

    const head = el("div", "cart-state");
    head.append(el("span", "muted small", cart.items.length + (cart.items.length === 1 ? " item" : " items")), statePill(cart.state));
    box.append(head);

    if (cart.items.length === 0) {
      box.append(el("p", "empty-cart", "Cart is empty."));
    } else {
      const list = el("ul", "cart-list");
      cart.items.forEach((i) => {
        const row = el("li", "cart-row");
        row.dataset.sku = i.sku;
        row.append(el("span", "name", i.name), el("span", "qty", "× " + i.qty), el("span", "line", api.money(i.line_total_usd)));
        list.append(row);
      });
      box.append(list);
    }

    const totals = el("div", "totals");
    totals.append(
      el("span", "muted", "Subtotal"), el("span", null, api.money(cart.subtotal_usd)),
      el("span", "muted", "Tax"), el("span", null, api.money(cart.tax_usd)),
      el("span", "grand", "Total"), el("span", "grand", api.money(cart.total_usd)),
    );
    box.append(totals);

    const budget = Number(cart.budget_usd) || 0;
    const ratio = budget > 0 ? cart.total_usd / budget : 0;
    const wrap = el("div", "budget");
    const label = el("div", "budget-label");
    label.append(el("span", null, "Budget"), el("span", null, api.money(cart.total_usd) + " of " + api.money(budget)));
    const bar = el("div", "bar" + (cart.over_budget ? " red" : ratio >= 0.8 ? " amber" : ""));
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", "Budget used");
    bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    const fill = el("span");
    fill.style.width = Math.min(100, ratio * 100) + "%";
    bar.append(fill);
    wrap.append(label, bar);
    box.append(wrap);

    (cart.warnings || []).forEach((w) => {
      box.append(el("p", "strip warn", w.message + " (bay " + w.bay + ")"));
    });
  }

  function renderOverrides(s) {
    const box = $("overrides");
    box.replaceChildren();
    const canOverride = s.session && s.session.state === "IN_STORE";
    const inCart = {};
    if (s.cart) s.cart.items.forEach((i) => { inCart[i.sku] = i.qty; });

    s.skus.forEach((sku) => {
      const row = el("div", "override-row");
      const meta = el("div", "meta");
      const ov = s.overrides[sku.sku] || 0;
      meta.append(el("span", "name", sku.name));
      meta.append(el("span", "ov" + (ov ? " active" : ""), ov ? "override " + (ov > 0 ? "+" : "") + ov : "no override"));

      const stepper = el("div", "stepper");
      const minus = el("button", null, "−");
      minus.type = "button";
      minus.title = "−1 from cart";
      minus.setAttribute("aria-label", "−1 " + sku.name + " from cart");
      const count = el("span", "count", String(inCart[sku.sku] || 0));
      count.setAttribute("aria-label", "in cart");
      const plus = el("button", null, "+");
      plus.type = "button";
      plus.title = "+1 to cart";
      plus.setAttribute("aria-label", "+1 " + sku.name + " to cart");
      [[minus, -1], [plus, 1]].forEach(([btn, delta]) => {
        btn.disabled = !canOverride;
        btn.addEventListener("click", () => act(btn, "/admin/override", { sku: sku.sku, delta }));
      });
      stepper.append(minus, count, plus);
      row.append(meta, stepper);
      box.append(row);
    });

    if (!canOverride) {
      box.append(el("p", "hint small mt-3", "Overrides need a shopper in the store (state IN_STORE)."));
    }
  }

  function renderShelf(shelf) {
    const box = $("bays");
    box.replaceChildren();
    Object.keys(shelf.bays).forEach((bayId) => {
      const status = shelf.bay_status[bayId] || {};
      const units = shelf.bays[bayId];
      let kind, text;
      if (status.stable === null || status.stable === undefined) {
        kind = "nodata"; text = "no data";
      } else if (status.motion) {
        kind = "motion"; text = "motion";
      } else if (status.stable) {
        kind = "stable"; text = "stable";
      } else {
        kind = "unstable"; text = "unstable";
      }
      const tile = el("div", "bay " + kind + (units.length ? "" : " empty"));
      const head = el("div", "bay-head");
      const pillKind = { stable: "ok", motion: "warn", unstable: "warn", nodata: "" }[kind];
      head.append(el("span", "bay-id", "Bay " + bayId), el("span", "pill" + (pillKind ? " " + pillKind : ""), text));
      const count = el("div", "bay-count", String(units.length));
      count.append(el("small", null, units.length === 1 ? "tag" : "tags"));
      tile.append(head, count, el("div", "bay-tags", units.length ? "ids " + units.join(", ") : "empty"));
      box.append(tile);
    });
    const counts = Object.entries(shelf.counts).map(([k, v]) => k + " " + v).join(", ");
    $("loose").textContent = "Counts: " + (counts || "–") + (shelf.loose_units.length ? " · loose tags: " + shelf.loose_units.join(", ") : "");
  }

  function logFamily(type) {
    if (/fail|error|declin|bad|refus/.test(type)) return "bad";
    if (type.startsWith("admin") || type === "force_decline" || type === "demo_login") return "admin";
    if (type.startsWith("ws_")) return "ws";
    if (/cart|override|shelf|bay/.test(type)) return "cart";
    if (/gate|enter|exit|session|dev_/.test(type)) return "gate";
    if (/pay|charge|stripe|receipt/.test(type)) return "pay";
    return "";
  }

  function renderLog() {
    const box = $("log");
    box.replaceChildren();
    box.classList.toggle("empty", events.length === 0);
    if (events.length === 0) {
      box.textContent = "No events yet.";
      return;
    }
    events.slice().reverse().forEach((e) => {
      const { ts, type, ...rest } = e;
      const row = el("div");
      const family = logFamily(type || "");
      row.append(
        el("span", "t", (ts || "").slice(11, 23)),
        el("span", "ty" + (family ? " " + family : ""), type),
        el("span", "d", JSON.stringify(rest)),
      );
      box.append(row);
    });
  }

  async function refresh() {
    try {
      renderState(await api.get("/admin/state"));
    } catch (e) {
      if (e.status === 401) showLogin();
      else api.toast(e.message);
    }
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 120);
  }

  async function pollHealth() {
    try {
      renderHealth(await api.get("/api/health"));
    } catch (e) {
      setBadge("b-vision", "Backend unreachable", "bad");
    }
  }

  async function act(button, path, body) {
    button.disabled = true;
    try {
      await api.post(path, body);
      scheduleRefresh();
    } catch (e) {
      api.toast(e.message);
    } finally {
      button.disabled = false;
    }
  }

  function onMessage(msg) {
    if (msg.type === "log") {
      events.push(msg.data);
      if (events.length > 50) events = events.slice(-50);
      renderLog();
      return;
    }
    // cart, shelf, gate, store_status: pull the full state once things settle.
    scheduleRefresh();
  }

  function setLive(live) {
    const dot = $("live-dot");
    dot.classList.toggle("live", live);
    dot.title = live ? "Live" : "Reconnecting…";
    dot.setAttribute("aria-label", dot.title);
    $("live-label").textContent = dot.title;
    const pill = $("live-pill");
    pill.classList.toggle("live", live);
    pill.classList.toggle("off", !live);
  }

  function showLogin() {
    $("panel").hidden = true;
    $("login").hidden = false;
    $("password").focus();
  }

  function showPanel() {
    $("login").hidden = true;
    $("panel").hidden = false;
    if (!socketStarted) {
      socketStarted = true;
      connectSocket({ role: "admin", onMessage, onStatus: setLive, onResync: scheduleRefresh });
      setInterval(pollHealth, 1000);
      setInterval(refresh, 5000);
    }
  }

  function init() {
    LED_COMMANDS.forEach((cmd) => {
      const b = el("button", null, cmd);
      b.type = "button";
      b.addEventListener("click", () => act(b, "/admin/led", { cmd }));
      $("leds").append(b);
    });

    $("login").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = ev.target.querySelector("button");
      btn.disabled = true;
      btn.classList.add("busy");
      try {
        await api.post("/admin/login", { password: $("password").value });
        $("password").value = "";
        showPanel();
        await refresh();
      } catch (e) {
        api.toast(e.message);
      } finally {
        btn.disabled = false;
        btn.classList.remove("busy");
      }
    });

    $("demo-login").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        const res = await api.post("/admin/demo-login");
        api.toast("This browser is now signed in as " + res.member.name + ".");
      } catch (e) {
        api.toast(e.message);
      } finally {
        btn.disabled = false;
      }
    });
    $("reset").addEventListener("click", (ev) => {
      if (confirm("Cancel the active session and clear overrides?")) act(ev.currentTarget, "/admin/reset");
    });
    $("force-exit").addEventListener("click", (ev) => {
      if (confirm("Cancel the active session?")) act(ev.currentTarget, "/admin/force-exit");
    });
    $("force-decline").addEventListener("click", (ev) =>
      act(ev.currentTarget, "/admin/force-decline", { on: !(state && state.force_decline) }));

    api.get("/admin/state")
      .then((s) => { showPanel(); renderState(s); })
      .catch((e) => { if (e.status === 401) showLogin(); else api.toast(e.message); });
  }

  init();
})();
