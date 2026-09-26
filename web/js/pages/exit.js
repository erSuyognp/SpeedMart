// Page script for exit.html?g=<token> (11.4, QR 3 · EXIT). Gates off: the cart's Checkout button opens this
// page without a token and the quote comes from /api/dev/checkout instead.
// Renders the backend's frozen cart and instruction; never adds anything up itself.

(function () {
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("g") || "";
  let cfg = { features: {} };
  let quote = null; // {cart, instruction, plan_check}

  const passkeysOn = () => !!cfg.features.passkeys;

  function notice(o) {
    $("review").hidden = true;
    $("notice").hidden = false;
    $("notice-icon").className = "big-icon " + (o.icon || "brand");
    $("notice-icon").textContent = o.mark || (o.icon === "ok" ? "✓" : "!");
    $("notice-title").textContent = o.title;
    $("notice-text").textContent = o.text || "";
    $("notice-link").hidden = !o.link;
    if (o.link) { $("notice-link").textContent = o.link.text; $("notice-link").href = o.link.href; }
  }

  function when(iso) {
    try { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return iso; }
  }

  function renderNetwork(instr) {
    $("network").hidden = !instr;
    if (!instr) return;
    const scope = instr.agent_token.scope;
    $("n-agent").textContent = instr.agent.name + " (" + instr.agent.id + ")";
    $("n-merchant").textContent = scope.merchant;
    $("n-max").textContent = api.money(scope.max_amount_usd) + " " + scope.currency;
    $("n-single").textContent = scope.single_use ? "Yes" : "No";
    $("n-expires").textContent = when(scope.expires_at);
    $("n-intent").textContent = instr.user_intent;
    $("n-label").textContent = instr.label;
    $("n-json").textContent = JSON.stringify(instr, null, 2);
  }

  function render(res) {
    quote = res;
    const cart = res.cart;
    if (cart.state === "CLOSED") {
      notice({ icon: "ok", title: "Nothing to pay, see you soon", text: "Your cart was empty, so there is no charge." });
      return;
    }
    $("notice").hidden = true;
    $("review").hidden = false;

    // F18: how this cart compares with the plan the shopper asked for (null when there is no plan).
    const planCheck = res.plan_check;
    $("plan-check").hidden = !planCheck || !planCheck.summary;
    $("plan-check").textContent = planCheck && planCheck.summary ? planCheck.summary : "";

    const list = $("items");
    list.innerHTML = "";
    const disputes = Dispute.enabled(cfg); // F20: tap an item to dispute it
    $("dispute-hint").hidden = !disputes || cart.items.length === 0;
    cart.items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "cart-row";
      li.innerHTML = '<span class="name"></span><span class="qty"></span><span class="line"></span>';
      li.querySelector(".name").textContent = item.name;
      li.querySelector(".qty").textContent = "× " + item.qty;
      li.querySelector(".line").textContent = api.money(item.line_total_usd);
      if (disputes) {
        li.classList.add("tappable");
        li.setAttribute("role", "button");
        li.tabIndex = 0;
        li.setAttribute("aria-label", "Dispute " + item.name);
        const openDispute = () => Dispute.open({ sku: item.sku, name: item.name,
          onChange: () => loadQuote().catch((e) => api.toast(e.message)) });
        li.addEventListener("click", openDispute);
        li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDispute(); } });
      }
      list.appendChild(li);
    });
    $("subtotal").textContent = api.money(cart.subtotal_usd);
    $("tax").textContent = api.money(cart.tax_usd);
    $("total").textContent = api.money(cart.total_usd);

    const instr = res.instruction;
    renderNetwork(instr);
    Guardrails.mount($("guardrails"), { open: true }); // the scope of this instruction, expanded
    const overScope = !!instr && cart.total_usd > instr.agent_token.scope.max_amount_usd;
    $("over-scope").hidden = !overScope;
    if (overScope) {
      $("over-scope").textContent = "This is over your " + api.money(instr.agent_token.scope.max_amount_usd) +
        " limit. Put something back to continue.";
    }
    const verb = passkeysOn() ? "Approve " + api.money(cart.total_usd) + " with Face ID" : "Confirm " + api.money(cart.total_usd);
    $("approve-btn").textContent = verb;
    $("approve-btn").hidden = !instr || overScope;
    $("cancel-btn").className = (overScope ? "primary" : "ghost") + " block";
    if (instr && !overScope && passkeysOn()) passkey.prefetch("login", "exit");
  }

  async function loadCard() {
    try {
      const me = await api.get("/api/me");
      if (me.member.card_label) $("card-line").textContent = "Paying with " + me.member.card_label;
    } catch (e) { /* the notice already explains */ }
    loadBank();
  }

  // Demo bank (8.15): the available demo balance this charge has to fit. The backend decides at approve time.
  async function loadBank() {
    try {
      const b = await api.get("/api/bank");
      const line = $("bank-line");
      line.replaceChildren();
      line.append(b.card_label + " \u00b7 available ");
      const strong = document.createElement("strong");
      strong.textContent = api.money(b.available_usd);
      line.append(strong);
      const note = document.createElement("span");
      note.className = "bank-note-inline";
      note.textContent = b.note;
      line.append(note);
      line.hidden = false;
    } catch (e) { $("bank-line").hidden = true; }
  }

  async function onApprove(ev) {
    const btn = ev.currentTarget;
    btn.disabled = true;
    $("declined").hidden = true;
    try {
      if (passkeysOn()) await passkey.signIn("exit"); // Face ID straight from the tap
      const res = await api.post("/api/gate/exit/approve");
      const p = res.payment;
      if (p.status === "AUTHORIZED") {
        location.href = "/receipt.html?s=" + encodeURIComponent(quote.cart.session_id);
        return;
      }
      $("declined").textContent = "Declined: " + (res.message || "the card was declined") + ". You can try again.";
      $("declined").hidden = false;
      btn.textContent = passkeysOn() ? "Try again with Face ID" : "Try again";
    } catch (e) {
      if (e instanceof api.ApiError && e.code === "over_scope") {
        $("over-scope").textContent = e.message;
        $("over-scope").hidden = false;
        btn.hidden = true;
      } else if (e instanceof api.ApiError && e.code === "insufficient_funds") {
        // Demo bank (8.15): the cart does not fit the demo balance. Put something back, or add demo funds.
        const strip = $("over-scope");
        strip.replaceChildren();
        strip.append(document.createTextNode(e.message + " "));
        const link = document.createElement("a");
        link.href = "/";
        link.textContent = "Add demo funds";
        strip.append(link);
        strip.hidden = false;
        btn.textContent = passkeysOn() ? "Try again with Face ID" : "Try again";
        loadBank();
      } else {
        $("declined").textContent = e instanceof api.ApiError ? e.message : passkey.friendlyError(e);
        $("declined").hidden = false;
      }
    } finally {
      btn.disabled = false;
    }
  }

  async function onCancel(ev) {
    ev.currentTarget.disabled = true;
    try { await api.post("/api/gate/exit/cancel"); } catch (e) { /* the cart page shows the real state */ }
    location.href = "/store.html";
  }

  // The quote freezes the cart once; asking again returns the frozen cart (and closes it if a dispute emptied it).
  async function loadQuote() {
    const res = cfg.features.gates
      ? await api.post("/api/gate/exit/quote", { gate_token: token })
      : await api.post("/api/dev/checkout");
    render(res);
  }

  async function init() {
    $("approve-btn").addEventListener("click", onApprove);
    $("cancel-btn").addEventListener("click", onCancel);
    try {
      cfg = await api.config();
      if (cfg.features.gates && !token) {
        notice({ title: "Scan the code again", text: "This link is missing the exit code.", icon: "bad" });
        return;
      }
      if (passkeysOn()) { passkey.load(); passkey.showNotes(); }
      await loadQuote();
      loadCard();
    } catch (e) {
      if (e.code === "not_logged_in") {
        notice({ icon: "bad", title: "Sign in first", text: "Open this code on the phone you shopped with.",
                 link: { text: "Go to SpeedMart", href: "/" } });
      } else if (e.code === "no_active_session") {
        notice({ icon: "bad", title: "You're not in the store", text: "Nothing to check out.",
                 link: { text: "Go to your cart", href: "/store.html" } });
      } else {
        notice({ icon: "bad", title: "Can't load your receipt", text: e.message });
      }
    }
  }

  init();
})();
