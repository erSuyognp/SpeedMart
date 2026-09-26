// Page script for receipt.html?s=<session_id> (11.5): paid receipt, auth code, points, payment record.
// Everything shown comes from GET /api/receipt/{session_id}.

(function () {
  const $ = (id) => document.getElementById(id);
  const sessionId = new URLSearchParams(location.search).get("s") || "";

  function time(iso) {
    try {
      return new Date(iso).toLocaleString([], { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
    } catch (e) { return iso; }
  }

  function fail(title, text) {
    $("icon").className = "big-icon bad";
    $("icon").textContent = "!";
    $("title").textContent = title;
    $("card").textContent = "";
    $("meta").textContent = text || "";
  }

  function render(r, cfg) {
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
      const provider = p.provider === "stripe_test" ? "Stripe test mode" : "sandbox mock";
      $("meta").textContent = "Auth code " + p.auth_code + " · " + time(p.created_at) + " · " + provider;
      $("points").hidden = !loyalty;
      $("points").textContent = "+" + r.points_earned + " points · " + r.points_total + " total";
    }

    $("items-card").hidden = r.items.length === 0;
    $("store-name").textContent = r.store_name || "Items";
    $("items").innerHTML = "";
    r.items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "cart-row";
      li.innerHTML = '<span class="name"></span><span class="qty"></span><span class="line"></span>';
      li.querySelector(".name").textContent = item.name;
      li.querySelector(".qty").textContent = "× " + item.qty;
      li.querySelector(".line").textContent = api.money(item.line_total_usd);
      $("items").appendChild(li);
    });
    $("subtotal").textContent = api.money(r.subtotal_usd);
    $("tax").textContent = api.money(r.tax_usd);
    $("total").textContent = api.money(r.total_usd);

    $("record").hidden = !p;
    if (p) $("record-json").textContent = JSON.stringify(p, null, 2);
  }

  async function init() {
    if (!sessionId) { fail("No receipt", "This link is missing the visit id."); return; }
    try {
      const cfg = await api.config();
      render(await api.get("/api/receipt/" + encodeURIComponent(sessionId)), cfg);
    } catch (e) {
      fail("Can't show this receipt", e.message);
    }
  }

  init();
})();
