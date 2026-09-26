// Demo bank (8.15): the simulated account behind the demo card. Renders what GET /api/bank and the
// {"type":"bank"} socket message carry; the page never adds balances up itself.
//
//   const card = Bank.mount(el, { compact: false });   // full card (home page) or the store header line
//   card.update(data);                                  // a bank summary (API result or socket message)
//   await card.load();                                  // GET /api/bank; hides the element when signed out

(function () {
  const reduceMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function money(usd) { return api.money(usd); }

  function signed(usd) {
    const n = Number(usd) || 0;
    return (n < 0 ? "−" : "+") + api.money(Math.abs(n));
  }

  function clock(iso) {
    try { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return iso || ""; }
  }

  // Tween a money amount from its last value to the new one (and bump it) so a live change is visible.
  function animateAmount(el, to) {
    const from = Number(el.dataset.value);
    const target = Number(to) || 0;
    el.dataset.value = String(target);
    if (Number.isNaN(from) || from === target || reduceMotion()) { el.textContent = money(target); return; }
    const start = performance.now();
    const dur = 550;
    el.classList.remove("bump");
    void el.offsetWidth; // restart the animation
    el.classList.add("bump");
    function step(now) {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = money(from + (target - from) * eased);
      if (t < 1) requestAnimationFrame(step);
      else el.textContent = money(target);
    }
    requestAnimationFrame(step);
  }

  const TYPE_LABEL = { opening: "Opening balance", charge: "Purchase", refund: "Refund", top_up: "Top up",
    hold: "Hold", hold_release: "Hold released" };

  function renderTransactions(list, txs, seen) {
    list.replaceChildren();
    if (!txs.length) {
      const p = document.createElement("p");
      p.className = "bank-empty";
      p.textContent = "No transactions yet.";
      list.appendChild(p);
      return;
    }
    txs.forEach((tx) => {
      const li = document.createElement("li");
      li.className = tx.status === "pending" ? "pending" : "";
      if (seen.size && !seen.has(tx.id)) li.classList.add("new");
      seen.add(tx.id);
      const desc = document.createElement("span");
      desc.className = "desc";
      desc.textContent = tx.description || TYPE_LABEL[tx.type] || tx.type;
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = (TYPE_LABEL[tx.type] || tx.type) + " · " + clock(tx.created_at);
      if (tx.status === "pending") {
        const badge = document.createElement("span");
        badge.className = "pending-badge";
        badge.textContent = "Pending";
        meta.appendChild(badge);
      }
      const amount = document.createElement("span");
      const credit = Number(tx.amount_usd) > 0 && tx.type !== "opening";
      amount.className = "amount " + (credit ? "credit" : "debit");
      amount.textContent = signed(tx.amount_usd);
      li.append(desc, meta, amount);
      list.appendChild(li);
    });
  }

  function buildFull(root) {
    root.innerHTML =
      '<section class="bank-card" aria-live="polite">' +
        '<div class="bank-head"><span class="bank-label" data-card></span><span class="bank-chip">Demo</span></div>' +
        '<span class="bank-available bank-amount" data-available>$0.00</span>' +
        '<div class="bank-available-label">Available</div>' +
        '<div class="bank-current">Current balance <strong class="bank-amount" data-current>$0.00</strong><span data-pending hidden></span></div>' +
        '<div class="bank-actions">' +
          '<button type="button" class="block" data-topup>Add $20 demo funds</button>' +
          '<p class="bank-topups" data-topups></p>' +
        '</div>' +
        '<p class="bank-note" data-note></p>' +
      '</section>' +
      '<section class="card">' +
        '<div class="card-head"><h2>Recent transactions</h2><span class="pill" data-count></span></div>' +
        '<ul class="bank-tx" data-tx></ul>' +
        '<p class="bank-note-inline small muted mt-3" data-note-2></p>' +
      '</section>';
  }

  function buildMini(root) {
    root.innerHTML =
      '<span class="bank-mini-card" data-card></span>' +
      '<span class="bank-mini-amount"><span class="bank-amount" data-available>$0.00</span> available</span>' +
      '<span class="bank-mini-note" data-note></span>';
  }

  function mount(root, opts) {
    const o = opts || {};
    const compact = !!o.compact;
    const seen = new Set();
    let data = null;
    if (compact) buildMini(root); else buildFull(root);
    const q = (sel) => root.querySelector(sel);

    function update(d) {
      if (!d) return;
      const first = data === null;
      data = d;
      root.hidden = false;
      q("[data-card]").textContent = d.card_label || "Demo Visa •••• 4242";
      const avail = q("[data-available]");
      if (first) { avail.dataset.value = String(Number(d.available_usd) || 0); avail.textContent = money(d.available_usd); }
      else animateAmount(avail, d.available_usd);
      const note = q("[data-note]");
      if (note) note.textContent = d.note || "Demo balance · not a real account";
      if (compact) return;
      const cur = q("[data-current]");
      if (first) { cur.dataset.value = String(Number(d.balance_usd) || 0); cur.textContent = money(d.balance_usd); }
      else animateAmount(cur, d.balance_usd);
      const pending = q("[data-pending]");
      const held = Number(d.pending_usd) || 0;
      pending.hidden = held <= 0;
      pending.textContent = held > 0 ? " · " + money(held) + " on hold" : "";
      const btn = q("[data-topup]");
      const left = Number(d.top_ups_left) || 0;
      btn.textContent = "Add " + money(d.top_up_usd || 20) + " demo funds";
      btn.disabled = left <= 0;
      q("[data-topups]").textContent = left > 0
        ? left + (left === 1 ? " top up left" : " top ups left") + " for this demo account"
        : "All demo top ups used. Ask the team to reset demo balances.";
      q("[data-count]").textContent = (d.transactions || []).length + " recent";
      renderTransactions(q("[data-tx]"), d.transactions || [], seen);
      q("[data-note-2]").textContent = d.note || "Demo balance · not a real account";
    }

    async function load() {
      try {
        update(await api.get("/api/bank"));
        return data;
      } catch (e) {
        if (e.status === 401) root.hidden = true;
        return null;
      }
    }

    async function topUp(ev) {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        update(await api.post("/api/bank/topup"));
        api.toast("Added " + money((data && data.top_up_usd) || 20) + " in demo funds.");
      } catch (e) {
        api.toast(e.message);
        if (e.code === "topup_limit") btn.disabled = true;
        else btn.disabled = false;
      }
    }

    if (!compact) q("[data-topup]").addEventListener("click", topUp);
    return { update, load, get data() { return data; } };
  }

  window.Bank = { mount, money: signed };
})();
