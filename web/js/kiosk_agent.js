// Kiosk agent: web/kiosk.html?k=<KIOSK_TOKEN> turns the entrance tablet into a guide for shoppers, first timers
// especially (docs/voice.md, "Kiosk"). js/pages/kiosk.js calls KioskAgent.init() only when the URL has ?k=; if
// the backend refuses the token, init() resolves false and the page stays the public kiosk (no voice, no
// shopper data).
//
//   - Start overlay, once per page load: the tap unlocks audio playback and asks for the microphone. After that
//     the kiosk speaks on its own.
//   - Step tracker 1 Join, 2 Enter, 3 Grab items, 4 Scan exit, driven by kiosk_visit messages on /ws.
//   - Speech queue: every line is shown as a large caption; its audio comes from GET /api/kiosk/tts/{id}. Lines
//     never interrupt each other. If the audio fails or is not ready within 5 s, the caption shows alone.
//   - Attract mode and the ~30 s tour, also offered on its own when the shelf reports motion while the store is
//     empty (shelf_activity), at most once every 2 minutes.
//   - The shopper panel (first name, budget left, cart total, item count: kiosk_cart), the greeting on entry
//     (GET /api/kiosk/shopper, fuller on a first visit) and spoken cart lines (kiosk_say, chosen by the backend).
//     All of it is cleared the moment the visit ends.
//   - With an ElevenLabs agent configured: a conversation with each shopper who walks in (GET
//     /api/kiosk/voice-session, mode "kiosk"). Its client tools act on the current shopper through the kiosk
//     token routes; cart lines become contextual updates so the agent mentions them without talking over itself.
//     It ends at the exit scan, when the visit ends, after 2 minutes of silence, and after 5 minutes at most.
//
// Every kiosk-only request carries the token as ?k=. Texts come from the backend; this file never builds a price.

