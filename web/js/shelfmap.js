// Shelf map: a drawn shelf of every bay with its printed card number, product and units on the shelf.
// Bays and products come from GET /api/catalog (config.json bays + catalog.json SKUs), so a new bay or SKU
// needs no change here. Recommended bays glow and pulse (a steady glow under prefers-reduced-motion).
//
//   const map = ShelfMap.create(el, { size: "compact" | "regular" | "large" });
//   map.setGlow([1, 3]);      // bay ids, as in Plan.bays and the plan_bays message
//   map.refresh();            // re-read the counts (throttled)
//   map.cardForSku("bar")     // 3: the card of the bay that product belongs in
//   ShelfMap.lookFor([1, 3])  // "Look for bay 2 and bay 4, they're glowing on your screen."
//
// Bay id 0 is card "1": the backend sends that as bays[].card, and cardOf() uses the same rule before the
// catalog has loaded.

(function () {
  const REFRESH_MIN_MS = 1500;
  const MAX_PIPS = 6;

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
    let bays = [];            // from /api/catalog
    let glow = new Set();
    let lastFetch = 0;
    let pending = null;

    root.classList.add("shelf-map", "shelf-map--" + size);
    root.setAttribute("role", "list");
    root.setAttribute("aria-label", "Shelf map");

    function render() {
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
        root.appendChild(bay);
      });
    }

    async function load() {
      lastFetch = Date.now();
      try {
        const cat = await api.get("/api/catalog");
        bays = cat.bays || [];
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

    function setGlow(ids) {
      glow = new Set((ids || []).map(Number));
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
