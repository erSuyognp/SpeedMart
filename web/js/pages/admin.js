// Page script for admin.html: team-only control panel (11.6).
// Everything shown comes from GET /admin/state, /api/health and admin WebSocket messages.

(function () {
  const $ = (id) => document.getElementById(id);
  const VISION_MAX_AGE_MS = 2000;
  const LED_COMMANDS = ["SHELF,GREEN", "SHELF,RED", "SHELF,IDLE", "GATE,OPEN", "GATE,CLOSED", "PING"];
  let state = null;
  let events = [];
  let refreshTimer = null;
  let socketStarted = false;

  function setBadge(id, text, kind) {
    const el = $(id);
    el.textContent = text;
    el.className = "badge" + (kind ? " " + kind : "");
  }

  function renderHealth(h) {
    if (h.vision_age_ms < 0) setBadge("b-vision", "Vision: no snapshot", "bad");
    else setBadge("b-vision", "Vision " + h.vision_age_ms + " ms", h.vision_age_ms > VISION_MAX_AGE_MS ? "bad" : "ok");
    setBadge("b-serial", "Serial " + (h.serial ? "on" : "off"), h.serial ? "ok" : "bad");
  }

  function kv(pairs) {
    const dl = document.createDocumentFragment();
    pairs.forEach(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      dl.append(dt, dd);
    });
    return dl;
  }

  function renderState(s) {
    state = s;
    renderHealth(s.health);
    setBadge("b-stripe", "Stripe " + s.health.stripe, s.health.stripe === "test" ? "ok" : "warn");
    setBadge("b-llm", "LLM " + (s.health.llm ? "on" : "off"), s.health.llm ? "ok" : "warn");
    setBadge("b-lock", s.lock.occupied ? "Store occupied" : "Store free", s.lock.occupied ? "warn" : "ok");

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
      box.textContent = "No shopper in the store.";
      box.className = "stack muted";
      return;
    }
    box.className = "stack";
    if (cart.items.length === 0) {
      const p = document.createElement("p");
      p.className = "muted";
      p.style.margin = "0";
      p.textContent = "Cart is empty.";
      box.append(p);
    }
    cart.items.forEach((i) => {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = '<span class="grow"></span><span class="mono"></span>';
      row.children[0].textContent = i.name + " × " + i.qty;
      row.children[1].textContent = api.money(i.line_total_usd);
      box.append(row);
    });
    const total = document.createElement("div");
    total.className = "row";
    total.innerHTML = '<strong class="grow">Total (' + cart.state + ')</strong><strong class="mono"></strong>';
    total.children[1].textContent = api.money(cart.total_usd) + " / " + api.money(cart.budget_usd);
    box.append(total);
    (cart.warnings || []).forEach((w) => {
      const p = document.createElement("p");
      p.className = "strip warn";
      p.textContent = w.message + " (bay " + w.bay + ")";
      box.append(p);
    });
  }

  function renderOverrides(s) {
    const box = $("overrides");
    box.replaceChildren();
    const canOverride = s.session && s.session.state === "IN_STORE";
    s.skus.forEach((sku) => {
      const row = document.createElement("div");
      row.className = "override-row";
      const label = document.createElement("span");
      const ov = s.overrides[sku.sku] || 0;
      label.textContent = sku.name + (ov ? " (override " + (ov > 0 ? "+" : "") + ov + ")" : "");
      const minus = document.createElement("button");
      minus.textContent = "−1 from cart";
      const plus = document.createElement("button");
      plus.textContent = "+1 to cart";
      [[minus, -1], [plus, 1]].forEach(([btn, delta]) => {
        btn.disabled = !canOverride;
        btn.addEventListener("click", () => act(btn, "/admin/override", { sku: sku.sku, delta }));
      });
      row.append(label, minus, plus);
      box.append(row);
    });
  }

  function renderShelf(shelf) {
    const box = $("bays");
    box.replaceChildren();
    Object.keys(shelf.bays).forEach((bayId) => {
      const status = shelf.bay_status[bayId] || {};
      const row = document.createElement("div");
      row.className = "bay";
      const label = document.createElement("span");
      const units = shelf.bays[bayId];
      label.textContent = "Bay " + bayId + ": tags " + (units.length ? units.join(", ") : "none");
      const badge = document.createElement("span");
      if (status.stable === null || status.stable === undefined) {
        badge.className = "badge";
        badge.textContent = "no data";
      } else if (status.motion) {
        badge.className = "badge warn";
        badge.textContent = "motion";
      } else {
        badge.className = "badge " + (status.stable ? "ok" : "warn");
        badge.textContent = status.stable ? "stable" : "unstable";
      }
      row.append(label, badge);
      box.append(row);
    });
    const counts = Object.entries(shelf.counts).map(([k, v]) => k + " " + v).join(", ");
    $("loose").textContent = "Counts: " + counts + (shelf.loose_units.length ? " · loose tags: " + shelf.loose_units.join(", ") : "");
  }

  function renderLog() {
    const box = $("log");
    box.replaceChildren();
    events.slice().reverse().forEach((e) => {
      const div = document.createElement("div");
      const { ts, type, ...rest } = e;
      div.textContent = (ts || "").slice(11, 23) + "  " + type + "  " + JSON.stringify(rest);
      box.append(div);
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
      const b = document.createElement("button");
      b.textContent = cmd;
      b.addEventListener("click", () => act(b, "/admin/led", { cmd }));
      $("leds").append(b);
    });

    $("login").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = ev.target.querySelector("button");
      btn.disabled = true;
      try {
        await api.post("/admin/login", { password: $("password").value });
        $("password").value = "";
        showPanel();
        await refresh();
      } catch (e) {
        api.toast(e.message);
      } finally {
        btn.disabled = false;
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