(function () {
  const AUDIO_WAIT_MS = 5000;      // fetch the clip; slower than this and the line is a caption only
  const MAX_PLAY_MS = 30000;       // a clip that never reports "ended" still lets the queue move on
  const READ_MS_PER_WORD = 380;    // caption-only lines stay up long enough to read
  const MIN_READ_MS = 2600;
  const GAP_MS = 350;              // breath between two lines
  const OFFER_EVERY_MS = 2 * 60 * 1000;
  const OFFER_GLOW_MS = 20000;
  const PAID_HOLD_MS = 6000;       // all four checks after paying, then back to step 1
  const CAPTION_DIM_MS = 12000;
  const IDLE_HINT_MS = 20000;      // after the last line, an idle kiosk goes back to its hint
  // Shown (not spoken) whenever nothing is being said: what to do at this step.
  const HINTS = {
    idle: "Scan a code with your phone camera to start.",
    shop: "Grab what you want from the shelf. The camera adds it to your cart.",
    exit: "Check your cart on your phone and approve.",
  };
  // 44 byte WAV with no samples: playing it inside the Start tap unlocks audio for this page.
  const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";

  // Same pinned SDK as the phone (js/pages/voice.js); sendContextualUpdate is in its BaseConversation types.
  const SDK_URL = "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm";
  const CONV_MAX_MS = 5 * 60 * 1000;     // the backend sends the same numbers with the session
  const CONV_SILENCE_MS = 2 * 60 * 1000;

  const $ = (id) => document.getElementById(id);

  let token = "";
  let script = null;        // GET /api/kiosk/phrases: {speech, voice, tour: [...], phrases: {...}}
  let started = false;      // the Start tap happened
  let audioEl = null;
  let visit = null;         // null while the store is free, else {step: 3 | 4}
  let paidUntil = 0;
  let touring = false;
  let tourStep = null;
  let lastOfferAt = 0;
  const queue = [];         // lines waiting to be said
  let playing = null;       // the line on screen and in the speaker now
  let stopPlaying = null;   // ends the current line early (the visit ended)
  let quietWaiters = [];
  let dimTimer = null;
  let hintTimer = null;
  let offerTimer = null;
  let paidTimer = null;
  let convState = "off";    // off | starting (fetching, waiting for quiet) | connecting | live
  let conv = null;          // the SDK Conversation while live
  let convTimers = [];
  let lastActivity = 0;
  let pendingContext = [];  // cart lines that arrived while the agent was connecting
  let sdkPromise = null;
  let convGen = 0;          // bumps on every start and end, so a start that was overtaken gives up

  // --- token-carrying requests ---

  function withToken(path) {
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + "k=" + encodeURIComponent(token);
  }

  function kget(path) { return api.get(withToken(path)); }

  function kpost(path, body) { return api.post(withToken(path), body); }

  async function kdelete(path) {
    const r = await fetch(withToken(path), { method: "DELETE", credentials: "same-origin" });
    if (!r.ok) throw new Error("The store couldn't do that right now.");
    return r.json();
  }

  // --- step tracker ---

  function renderSteps(current, doneUpTo) {
    document.querySelectorAll("#steps .k-step").forEach((li) => {
      const n = Number(li.dataset.step);
      li.classList.toggle("is-done", n <= doneUpTo);
      li.classList.toggle("is-current", n === current);
      if (n === current) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
  }

  function showSteps() {
    if (touring && tourStep) return renderSteps(tourStep, 0);      // the tour highlights the step it describes
    if (Date.now() < paidUntil) return renderSteps(0, 4);          // paid: every step checked
    if (visit) return renderSteps(visit.step, visit.step - 1);     // in the store: join and enter are done
    return renderSteps(touring ? 0 : 1, 0);                        // free: a newcomer starts at step 1
  }

  // --- caption and speaking indicator ---

  function setCaption(text, isHint) {
    const el = $("caption");
    el.textContent = text || "";
    el.classList.toggle("is-hint", !!isHint);
    el.classList.toggle("is-long", String(text || "").length > 100);
    el.classList.remove("is-old");
    clearTimeout(dimTimer);
    clearTimeout(hintTimer);
    if (!isHint) dimTimer = setTimeout(() => el.classList.add("is-old"), CAPTION_DIM_MS);
  }

  function hintText() {
    if (!visit) return HINTS.idle;
    return visit.step === 4 ? HINTS.exit : HINTS.shop;
  }

  function showHint() { setCaption(hintText(), true); }

  function hintLater() {
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { if (!playing && !queue.length) showHint(); }, IDLE_HINT_MS);
  }

  function setMode(mode, label) {
    $("agent").dataset.mode = mode;
    $("agent-state").textContent = label;
  }

  // --- speech queue ---

  function readMs(text) {
    return Math.max(MIN_READ_MS, String(text).split(/\s+/).length * READ_MS_PER_WORD);
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  async function fetchAudio(url) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), AUDIO_WAIT_MS);
    try {
      const r = await fetch(withToken(url), { signal: ctrl.signal, credentials: "same-origin" });
      if (!r.ok) return null;             // 503 tts_unavailable: caption only
      const blob = await r.blob();
      return blob.size ? URL.createObjectURL(blob) : null;
    } catch (e) {
      return null;                        // timeout or network: caption only
    } finally {
      clearTimeout(timer);
    }
  }

  // Resolves when the clip ends, fails, is stopped, or runs past MAX_PLAY_MS.
  function playClip(url, text) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(cap);
        stopPlaying = null;
        audioEl.onended = audioEl.onerror = null;
        try { audioEl.pause(); } catch (e) { /* not playing */ }
        URL.revokeObjectURL(url);
        resolve();
      };
      const cap = setTimeout(finish, MAX_PLAY_MS);
      stopPlaying = finish;
      audioEl.onended = finish;
      audioEl.onerror = finish;
      audioEl.src = url;
      const p = audioEl.play();
      if (p && p.catch) p.catch(() => setTimeout(finish, readMs(text))); // blocked: the caption still reads
    });
  }

  function captionPause(text) {
    return new Promise((resolve) => {
      const t = setTimeout(() => { stopPlaying = null; resolve(); }, readMs(text));
      stopPlaying = () => { clearTimeout(t); stopPlaying = null; resolve(); };
    });
  }

  function settleQuiet() {
    if (playing || queue.length) return;
    const waiters = quietWaiters;
    quietWaiters = [];
    waiters.forEach((w) => w());
  }

  // A promise that resolves once nothing is queued or playing (Phase 3 waits for this before it talks).
  function whenQuiet() {
    return new Promise((resolve) => { quietWaiters.push(resolve); settleQuiet(); });
  }

  async function pump() {
    // The agent speaks for itself while it is connected: queued lines wait (or become contextual updates).
    if (!started || playing || convState === "connecting" || convState === "live") return;
    if (!queue.length) { settleQuiet(); return; }
    const item = queue.shift();
    playing = item;
    const clip = item.audio_url ? await fetchAudio(item.audio_url) : null;
    if (item.dropped) {                   // the visit ended while the clip was loading
      if (clip) URL.revokeObjectURL(clip);
    } else {
      if (item.onStart) item.onStart();
      setCaption(item.text, false);
      setMode("speaking", "Speaking…");
      if (clip) await playClip(clip, item.text); else await captionPause(item.text);
      setMode("idle", "SpeedMart guide");
      if (item.onEnd) item.onEnd();
    }
    playing = null;
    if (!queue.length) hintLater();
    setTimeout(pump, GAP_MS);
  }

  // item: {kind, text, audio_url, step?}; opts: {onStart, onEnd}
  function say(item, opts) {
    if (!item || !item.text) return;
    queue.push(Object.assign({}, item, opts || {}));
    pump();
  }

  function dropQueued(match) {
    for (let i = queue.length - 1; i >= 0; i--) {
      if (match(queue[i])) { queue[i].dropped = true; queue.splice(i, 1); }
    }
    settleQuiet();
  }

  // --- tour and attract mode ---

  function stopOfferGlow() {
    clearTimeout(offerTimer);
    $("tour-btn").classList.remove("is-offer");
  }

  function endTour() {
    touring = false;
    tourStep = null;
    document.body.classList.remove("k-touring");
    showSteps();
  }

  function startTour() {
    if (!script || visit || touring || !started) return;
    touring = true;
    lastOfferAt = Date.now();             // no offer right after a tour
    document.body.classList.add("k-touring");
    stopOfferGlow();
    dropQueued((it) => it.kind === "offer");
    const lines = script.tour || [];
    lines.forEach((line, i) => say(line, {
      onStart: () => { tourStep = line.step || null; showSteps(); },
      onEnd: i === lines.length - 1 ? endTour : null,
    }));
    if (!lines.length) endTour();
  }

  function cancelTour() {
    if (!touring) return;
    dropQueued((it) => it.kind === "tour");
    if (playing && playing.kind === "tour") playing.onEnd = endTour; else endTour();
  }

  function onShelfActivity() {
    if (!started || !script || visit || touring || playing || queue.length) return;
    if (Date.now() - lastOfferAt < OFFER_EVERY_MS) return;
    lastOfferAt = Date.now();
    say(script.phrases.offer);
    $("tour-btn").classList.add("is-offer");
    clearTimeout(offerTimer);
    offerTimer = setTimeout(stopOfferGlow, OFFER_GLOW_MS);
  }

  // --- the shopper panel: first name, budget left, cart total, item count (kiosk_cart / GET /api/kiosk/shopper) ---

  function renderShopper(v) {
    if (!visit || !v) return;
    $("shopper-name").textContent = v.first_name;
    $("shopper-remaining").textContent = api.money(v.remaining_usd);
    $("shopper-total").textContent = api.money(v.cart_total_usd);
    $("shopper-items").textContent = String(v.item_count);
    const over = v.cart_total_usd > v.budget_usd;
    const left = v.budget_usd > 0 ? Math.max(0, Math.min(1, v.remaining_usd / v.budget_usd)) : 0;
    const bar = $("shopper-bar");
    bar.classList.toggle("is-over", over);
    bar.classList.toggle("is-warn", !over && left <= 0.2);
    bar.firstElementChild.style.width = (over ? 100 : Math.round(left * 100)) + "%";
    document.body.classList.add("k-has-shopper");
  }

  function clearShopper() {
    document.body.classList.remove("k-has-shopper");
    ["shopper-name", "shopper-remaining", "shopper-total", "shopper-items"].forEach((id) => { $(id).textContent = ""; });
    $("shopper-bar").firstElementChild.style.width = "0";
  }

  // On entry: the panel and the greeting (fuller on a first visit). On a resync: the panel only.
  // With an agent, a first timer hears the fuller welcome first and then the agent ("Hi <name>, you have $10 to
  // spend..."); a returning shopper's short greeting is only a caption, because the agent's first line says it.
  async function loadShopper(greet) {
    let body = null;
    try { body = await kget("/api/kiosk/shopper"); } catch (e) { return; }
    if (!visit || !body || !body.shopper) return;
    renderShopper(body.shopper);
    if (!greet || !body.greeting) return;
    if (!script.conversation || !started) { say(body.greeting); return; }
    const first = body.greeting.kind === "greeting_first";
    if (first) say(body.greeting); else setCaption(body.greeting.text, false);
    const live = await startConversation();
    if (!live && !first && visit) say(body.greeting); // no agent after all: the kiosk greets on its own
  }

  // Spoken lines about the cart replace an older one that has not started yet: the newest cart wins, and a
  // line already playing is never cut off.
  const NARRATION = ["pick", "put_back", "over_budget", "misplaced", "exit_reminder"];

  function onNarration(item) {
    if (!visit || !item) return;
    if (convState === "live" && conv) { sendContext(item.text); return; }
    if (convState === "connecting") { pendingContext.push(item.text); return; }
    dropQueued((it) => NARRATION.indexOf(it.kind) >= 0);
    say(item);
  }

  // --- the conversation (ElevenLabs agent, mode "kiosk") ---

  function loadSdk() {
    if (!sdkPromise) sdkPromise = import(SDK_URL).catch((e) => { sdkPromise = null; throw e; });
    return sdkPromise;
  }

  function sendContext(text) {
    try {
      conv.sendContextualUpdate("Store update for the shopper at the shelf (mention it briefly when it fits, " +
        "do not read it word for word): " + text);
    } catch (e) { /* the socket is closing; a cart line is not worth queueing */ }
  }

  function toolError(e) {
    return JSON.stringify({ error: (e && e.message) || "The store couldn't do that right now." });
  }

  // Names and parameters match the agent's client tools (docs/voice.md). Each acts on the current shopper.
  const clientTools = {
    async get_catalog() {
      try { return JSON.stringify((await kget("/api/kiosk/catalog")).products); } catch (e) { return toolError(e); }
    },
    async get_cart() {
      try { return JSON.stringify(await kget("/api/kiosk/cart")); } catch (e) { return toolError(e); }
    },
    async make_plan({ goal_text } = {}) {
      try {
        const plan = await kpost("/api/kiosk/plan", { goal_text: String(goal_text || "").slice(0, 300) });
        const bays = plan.items.length > 0 ? plan.bays : []; // plan_bays on /ws lights the shelf map below
        return JSON.stringify({
          goal_summary: plan.goal_summary,
          items: plan.items.map((i) => ({ name: i.name, qty: i.qty, unit_price_usd: i.unit_price_usd, reason: i.reason })),
          est_total_usd: plan.est_total_usd,
          budget_usd: plan.budget_usd,
          fits_budget: plan.fits_budget,
          bays_to_find: [...new Set(bays)].sort((a, b) => a - b).map(ShelfMap.cardOf),
          where_to_look: bays.length ? "Look for " + ShelfMap.baysText(bays) + ", they're glowing on the shelf map." : "",
        });
      } catch (e) { return toolError(e); }
    },
    async clear_plan() {
      try { await kdelete("/api/kiosk/plan"); return JSON.stringify({ ok: true }); } catch (e) { return toolError(e); }
    },
  };

  function activity() { lastActivity = Date.now(); }

  function listening() { setMode("listening", "Listening. Just talk to me."); }

  // Resolves true once the agent is connected, false if it could not start (voice off, no agent, no network).
  async function startConversation() {
    if (!script.conversation || !started || !visit || convState !== "off") return false;
    convState = "starting";
    const gen = ++convGen;
    let session, sdk;
    try {
      [session, sdk] = await Promise.all([kget("/api/kiosk/voice-session"), loadSdk()]);
    } catch (e) {
      console.warn("kiosk: conversation unavailable", e && (e.code || e.message));
      if (gen === convGen) { convState = "off"; pump(); }
      return false;
    }
    await whenQuiet();                    // the welcome finishes before the agent says hello
    if (gen !== convGen) return false;    // ended (or restarted) while we waited
    if (!visit) { convState = "off"; pump(); return false; }
    convState = "connecting";
    let c;
    try {
      c = await sdk.Conversation.startSession({
        signedUrl: session.signed_url,
        connectionType: "websocket",
        dynamicVariables: session.dynamic_variables,
        clientTools,
        onModeChange: ({ mode }) => {
          activity();
          if (convState !== "live") return;
          if (mode === "speaking") setMode("speaking", "Speaking..."); else listening();
        },
        onMessage: ({ message, role, source }) => {
          activity();
          if ((role || (source === "user" ? "user" : "agent")) === "agent" && message) setCaption(message, false);
        },
        onError: (message) => console.warn("kiosk voice", message),
        onDisconnect: (details) => endConversation(details && details.reason === "error" ? "error" : "disconnected"),
      });
    } catch (e) {
      console.warn("kiosk: conversation failed to start", e);
      if (gen === convGen) { convState = "off"; pump(); }
      return false;
    }
    if (gen !== convGen) { c.endSession().catch(() => {}); return false; } // the visit ended meanwhile
    conv = c;
    convState = "live";
    activity();
    listening();
    kpost("/api/kiosk/conversation", { active: true }).catch(() => {});
    const maxMs = (session.max_duration_s || CONV_MAX_MS / 1000) * 1000;
    const silenceMs = (session.silence_timeout_s || CONV_SILENCE_MS / 1000) * 1000;
    convTimers = [
      setTimeout(() => endConversation("max_duration"), maxMs),
      setInterval(() => { if (Date.now() - lastActivity > silenceMs) endConversation("silence"); }, 5000),
    ];
    pendingContext.splice(0).forEach(sendContext);
    return true;
  }

  function endConversation(reason) {
    if (convState === "off") return;
    const wasLive = convState === "live";
    convState = "off";
    convGen++;
    convTimers.forEach((t) => { clearTimeout(t); clearInterval(t); });
    convTimers = [];
    pendingContext = [];
    const c = conv;
    conv = null;
    if (c) c.endSession().catch(() => { /* already closed */ });
    if (wasLive) kpost("/api/kiosk/conversation", { active: false, reason: reason }).catch(() => {});
    setMode("idle", "SpeedMart guide");
    pump();                               // cart lines are spoken by the kiosk again
  }

  // --- visits (kiosk_visit on /ws) ---

  function setVisit(step) {
    const changed = !visit || visit.step !== (step || 3);
    visit = { step: step || 3 };
    document.body.classList.add("k-visit");
    stopOfferGlow();
    if ($("caption").classList.contains("is-hint") || (changed && !playing)) showHint();
    showSteps();
  }

  // The visit is over: nothing about the shopper stays on the screen or in the queue.
  function endVisit() {
    const hadVisit = !!visit;
    visit = null;
    document.body.classList.remove("k-visit");
    clearShopper();
    endConversation("visit_ended");
    dropQueued((it) => it.visit);
    if (playing && playing.visit) {
      playing.dropped = true;             // still loading its audio: it will not be shown
      if (stopPlaying) stopPlaying();
    }
    if (hadVisit) showHint();
    showSteps();
  }

  function onVisit(data) {
    const ev = data && data.event;
    if (ev === "entered" || ev === "sync") {
      cancelTour();
      setVisit(data.step);
      loadShopper(ev === "entered");
    } else if (ev === "exit_pending" || ev === "resumed") {
      if (ev === "exit_pending") endConversation("exit");
      setVisit(data.step);
    } else if (ev === "paid") {
      paidUntil = Date.now() + PAID_HOLD_MS;
      endVisit();
      clearTimeout(paidTimer);
      paidTimer = setTimeout(showSteps, PAID_HOLD_MS + 50);
    } else if (ev === "ended") {
      endVisit();
    }
  }

  function onMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "kiosk_visit") onVisit(msg.data);
    else if (msg.type === "kiosk_cart") renderShopper(msg.data);
    else if (msg.type === "kiosk_say") onNarration(msg.data);
    else if (msg.type === "shelf_activity") onShelfActivity();
    else if (msg.type === "store_status" && msg.data && !msg.data.occupied && visit) endVisit();
  }

  // On every (re)connect the server resends the visit in progress (kiosk_visit "sync"); start from "free".
  function onStatus(connected) {
    if (connected && visit) { visit = null; document.body.classList.remove("k-visit"); clearShopper(); showSteps(); }
  }

  // --- Start overlay ---

  async function unlock() {
    $("start").hidden = true;
    audioEl = new Audio();
    audioEl.preload = "auto";
    try { audioEl.src = SILENT_WAV; await audioEl.play(); } catch (e) { /* the tap still counts */ }
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) { const ctx = new Ctx(); await ctx.resume(); ctx.close(); }
    } catch (e) { /* not needed where HTMLAudioElement is enough */ }
    if (script.voice && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((t) => t.stop()); // the voice SDK opens its own stream later
      } catch (e) {
        console.warn("kiosk: microphone not available", e && e.name);
      }
    }
    started = true;
    if (!playing) showHint();
    showSteps();
    pump();
  }

  // --- boot ---

  // opts: {token, addExitCard()}. Resolves true in agent mode, false when the token is refused (public page).
  async function init(opts) {
    token = opts.token;
    // Only a refused token means "public page". A backend that is still starting is retried, so a tablet that
    // boots before the laptop still becomes the agent.
    for (;;) {
      try {
        script = await kget("/api/kiosk/phrases");
        break;
      } catch (e) {
        if (e && e.status >= 400 && e.status < 500) return false; // 403 kiosk_only (or no such route)
        await sleep(3000);
      }
    }
    document.body.classList.add("kiosk-agent");
    let gates = true;
    try { gates = !!(await api.config()).features.gates; } catch (e) { /* assume the gates are on */ }
    if (gates && opts.addExitCard) {                   // step 4's code, shown while someone is shopping
      opts.addExitCard();
      document.body.classList.add("k-has-exit");
    }
    $("tour-btn").addEventListener("click", startTour);
    window.addEventListener("pagehide", () => {  // a reloaded kiosk must not keep the phone's voice blocked
      if (convState !== "live") return;
      const body = new Blob([JSON.stringify({ active: false, reason: "unload" })], { type: "application/json" });
      navigator.sendBeacon(withToken("/api/kiosk/conversation"), body);
    });
    $("start").hidden = false;
    $("start").addEventListener("click", unlock, { once: true });
    showHint();
    showSteps();
    return true;
  }

  window.KioskAgent = { init, onMessage, onStatus, say, whenQuiet };
})();
