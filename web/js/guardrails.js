// Permissions card, "Your agent's permissions" (8.10): what the store agent may do on its own, what only the
// shopper can do, and what never happens. Every line comes from GET /api/guardrails, which builds it from the
// signed-in member and the agent token's scope; this file only lays it out.
//
//   <details id="guardrails" class="card disclosure guardrails" hidden></details>
//   Guardrails.mount(document.getElementById("guardrails"), { open: false });   // store.html: tap to expand
//   Guardrails.mount(el, { open: true });                                        // exit.html: expanded

(function () {
  const GROUPS = [
    { key: "agent_can", title: "The agent can", kind: "can" },
    { key: "only_you", title: "Only you can", kind: "you" },
    { key: "never", title: "Never", kind: "never" },
  ];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function render(box, g, open) {
    box.replaceChildren();
    const summary = el("summary");
    summary.append(el("span", null, g.title));
    box.append(summary);
    GROUPS.forEach((group) => {
      const items = g[group.key] || [];
      if (!items.length) return;
      const section = el("div", "gr-group " + group.kind);
      section.append(el("p", "gr-title", group.title));
      const list = el("ul", "gr-list");
      items.forEach((text) => list.append(el("li", null, text)));
      section.append(list);
      box.append(section);
    });
    box.open = !!open;
    box.hidden = false;
  }

  // Returns the permissions shown, or null (signed out / offline: the card stays hidden).
  async function mount(box, opts) {
    if (!box) return null;
    try {
      const g = await api.get("/api/guardrails");
      render(box, g, box.hidden ? (opts && opts.open) : box.open); // a refresh keeps the shopper's choice
      return g;
    } catch (e) {
      box.hidden = true;
      return null;
    }
  }

  window.Guardrails = { mount };
})();
