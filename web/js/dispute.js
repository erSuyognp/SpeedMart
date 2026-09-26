// Cart disputes with camera evidence (8.13, F20 `disputes`): one bottom sheet shared by store.html ("Not mine?"),
// exit.html (tap an item) and receipt.html ("Report a problem"). The backend rechecks the shelf and decides;
// this only shows its answer: "Our mistake" at once, or the two shelf photos with "Found it, keep it" and
// "Remove anyway". Photos come from an authenticated route that only serves this shopper's own visit.

(function () {
  const STAFF = "Please ask a staff member.";
  const TEST_REFUND = "Test refund. No real money moves.";
  let els = null;
  let opts = null; // {sku, name, sessionId, onChange}
  let dispute = null; // last Dispute from the backend

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function build() {
    if (els) return els;
    const backdrop = el("div", "sheet-backdrop");
    const sheet = el("div", "sheet dispute-sheet");
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.setAttribute("aria-labelledby", "dispute-title");
    const title = el("h2", null, "Not mine?");
    title.id = "dispute-title";
    const item = el("p", "muted dispute-item");
    const body = el("div", "stack dispute-body");
    body.setAttribute("aria-live", "polite");
    const actions = el("div", "actions");
    sheet.append(el("div", "sheet-handle"), title, item, body, actions);
    document.body.append(backdrop, sheet);
    backdrop.addEventListener("click", close);
    els = { backdrop, sheet, title, item, body, actions };
    return els;
  }

  function show(on) {
    els.backdrop.classList.toggle("open", on);
    els.sheet.classList.toggle("open", on);
  }

  function close() {
    if (els) show(false);
  }

  function button(text, kind, onClick) {
    const b = el("button", kind + " block", text);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function setActions(...buttons) {
    els.actions.replaceChildren(...buttons);
  }

  function message(text, kind) {
    const p = el("p", "strip " + (kind || "ok"));
    p.append(el("span", null, text));
    return p;
  }

  function photo(p, caption, bay) {
    const fig = el("figure", "evidence");
    const frame = el("div", "evidence-frame");
    if (p && p.url) {
      const img = el("img");
      img.src = p.url;
      img.alt = "Bay " + bay + ", " + caption.toLowerCase();
      img.loading = "eager";
      frame.append(img);
      if (p.outline) {
        const box = el("span", "evidence-outline");
        box.style.left = p.outline.x * 100 + "%";
        box.style.top = p.outline.y * 100 + "%";
        box.style.width = p.outline.w * 100 + "%";
        box.style.height = p.outline.h * 100 + "%";
        frame.append(box);
      }
    } else {
      frame.classList.add("empty");
      frame.append(el("span", "muted small", "No photo"));
    }
    fig.append(frame, el("figcaption", null, caption));
    return fig;
  }

  function renderChecking() {
    const row = el("div", "dispute-checking");
    row.append(el("span", "spinner"), el("span", "muted", "Checking the shelf camera…"));
    els.body.replaceChildren(row);
    setActions();
  }

  function renderDone(text, kind) {
    els.body.replaceChildren(message(text, kind));
    setActions(button("Done", "primary", close));
  }

  function renderError(text) {
    renderDone(text, "bad");
  }

  function renderReview(d) {
    const ev = d.evidence || {};
    const nodes = [el("p", "muted", d.message)];
    const grid = el("div", "evidence-grid");
    grid.append(photo(ev.before, "When you walked in", ev.card), photo(ev.now, "Now", ev.card));
    nodes.push(grid);
    const outlined = [ev.before, ev.now].some((p) => p && p.url && p.outline);
    const note = !ev.before && !ev.now ? "No shelf photos were saved for this visit."
      : outlined ? "The red box is where it was last seen." : "";
    if (ev.card) nodes.push(el("p", "muted small", "Bay " + ev.card + ". " + note));
    nodes.push(el("p", "privacy-note small", ev.privacy || "These photos show only the shelf and are deleted after your visit."));
    nodes.push(el("p", "muted small", "Not sure? Leave it: our team reviews every open case and your receipt shows the answer."));
    els.body.replaceChildren(...nodes);
    const keep = button("Found it, keep it", "secondary", onKeep);
    if (d.remove_anyway_left > 0) {
      setActions(keep, button("Remove anyway", "danger", onRemove));
    } else {
      els.body.append(message(STAFF, "warn"));
      setActions(keep);
    }
  }

  function render(res) {
    dispute = res.dispute;
    if (res.refund && res.refund.status !== "SUCCEEDED") {
      renderReview(dispute);
      els.body.append(message(res.message || "The refund did not go through. Try again.", "bad"));
      return;
    }
    if (dispute.outcome === "needs_review") renderReview(dispute);
    else renderDone(dispute.message);
    if (res.refund) {  // after paying: say where the refund went, and that it is a sandbox one
      const card = res.refund.card_label ? "Refund to " + res.refund.card_label + ". " : "";
      els.body.append(el("p", "demo-line small", card + TEST_REFUND));
    }
  }

  function changed(res) {
    if (opts && opts.onChange) {
      try { opts.onChange(res); } catch (e) { /* the page refreshes on its own too */ }
    }
  }

  async function act(path, btn) {
    btn.disabled = true;
    try {
      const res = await api.post(path);
      render(res);
      changed(res);
    } catch (e) {
      if (e.code === "dispute_limit") {
        renderReview({ ...dispute, remove_anyway_left: 0 });
      } else {
        renderError(e.message);
      }
    } finally {
      btn.disabled = false;
    }
  }

  function onKeep(ev) {
    act("/api/disputes/" + encodeURIComponent(dispute.dispute_id) + "/keep", ev.currentTarget);
  }

  function onRemove(ev) {
    act("/api/disputes/" + encodeURIComponent(dispute.dispute_id) + "/remove", ev.currentTarget);
  }

  async function open(o) {
    opts = o;
    dispute = null;
    build();
    els.title.textContent = o.sessionId ? "Report a problem" : "Not mine?";
    els.item.textContent = o.name || "";
    renderChecking();
    show(true);
    const body = { sku: o.sku };
    if (o.sessionId) body.session_id = o.sessionId;
    try {
      const res = await api.post("/api/disputes", body);
      render(res);
      changed(res);
    } catch (e) {
      renderError(e.message);
    }
  }

  window.Dispute = { open, close, enabled: (cfg) => !!(cfg && cfg.features && cfg.features.disputes) };
})();
