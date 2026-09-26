// Page script for admin.html: team-only control panel (11.6).
// Everything shown comes from GET /admin/state, /api/health and admin WebSocket messages.
// Same ids, endpoints and behaviour as before; only the rendered markup changed for the dashboard look.

(function () {
  const $ = (id) => document.getElementById(id);
  const VISION_MAX_AGE_MS = 2000;
  // Gate screen tests (raw serial). There are no bay or status LEDs any more, so no LED/SHELF/GATE/HILITE.
  const LED_COMMANDS = ["DISP,IDLE", "DISP,WELCOME,Maya", "DISP,TOTAL,$8.64,1", "DISP,FIND,2 and 4",
    "DISP,PAID,$8.64,A1B2C3", "DISP,REFUND,$8.64", "DISP,DECLINED", "DISP,OCCUPIED,Maya", "PING"];
  const STRIPE_LABELS = { test: "Test mode", no_key: "No key", off: "Off" };
  let state = null;
  let events = [];
  let refreshTimer = null;
  let socketStarted = false;
  // Review queue (8.14): a sound and a badge when a new dispute needs a decision.
  let soundOn = true;
  let audioCtx = null;
  const seenOpen = new Set();
  let disputesSeeded = false;

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
    const kind = { IN_STORE: "live", CHECKOUT_PENDING: "warn", RETURNING: "warn", PAID: "ok", CANCELLED: "bad", CLOSED: "" }[sessionState];
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
    renderReviewHealth(s.health.review);
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

    renderCart(s.cart, s.return);
    renderOverrides(s);
    renderMetrics(s.metrics);
    renderDisputes(s.disputes || []);
    renderShelf(s.shelf);
    events = s.events.slice();
    renderLog();
    $("state-json").textContent = JSON.stringify(s, null, 2);
  }

  function seconds(v) {
    return v === null || v === undefined ? "–" : v + " s";
  }

  // The dispute review model (8.14): configured, and does it take images?
  function renderReviewHealth(r) {
    if (!r || !r.available) { setBadge("b-review", "Off · a person decides", "warn"); return; }
    if (r.vision === true) setBadge("b-review", "Vision · " + r.model, "ok");
    else if (r.vision === false) setBadge("b-review", "Text only · " + r.model, "warn");
    else setBadge("b-review", "Checking " + r.model + "…", "warn");
  }

  // Measured results for today: sessions, time in store, exit scan to approval, refunds.
  function renderMetrics(m) {
    const box = $("metrics");
    box.replaceChildren();
    if (!m) return;
    [["Sessions", String(m.sessions_today) + (m.paid_today !== m.sessions_today ? " · " + m.paid_today + " paid" : "")],
      ["Avg in store", seconds(m.avg_in_store_s)],
      ["Exit scan to approval", seconds(m.avg_exit_to_approval_s)],
      ["Refunds", m.refunds_today + (m.refunds_today ? " · " + api.money(m.refunded_usd_today) : "")],
      ["Disputes", String(m.disputes_today) + (m.open_now ? " · " + m.open_now + " open" : "")],
      ["AI · human agree", m.ai_human_agreement_pct === null || m.ai_human_agreement_pct === undefined ? "–" : m.ai_human_agreement_pct + "%"],
      ["Auto approved", String(m.auto_approved_today)]]
      .forEach(([k, v]) => {
        const tile = el("span", "stat");
        tile.append(el("span", "k", k), el("span", "v", v));
        box.append(tile);
      });
  }

  // Review queue (8.13 + 8.14): each dispute with the AI verdict (confidence bar + summary), its observations
  // linked to keyframes, an inline player per clip, the baseline vs latest photos, the timeline and the two
  // decision buttons. The AI never decides against a shopper; only "Keep the charge" here does, with a note.
  const OUTCOME_KIND = { resolved_camera: "ok", needs_review: "warn", kept: "", removed: "ok", refunded: "ok", charge_confirmed: "bad" };
  const OUTCOME_LABEL = { needs_review: "needs a decision", resolved_camera: "camera resolved", kept: "shopper kept it",
    removed: "removed", refunded: "refunded", charge_confirmed: "charge confirmed" };
  const VERDICT_LABEL = { supports_customer: "Supports the shopper", supports_charge: "Supports the charge", unclear: "Unclear" };
  const VERDICT_KIND = { supports_customer: "ok", supports_charge: "bad", unclear: "warn" };

  function beep() {
    if (!soundOn) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") audioCtx.resume();
      [[880, 0], [1175, 0.16]].forEach(([freq, at]) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, audioCtx.currentTime + at);
        gain.gain.exponentialRampToValueAtTime(0.2, audioCtx.currentTime + at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + at + 0.15);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(audioCtx.currentTime + at);
        osc.stop(audioCtx.currentTime + at + 0.16);
      });
    } catch (e) { /* no audio: the badge still shows */ }
  }

  function evidencePhoto(p, caption) {
    const fig = el("figure", "evidence");
    const frame = el("div", "evidence-frame" + (p ? "" : " empty"));
    if (p) {
      const img = el("img");
      img.src = p.url;
      img.alt = caption;
      img.loading = "lazy";
      frame.append(img);
      if (p.outline) {
        const box = el("span", "evidence-outline");
        Object.assign(box.style, { left: p.outline.x * 100 + "%", top: p.outline.y * 100 + "%",
          width: p.outline.w * 100 + "%", height: p.outline.h * 100 + "%" });
        frame.append(box);
      }
    } else {
      frame.append(el("span", "muted small", "No photo"));
    }
    fig.append(frame, el("figcaption", null, caption));
    return fig;
  }

  function clock(iso) {
    try { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }); }
    catch (e) { return iso || ""; }
  }

  function renderReview(d) {
    const box = el("div", "ai-verdict");
    const r = d.review;
    const row = el("div", "row");
    if (!r) {
      row.append(el("strong", null, "AI review"), el("span", "pill warn", "pending"));
      box.append(row, el("p", "ai-summary muted small", "Looking at the shelf keyframes…"));
      return box;
    }
    const kind = VERDICT_KIND[r.verdict] || "warn";
    row.append(el("strong", null, "AI review: " + (VERDICT_LABEL[r.verdict] || r.verdict)),
      el("span", "pill " + kind, r.supports_customer_pct + "% for the shopper"));
    const bar = el("div", "confidence " + kind);
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", "Supports the shopper");
    bar.setAttribute("aria-valuenow", String(r.supports_customer_pct));
    const fill = el("span");
    fill.style.width = Math.max(0, Math.min(100, r.supports_customer_pct)) + "%";
    bar.append(fill);
    box.append(row, bar, el("p", "ai-summary", r.summary));
    const source = r.source === "vision" ? "from the keyframes and the timeline" : r.source === "timeline"
      ? "from the timeline only (the model took no images)" : "no model answer";
    box.append(el("p", "muted small", source + (r.model ? " · " + r.model : "") + " · the AI can only speed up small refunds; a person decides the rest"));
    if (r.evidence && r.evidence.length) {
      const list = el("ul", "observations");
      r.evidence.forEach((e) => {
        const li = el("li");
        if (e.url) {
          const a = el("a");
          a.href = e.url;
          a.target = "_blank";
          a.rel = "noopener";
          a.title = "Open frame " + e.frame;
          const img = el("img");
          img.src = e.url;
          img.alt = "Frame " + e.frame;
          img.loading = "lazy";
          a.append(img);
          li.append(a);
        }
        const text = el("div");
        text.append(el("span", "frame-id", e.frame + " "), el("span", null, e.observation));
        li.append(text);
        list.append(li);
      });
      box.append(list);
    }
    return box;
  }

  function renderClips(d) {
    const clips = d.clips || [];
    if (!clips.length) return el("p", "muted small mt-3", "No event clips for this bay.");
    const grid = el("div", "clip-list");
    clips.forEach((c) => {
      const card = el("div", "clip");
      card.append(el("div", "small", "Bay " + c.card + " changed at " + clock(c.change_at) + " · tags " +
        (c.units_before.join(", ") || "none") + " → " + (c.units_after.join(", ") || "none") +
        (c.motion.length ? " · motion " + c.motion.map((m) => clock(m.from) + "–" + clock(m.to)).join(", ") : "")));
      if (c.url) {
        const video = el("video");
        video.controls = true;
        video.preload = "metadata";
        video.muted = true;
        video.playsInline = true;
        video.src = c.url;
        card.append(video);
      }
      const strip = el("div", "keyframe-strip");
      c.keyframes.forEach((k) => {
        const a = el("a");
        a.href = k.url;
        a.target = "_blank";
        a.rel = "noopener";
        const img = el("img");
        img.src = k.url;
        img.alt = k.id + " at " + clock(k.captured_at);
        img.title = k.id + " · " + clock(k.captured_at);
        img.loading = "lazy";
        a.append(img);
        strip.append(a);
      });
      card.append(strip);
      grid.append(card);
    });
    return grid;
  }

  function renderTimeline(d) {
    const t = d.timeline;
    const box = el("details", "timeline");
    box.append(el("summary", null, "Timeline"));
    if (!t) { box.append(el("p", "muted small", "No timeline.")); return box; }
    const lines = [];
    if (t.session_started_at) lines.push(clock(t.session_started_at) + "  visit started");
    (t.tags_seen || []).forEach((x) => lines.push(clock(x.at) + "  " + x.kind + " photo, tags " + (x.tag_ids.join(", ") || "none")));
    (t.clips || []).forEach((c) => {
      lines.push(clock(c.change_at) + "  stable change, units " + (c.units_before.join(", ") || "none") + " → " + (c.units_after.join(", ") || "none"));
      (c.motion_periods || []).forEach((m) => lines.push(clock(m.from) + "  motion until " + clock(m.to)));
    });
    (t.cart_changes || []).forEach((c) => lines.push(clock(c.at) + "  cart " + JSON.stringify(c.items) + " · " + api.money(c.total_usd) + " (" + c.source + ")"));
    lines.push(clock(t.dispute_opened_at) + "  dispute opened (" + t.stage.replace("_", " ") + "), missing tag " + (t.missing_tag_id === null ? "unknown" : t.missing_tag_id));
    const list = el("ul");
    lines.sort().forEach((l) => list.append(el("li", null, l)));
    box.append(list);
    return box;
  }

  async function decide(d, action, note, buttons) {
    if (note.trim().length < 3) { api.toast("Add a short note for the record."); return; }
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const res = await api.post("/admin/disputes/" + encodeURIComponent(d.dispute_id) + "/" + action, { note: note.trim() });
      api.toast(action === "approve" ? "Refund approved" + (res.refund ? ": " + api.money(res.refund.amount_usd) : ".") : "Charge kept. The shopper sees your note.");
      scheduleRefresh();
    } catch (e) {
      api.toast(e.message);
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  function renderDecision(d) {
    if (d.status === "open") {
      const box = el("div", "decision");
      const note = el("textarea");
      note.placeholder = "Short note for the record (required)";
      note.maxLength = 300;
      note.rows = 2;
      const approve = el("button", "primary", "Approve refund" + (d.disputed_usd ? " " + api.money(d.disputed_usd) : ""));
      approve.type = "button";
      const keep = el("button", "danger", "Keep the charge");
      keep.type = "button";
      approve.addEventListener("click", () => decide(d, "approve", note.value, [approve, keep]));
      keep.addEventListener("click", () => decide(d, "keep", note.value, [approve, keep]));
      const buttons = el("div", "buttons");
      buttons.append(approve, keep);
      box.append(note, buttons);
      return box;
    }
    const dec = d.decision;
    if (!dec) {
      return el("p", "decision-record muted", d.outcome === "kept" ? "The shopper found it and kept it."
        : d.outcome === "resolved_camera" ? "Settled by the camera recheck."
        : "Settled by the shopper (Remove anyway).");
    }
    const who = dec.by === "ai" ? "Auto approved by policy (small amount, clear review)" : "Staff decision: " + (dec.decision === "approve" ? "refund approved" : "charge kept");
    const agree = dec.agreed === true ? " · agreed with the AI" : dec.agreed === false ? " · overruled the AI" : "";
    const p = el("p", "decision-record");
    p.append(el("strong", null, who + agree + " · " + ago(dec.at)));
    if (dec.note) p.append(el("br"), el("span", null, "Note: " + dec.note));
    return p;
  }

  let disputesKey = null;

  function renderDisputes(list) {
    const open = list.filter((d) => d.status === "open");
    const badge = $("dispute-badge");
    badge.textContent = String(open.length);
    badge.hidden = open.length === 0;
    document.title = (open.length ? "(" + open.length + ") " : "") + "SpeedMart · Admin";
    let fresh = false;
    open.forEach((d) => {
      if (!seenOpen.has(d.dispute_id)) { seenOpen.add(d.dispute_id); fresh = true; }
    });
    if (fresh && disputesSeeded) beep();
    disputesSeeded = true;

    const key = JSON.stringify(list.map((d) => [d.dispute_id, d.outcome, d.status, d.amount_usd, !!d.review, !!d.decision,
      !!d.evidence && !!d.evidence.now, (d.clips || []).length]));
    if (key === disputesKey) return; // the 5 s refresh must not reload every photo or clear a half typed note
    disputesKey = key;
    const box = $("disputes");
    box.replaceChildren();
    if (!list.length) {
      box.append(el("p", "empty-cart", "No disputes yet."));
      return;
    }
    list.forEach((d) => {
      const entry = el("div", "dispute-entry" + (d.status === "open" ? " open" : ""));
      const head = el("div", "head");
      const who = el("div");
      who.append(el("div", null, (d.first_name || d.member_name || "Shopper") + " · " + d.name + " · " + api.money(d.disputed_usd || 0)));
      who.append(el("div", "muted small mono", d.stage.replace("_", " ") + " · " + d.session_id + " · " + ago(d.created_at)));
      const kind = OUTCOME_KIND[d.outcome];
      const pill = el("span", "pill" + (kind ? " " + kind : ""), (OUTCOME_LABEL[d.outcome] || d.outcome.replace("_", " ")) +
        (d.amount_usd !== null && d.amount_usd !== undefined ? " · " + api.money(d.amount_usd) : ""));
      head.append(who, pill);
      entry.append(head);
      entry.append(renderReview(d));
      const ev = d.evidence;
      if (ev) {
        const grid = el("div", "evidence-grid");
        grid.append(evidencePhoto(ev.before, "Walked in"), evidencePhoto(ev.now, "Latest"));
        entry.append(grid);
      }
      entry.append(renderClips(d));
      entry.append(renderTimeline(d));
      entry.append(renderDecision(d));
      if (d.refund_id) entry.append(el("p", "muted small mono", "Refund " + d.refund_id));
      box.append(entry);
    });
  }

  // A return in progress: what the camera has seen come back so far (items capped at the purchase).
  function renderReturn(box, ret) {
    const head = el("div", "cart-state");
    head.append(el("span", "muted small", "Return of " + ret.original_session_id), statePill("RETURNING"));
    box.append(head);
    const list = el("ul", "cart-list");
    ret.items.forEach((i) => {
      const r = el("li", "cart-row");
      r.append(el("span", "name", i.name), el("span", "qty", "× " + i.qty), el("span", "line", api.money(i.line_total_usd)));
      list.append(r);
    });
    ret.ignored.forEach((i) => {
      const r = el("li", "cart-row");
      r.append(el("span", "name", i.name), el("span", "qty", "× " + i.qty), el("span", "line muted", i.message));
      list.append(r);
    });
    box.append(ret.items.length || ret.ignored.length ? list : el("p", "empty-cart", "Nothing back on the shelf yet."));
    const totals = el("div", "totals");
    totals.append(el("span", "grand", "Refund"), el("span", "grand", api.money(ret.total_usd)));
    box.append(totals);
  }

  function renderCart(cart, ret) {
    const box = $("cart");
    box.replaceChildren();
    if (ret) {
      box.className = "stack";
      renderReturn(box, ret);
      return;
    }
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
    if (/pay|charge|stripe|receipt|refund|return/.test(type)) return "pay";
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
    if (msg.type === "dispute") {  // a new dispute, an AI review or a decision: the queue re-renders from state
      scheduleRefresh();
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
    $("sound-toggle").addEventListener("click", (ev) => {
      soundOn = !soundOn;
      ev.currentTarget.textContent = "Sound: " + (soundOn ? "on" : "off");
      ev.currentTarget.setAttribute("aria-pressed", String(soundOn));
      if (soundOn) beep();
    });

    api.get("/admin/state")
      .then((s) => { showPanel(); renderState(s); })
      .catch((e) => { if (e.status === 401) showLogin(); else api.toast(e.message); });
  }

  init();
})();
