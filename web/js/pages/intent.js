// Page script for intent.html (F18): shopper says what they need, backend returns a Plan.
// Renders only what the backend sends. It never adds up prices itself.

(function () {
  const $ = (id) => document.getElementById(id);
  let busy = false;

  function show(which) {
    ["signin", "ask", "loading", "plan"].forEach((id) => { $(id).hidden = !which.includes(id); });
  }

  function showError(text) {
    const el = $("error");
    el.textContent = text || "";
    el.hidden = !text;
  }

  function renderPlan(plan) {
    $("plan-title").textContent = plan.goal_summary ? "Your plan: " + plan.goal_summary : "Your plan";
    const list = $("plan-items");
    list.textContent = "";
    plan.items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "plan-item";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = item.name + (item.qty > 1 ? " × " + item.qty : "");
      const price = document.createElement("span");
      price.className = "price";
      price.textContent = api.money(item.unit_price_usd) + (item.qty > 1 ? " each" : "");
      const reason = document.createElement("span");
      reason.className = "reason";
      reason.textContent = item.reason;
      li.append(name, price, reason);
      list.appendChild(li);
    });
    const empty = plan.items.length === 0;
    $("plan-empty").hidden = !empty;
    $("plan-total").textContent = api.money(plan.est_total_usd);
    $("plan-budget").textContent = api.money(plan.budget_usd) + (plan.fits_budget ? " ✓" : "");
    $("plan-source").textContent = plan.source === "llm"
      ? "Planned by the store AI from today's shelf."
      : "Planned from today's shelf by keyword match.";
    $("blink-note").hidden = empty || !plan.bays || plan.bays.length === 0;
    show(["plan"]);
  }

  async function ask(text) {
    text = (text || "").trim();
    if (!text) { showError("Tell us what you need first, or tap an example."); return; }
    if (busy) return;
    busy = true;
    showError("");
    show(["loading"]);
    try {
      renderPlan(await api.post("/api/intent", { text }));
    } catch (e) {
      if (e.status === 401) { show(["signin"]); return; }
      show(["ask"]);
      showError(e.code === "network" ? e.message : (e.message || "We couldn't make a plan. Please try again."));
    } finally {
      busy = false;
    }
  }

  async function startOver() {
    showError("");
    try {
      // api.js has no DELETE helper; plain fetch keeps this page self-contained.
      await fetch("/api/intent", { method: "DELETE", credentials: "same-origin" });
    } catch (e) { /* offline: still let them type a new request */ }
    $("goal").value = "";
    show(["ask"]);
    $("goal").focus();
  }

  function setupMic() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btn = $("mic-btn");
    if (!Recognition) { btn.hidden = true; return; } // Web Speech API not supported here
    btn.hidden = false;
    let rec = null;
    btn.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new Recognition();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = true;
      rec.maxAlternatives = 1;
      btn.classList.add("listening");
      btn.setAttribute("aria-label", "Stop listening");
      let finalText = "";
      rec.onresult = (ev) => {
        let interim = "";
        for (let i = ev.resultIndex; i < ev.results.length; i++) {
          const t = ev.results[i][0].transcript;
          if (ev.results[i].isFinal) finalText += t; else interim += t;
        }
        $("goal").value = (finalText + interim).trim();
      };
      rec.onerror = (ev) => {
        if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
          api.toast("Microphone is blocked. You can type instead.");
        } else if (ev.error !== "aborted" && ev.error !== "no-speech") {
          api.toast("Didn't catch that. Try again or type it.");
        }
      };
      rec.onend = () => {
        btn.classList.remove("listening");
        btn.setAttribute("aria-label", "Speak your request");
        rec = null;
        if (finalText.trim()) ask(finalText);
      };
      try { rec.start(); } catch (e) { rec.onend(); }
    });
  }

  async function init() {
    $("ask-form").addEventListener("submit", (ev) => { ev.preventDefault(); ask($("goal").value); });
    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => { $("goal").value = chip.textContent; ask(chip.textContent); });
    });
    $("restart-btn").addEventListener("click", startOver);
    setupMic();

    show(["loading"]);
    try {
      const plan = await api.get("/api/intent/current");
      if (plan) renderPlan(plan); else show(["ask"]);
    } catch (e) {
      if (e.status === 401) { show(["signin"]); return; }
      show(["ask"]);
      showError(e.message);
    }
  }

  init();
})();
