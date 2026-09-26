// Page script for receipt.html?s=<session_id> (11.5): paid receipt, auth code, points, payment record, measured
// results, and the Continue stage: "Return an item" within 30 minutes (Face ID, then put it back on the shelf and
// the camera confirms it). Everything shown comes from GET /api/receipt/{session_id} and the returns routes;
// the page never works out a refund itself.

(function () {
  const $ = (id) => document.getElementById(id);
  const sessionId = new URLSearchParams(location.search).get("s") || "";
  let cfg = { features: {} };
  let returning = null; // ReturnSnapshot while a return for this visit is open
  let timer = null;
  let finishing = false; // confirm sent: the socket's session messages are expected, not news
  let cardLabel = ""; // the paid visit's card label: always the demo card, never the shopper's own
  const TEST_REFUND = "Test refund. No real money moves.";

  const passkeysOn = () => !!cfg.features.passkeys;

  function time(iso) {
    try {
      return new Date(iso).toLocaleString([], { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
    } catch (e) { return iso; }
  }

  function clock(iso) {
    try { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return iso; }
  }

  function fail(title, text) {
    $("icon").className = "big-icon bad";
    $("icon").textContent = "!";
    $("title").textContent = title;
    $("card").textContent = "";
    $("test-note").hidden = true;
    $("meta").textContent = text || "";
  }

  function row(name, qty, line) {
    const li = document.createElement("li");
    li.className = "cart-row";
    li.innerHTML = '<span class="name"></span><span class="qty"></span><span class="line"></span>';
    li.querySelector(".name").textContent = name;
    li.querySelector(".qty").textContent = qty;
    li.querySelector(".line").textContent = line;
    return li;
  }

  // Demo bank (8.15): "Balance after this purchase: $X.XX" (hidden when the charge predates the demo bank).
  function renderBankAfter(r) {
    const line = $("bank-after");
    const b = r.bank || {};
    const after = b.balance_after_usd;
    if (!r.paid || after === null || after === undefined) { line.hidden = true; return; }
    line.replaceChildren();
    line.append("Balance after this purchase: ");
    const strong = document.createElement("strong");
    strong.textContent = api.money(after);
    line.append(strong);
    const note = document.createElement("span");
    note.className = "bank-note-inline";
    note.textContent = (b.card_label ? b.card_label + " · " : "") + (b.note || "Demo balance · not a real account");
    line.append(note);
    line.hidden = false;
  }

  function renderResults(r) {
    const box = $("results");
    box.replaceChildren();
    if (!r.paid || r.in_and_out_s === null || r.in_and_out_s === undefined) { box.hidden = true; return; }
    const chips = ["In and out in " + r.in_and_out_s + (r.in_and_out_s === 1 ? " second" : " seconds"),
      r.approvals + (r.approvals === 1 ? " tap to pay" : " taps to pay")];
    chips.forEach((text) => {
      const chip = document.createElement("span");
      chip.className = "result-chip";
      chip.textContent = text;
      box.appendChild(chip);
    });
    box.hidden = false;
  }

  function renderRefunds(refunds) {
    const box = $("refunds");
    box.replaceChildren();
    (refunds || []).forEach((f) => {
      const strip = document.createElement("section");
      strip.className = "strip ok";
      const main = document.createElement("p");
      main.textContent = "Refunded " + api.money(f.amount_usd) + " for " + f.items_text +
        (f.reason === "dispute" ? " (reported problem)" : "");
      const sub = document.createElement("p");
      sub.className = "small";
      const provider = f.provider === "stripe_test" ? "Stripe test mode" : "sandbox mock";
      sub.textContent = "Refund " + (f.provider_ref || f.refund_id) + " · " + provider +
        (f.points_removed ? " · −" + f.points_removed + " points" : "");
      const test = document.createElement("p");
      test.className = "small";
      test.textContent = (f.card_label ? "To " + f.card_label + ". " : "") + TEST_REFUND;
      strip.append(main, sub, test);
      box.appendChild(strip);
    });
    box.hidden = !box.children.length;
  }

  // Reported problems (8.14): the status of each dispute on this visit, in neutral words, plus the staff note.
  const STATUS_TEXT = {
    "Under review": "Our team is taking a look at the shelf photos. The answer will show up here.",
    "Refunded": "Refunded to your test card. No real money moves.",
    "Removed from your cart": "Taken off your cart before you paid.",
    "Charge confirmed": "Our team checked the shelf photos and kept this charge.",
  };
  const STATUS_KIND = { "Under review": "warn", "Refunded": "ok", "Removed from your cart": "ok", "Charge confirmed": "" };

  function renderDisputes(list) {
    const card = $("disputes-card");
    const box = $("dispute-list");
    box.replaceChildren();
    (list || []).forEach((d) => {
      const status = d.customer_status || {};
      const li = document.createElement("li");
      li.className = "dispute-status";
      const head = document.createElement("div");
      head.className = "head";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = d.name + (d.amount_usd !== null && d.amount_usd !== undefined ? " · " + api.money(d.amount_usd) : "");
      const pill = document.createElement("span");
      const kind = STATUS_KIND[status.label];
      pill.className = "pill" + (kind ? " " + kind : "");
      pill.textContent = status.label || "";
      head.append(name, pill);
      const text = document.createElement("p");
      text.className = "muted small";
      text.textContent = STATUS_TEXT[status.label] || "";
      li.append(head, text);
      if (status.note) {
        const note = document.createElement("p");
        note.className = "staff-note small";
        note.textContent = "Staff note: " + status.note;
        li.append(note);
      }
      const keys = (d.clips || []).flatMap((c) => c.keyframes || []).slice(-6);
      if (keys.length) {
        const strip = document.createElement("div");
        strip.className = "keyframe-strip";
        keys.forEach((k) => {
          const img = document.createElement("img");
          img.src = k.url;
          img.alt = "Shelf at " + clock(k.captured_at);
          img.loading = "lazy";
          strip.append(img);
        });
        const cap = document.createElement("p");
        cap.className = "privacy-note small";
        cap.textContent = "Shelf moments our team looks at. Shelf only, deleted after the review.";
        li.append(strip, cap);
      }
      box.append(li);
    });
    card.hidden = !box.children.length || !!returning;
  }

  function renderOffer(r) {
    const ret = r.return || {};
    const show = r.paid && !returning && (ret.eligible || ret.reason === "return_window_closed");
    $("return-offer").hidden = !show;
    if (!show) return;
    $("return-btn").hidden = !ret.eligible;
    $("return-note").textContent = ret.eligible
      ? "Put it back on the shelf and get a refund. Returns close at " + clock(ret.deadline) + "."
      : ret.message;
    if (ret.eligible && passkeysOn()) passkey.prefetch("login", "return");
  }

  // "Report a problem" (8.13): pick an item from this visit, then the same dispute sheet as the cart.
  function renderReport(r) {
    const rep = r.report || {};
    const show = !!(r.paid && !returning && Dispute.enabled(cfg) && rep.eligible);
    $("report-offer").hidden = !show;
    if (!show) return;
    $("report-note").textContent = "Charged for something you didn't take? Report it by " + clock(rep.deadline) + ".";
    const list = $("report-items");
    list.replaceChildren();
    rep.items.forEach((item) => {
      const li = row(item.name, "× " + item.qty, "");
      li.classList.add("tappable");
      li.setAttribute("role", "button");
      li.tabIndex = 0;
      li.setAttribute("aria-label", "Report " + item.name);
      const openDispute = () => Dispute.open({ sku: item.sku, name: item.name, sessionId,
        onChange: () => loadReceipt().catch((e) => api.toast(e.message)) });
      li.addEventListener("click", openDispute);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDispute(); } });
      list.appendChild(li);
    });
  }

  function render(r) {
    const p = r.payment;
    const loyalty = !!(cfg.features.loyalty && r.loyalty);
    if (!r.paid) {
      fail("Not paid", p ? "The last attempt was " + p.status.toLowerCase() + ". Go back to the exit to try again."
        : "There is no payment for this visit.");
    } else {
      $("icon").className = "big-icon ok";
      $("icon").textContent = "✓";
      $("title").textContent = "Paid " + api.money(p.amount_usd);
      $("card").textContent = p.card_label;
      $("test-note").hidden = false;
      cardLabel = p.card_label || "";
      const provider = p.provider === "stripe_test" ? "Stripe test mode" : "sandbox mock";
      $("meta").textContent = "Auth code " + p.auth_code + " · " + time(p.created_at) + " · " + provider;
      $("points").hidden = !loyalty;
      $("points").textContent = "+" + r.points_earned + " points · " + r.points_total + " total";
    }
    renderBankAfter(r);
    renderResults(r);
    renderRefunds(r.refunds);

    $("items-card").hidden = r.items.length === 0;
    $("store-name").textContent = r.store_name || "Items";
    $("items").innerHTML = "";
    r.items.forEach((item) => $("items").appendChild(row(item.name, "× " + item.qty, api.money(item.line_total_usd))));
    $("subtotal").textContent = api.money(r.subtotal_usd);
    $("tax").textContent = api.money(r.tax_usd);
    $("total").textContent = api.money(r.total_usd);

    $("record").hidden = !p;
    if (p) $("record-json").textContent = JSON.stringify({ payment: p, refunds: r.refunds || [] }, null, 2);
    renderOffer(r);
    renderReport(r);
    renderDisputes(r.disputes);
  }

  async function loadReceipt() {
    render(await api.get("/api/receipt/" + encodeURIComponent(sessionId)));
  }

  // --- the return (RETURNING session) ---

  function setReturnMode(on) {
    $("return-panel").hidden = !on;
    $("confirm-refund").hidden = !on;
    $("cancel-return").hidden = !on;
    $("done").hidden = on;
    $("paid-card").hidden = on; // the instruction goes to the top of the screen
    ["items-card", "record", "report-offer", "disputes-card"].forEach((id) => { if (on) $(id).hidden = true; });
    if (on) $("return-offer").hidden = true;
    clearInterval(timer);
    if (on) timer = setInterval(tick, 1000);
  }

  function tick() {
    if (!returning) return;
    const left = Math.max(0, Math.round((Date.parse(returning.expires_at) - Date.now()) / 1000));
    $("return-timer").textContent = left > 0
      ? "Time left " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") + "."
      : "Time's up.";
    if (left === 0) $("confirm-refund").disabled = true;
  }

  function renderReturn(snap) {
    returning = snap;
    setReturnMode(true);
    $("return-items").replaceChildren();
    snap.items.forEach((i) => $("return-items").appendChild(row(i.name, "× " + i.qty, api.money(i.line_total_usd))));
    $("return-empty").hidden = snap.items.length > 0;
    $("return-ignored").replaceChildren();
    snap.ignored.forEach((i) => $("return-ignored").appendChild(row(i.name, "× " + i.qty, i.message)));
    $("return-subtotal").textContent = api.money(snap.subtotal_usd);
    $("return-tax").textContent = api.money(snap.tax_usd);
    $("return-total").textContent = api.money(snap.total_usd);
    $("return-from").textContent = snap.returnable.length
      ? "Refundable from this visit: " + snap.returnable.map((i) => i.qty + " " + i.name).join(", ") + "."
      : "";
    $("return-card").textContent = (cardLabel ? "Refund to " + cardLabel + ". " : "") + TEST_REFUND;
    const btn = $("confirm-refund");
    btn.textContent = snap.items.length ? "Confirm refund " + api.money(snap.total_usd) : "Confirm refund";
    btn.disabled = snap.items.length === 0;
    tick();
  }

  async function endReturn(message) {
    returning = null;
    setReturnMode(false);
    if (message) api.toast(message);
    try { await loadReceipt(); } catch (e) { api.toast(e.message); }
  }

  async function onReturn(ev) {
    const btn = ev.currentTarget;
    btn.disabled = true;
    $("return-error").hidden = true;
    try {
      if (passkeysOn()) await passkey.signIn("return"); // Face ID straight from the tap, same rule as the exit
      const res = await api.post("/api/returns/start", { session_id: sessionId });
      $("refund-agent").hidden = true;
      renderReturn(res.return);
    } catch (e) {
      let text = e instanceof api.ApiError ? e.message : passkey.friendlyError(e);
      if (e.code === "store_occupied") text = "Someone is shopping right now. Try again in a minute.";
      $("return-error").textContent = text;
      $("return-error").hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  async function onConfirm(ev) {
    const btn = ev.currentTarget;
    btn.disabled = true;
    $("return-panel-error").hidden = true;
    finishing = true;
    try {
      const res = await api.post("/api/returns/confirm");
      if (res.refund.status !== "SUCCEEDED") {
        $("return-panel-error").textContent = res.message || "The refund did not go through. Try again.";
        $("return-panel-error").hidden = false;
        btn.disabled = false;
        return;
      }
      $("refund-agent").textContent = res.agent_line + " " + TEST_REFUND;
      $("refund-agent").hidden = false;
      await endReturn();
    } catch (e) {
      $("return-panel-error").textContent = e.message;
      $("return-panel-error").hidden = false;
      if (e.code === "return_expired" || e.code === "no_active_return") await endReturn();
      else btn.disabled = false;
    } finally {
      finishing = false;
    }
  }

  async function onCancel(ev) {
    ev.currentTarget.disabled = true;
    try { await api.post("/api/returns/cancel"); } catch (e) { /* the receipt shows the real state */ }
    ev.currentTarget.disabled = false;
    await endReturn("Return cancelled. Nothing was refunded.");
  }

  async function syncReturn() {
    try {
      const res = await api.get("/api/returns/current");
      const snap = res.return;
      if (snap && snap.original_session_id === sessionId) renderReturn(snap);
      else if (returning && !finishing) await endReturn("Your return has ended.");
    } catch (e) { /* signed out: the receipt call already explained */ }
  }

  function onMessage(msg) {
    if (msg.type === "return" && msg.data.original_session_id === sessionId) {
      renderReturn(msg.data);
    } else if (msg.type === "gate" && msg.data.event === "cancelled" && returning) {
      endReturn("The return timed out. Nothing was refunded.");
    } else if (msg.type === "dispute" && msg.data.session_id === sessionId && !returning) {
      loadReceipt().catch(() => { /* the next reload shows it */ });  // a decision landed: refresh the status
    }
  }

  async function init() {
    if (!sessionId) { fail("No receipt", "This link is missing the visit id."); return; }
    $("return-btn").addEventListener("click", onReturn);
    $("confirm-refund").addEventListener("click", onConfirm);
    $("cancel-return").addEventListener("click", onCancel);
    $("report-btn").addEventListener("click", () => {
      $("report-btn").hidden = true;
      $("report-pick").hidden = false;
      $("report-items").hidden = false;
    });
    try {
      cfg = await api.config();
      if (passkeysOn()) { passkey.load(); passkey.showNotes(); }
      await loadReceipt();
    } catch (e) {
      fail("Can't show this receipt", e.message);
      return;
    }
    await syncReturn(); // reopened mid-return: pick it up again
    connectSocket({ onMessage, onResync: syncReturn });
  }

  init();
})();
