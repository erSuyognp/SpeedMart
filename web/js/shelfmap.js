// Shelf map: a drawn shelf of every bay with its printed card number, product and units on the shelf.
// Bays and products come from GET /api/catalog (config.json bays + catalog.json SKUs), so a new bay or SKU
// needs no change here. Recommended bays glow and pulse (a steady glow under prefers-reduced-motion).
//
//   const map = ShelfMap.create(el, { size: "compact" | "regular" | "large", flash: true });
//   map.setGlow([1, 3]);      // bay ids, as in Plan.bays and the plan_bays message
//   map.setGlow([1, 3], { sweep: true });  // bays that just lit up get one sweep of the glow gradient first
//   map.refresh();            // re-read the counts (throttled)
//   map.cardForSku("bar")     // 3: the card of the bay that product belongs in
//   ShelfMap.lookFor([1, 3])  // "Look for bay 2 and bay 4, they're glowing on your screen."
//
// Bay id 0 is card "1": the backend sends that as bays[].card, and cardOf() uses the same rule before the
// catalog has loaded.
//
// flash (the kiosk): a bay whose unit count changed since the last read flashes, green when a unit was taken
// (is-taken), blue when one came back (is-returned); app.css section 13 draws it. The map is re-drawn on every
// read, so a running effect is re-applied with a negative --fx-delay and carries on where it was.

(function () {
  const REFRESH_MIN_MS = 1500;
  const MAX_PIPS = 6;
  const FX_MS = { "is-taken": 1700, "is-returned": 1700, "glow--once": 1500 }; // how long each bay effect runs

  function cardOf(bay) { return Number(bay) + 1; }

  function joinAnd(parts) {
    if (parts.length <= 1) return parts.join("");
    return parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
  }

  // [1, 3] -> "bay 2 and bay 4"
  function baysText(bays) {
    const cards = [...new Set((bays || []).map(Number))].sort((a, b) => a - b).map(cardOf);
    return joinAnd(cards.map((c) => "bay " + c));
  }

  function lookFor(bays) {
    const n = new Set(bays || []).size;
    if (!n) return "";
    return "Look for " + baysText(bays) + (n === 1 ? ", it's glowing on your screen." : ", they're glowing on your screen.");
  }

  function countText(n) {
    if (n === null || n === undefined) return "…";
    return n === 0 ? "none left" : n + " on shelf";
  }

  function create(root, opts) {
    const size = (opts && opts.size) || "regular";
    const flash = !!(opts && opts.flash);
    let bays = [];            // from /api/catalog
    let glow = new Set();
    let lastFetch = 0;
    let pending = null;
    let counts = null;        // flash: bay id -> units on the shelf at the previous read
    const effects = new Map(); // bay id -> {cls, at}: a flash or sweep still running

    root.classList.add("shelf-map", "shelf-map--" + size);
    root.setAttribute("role", "list");
    root.setAttribute("aria-label", "Shelf map");

    function render() {
      const now = Date.now();
      root.textContent = "";
      root.style.setProperty("--sm-cols", String(Math.max(1, bays.length)));
      bays.forEach((b) => {
        const bay = document.createElement("div");
        bay.className = "sm-bay" + (glow.has(b.bay) ? " is-glow" : "") + (b.on_shelf === 0 ? " is-empty" : "");
        bay.setAttribute("role", "listitem");
        bay.setAttribute("aria-label", "Bay " + b.card + ": " + b.name + ", " + countText(b.on_shelf) +
          (glow.has(b.bay) ? ", on your list" : ""));

        const slot = document.createElement("div");
        slot.className = "sm-slot";
        slot.setAttribute("aria-hidden", "true");
        const pips = Math.min(b.on_shelf || 0, MAX_PIPS);
        for (let i = 0; i < pips; i++) {
          const pip = document.createElement("span");
          pip.className = "sm-unit";
          slot.appendChild(pip);
        }

        const name = document.createElement("div");
        name.className = "sm-name";
        name.textContent = b.name;
        name.setAttribute("aria-hidden", "true");

        const count = document.createElement("div");
        count.className = "sm-count";
        count.textContent = countText(b.on_shelf);
        count.setAttribute("aria-hidden", "true");

        // The printed number card taped to the shelf front.
        const card = document.createElement("div");
        card.className = "sm-card";
        card.textContent = String(b.card);
        card.setAttribute("aria-hidden", "true");

        bay.append(slot, name, count, card);
        const fx = effects.get(b.bay);
        if (fx && now - fx.at < FX_MS[fx.cls]) {
          bay.classList.add(fx.cls);
          bay.style.setProperty("--fx-delay", (fx.at - now) + "ms");
        }
        root.appendChild(bay);
      });
    }

    async function load() {
      lastFetch = Date.now();
      try {
        const cat = await api.get("/api/catalog");
        bays = cat.bays || [];
        noteCounts(bays);
        render();
      } catch (e) {
        // Offline: keep the last drawing; the next refresh tries again.
      }
    }

    function refresh() {
      const wait = lastFetch + REFRESH_MIN_MS - Date.now();
      if (wait <= 0) return load();
      if (!pending) pending = setTimeout(() => { pending = null; load(); }, wait);
      return undefined;
    }

    // Flash mode: which bays hold fewer (taken) or more (returned) units than at the previous read.
    function noteCounts(next) {
      if (!flash) return;
      const now = Date.now();
      if (counts) {
        next.forEach((b) => {
          const was = counts.get(b.bay);
          if (typeof was !== "number" || typeof b.on_shelf !== "number" || was === b.on_shelf) return;
          effects.set(b.bay, { cls: b.on_shelf < was ? "is-taken" : "is-returned", at: now });
        });
      }
      counts = new Map(next.map((b) => [b.bay, b.on_shelf]));
    }

    function setGlow(ids, o) {
      const next = new Set((ids || []).map(Number));
      if (o && o.sweep) {
        const now = Date.now();
        next.forEach((id) => { if (!glow.has(id)) effects.set(id, { cls: "glow--once", at: now }); });
      }
      glow = next;
      render();
    }

    // The card a product belongs on (for "return it to bay 3"), or null before the catalog has loaded.
    function cardForSku(sku) {
      const b = bays.find((x) => x.sku === sku);
      return b ? b.card : null;
    }

    load();
    return { refresh, setGlow, cardForSku, get glowing() { return [...glow]; } };
  }

  window.ShelfMap = { create, cardOf, baysText, lookFor };
})();
