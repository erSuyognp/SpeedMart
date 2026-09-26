// F19 voice store agent on intent.html: talk to an ElevenLabs agent that can only act through the store's tools.
// The API key stays on the backend; this page gets a short-lived signed URL from /api/voice/session.
// Every client tool is a thin wrapper over an existing endpoint and returns a short JSON string.
// Any failure (flag off, no mic, SDK blocked, session error) falls back to the text form on the same page.
// SDK API: see docs/voice.md (checked against @elevenlabs/client 1.25.0 type definitions).

const SDK_URL = "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm";
const MAX_SESSION_MS = 2 * 60 * 1000;

const $ = (id) => document.getElementById(id);
const page = () => window.intentPage; // set by intent.js, which loads first

let sdkPromise = null;
let conversation = null;
let active = false; // true from the tap until the session ends, fails or is cancelled
let stopTimer = null;

function loadSdk() {
  if (!sdkPromise) sdkPromise = import(SDK_URL).catch((e) => { sdkPromise = null; throw e; });
  return sdkPromise;
}

const STATE_TEXT = {
  idle: "Tap to talk. Up to 2 minutes.",
  connecting: "Connecting…",
  listening: "Listening. Say what you need.",
  speaking: "SpeedMart is speaking…",
  ended: "Conversation ended. Tap to talk again.",
};

function setState(state) {
  $("voice-state").dataset.state = state;
  $("voice-state-text").textContent = STATE_TEXT[state];
  const live = state === "connecting" || state === "listening" || state === "speaking";
  $("voice-btn").classList.toggle("live", live);
  $("voice-btn-label").textContent = live ? "End conversation" : "Talk to SpeedMart";
}

function addTurn(role, text) {
  if (!text) return;
  const list = $("voice-transcript");
  const li = document.createElement("li");
  li.className = role === "user" ? "user" : "agent";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role === "user" ? "You:" : "SpeedMart:";
  li.append(who, document.createTextNode(text));
  list.appendChild(li);
  list.hidden = false;
  list.scrollTop = list.scrollHeight;
}

function fail(message) {
  if (!active) return; // cancelled or already ended: stay quiet
  active = false;
  clearTimeout(stopTimer);
  const c = conversation;
  conversation = null;
  if (c) c.endSession().catch(() => {});
  setState("ended");
  page().showError(message);
  page().focusTextInput();
}

async function endConversation() {
  active = false;
  clearTimeout(stopTimer);
  const c = conversation;
  conversation = null;
  setState("ended");
  if (c) {
    try { await c.endSession(); } catch (e) { /* already closed */ }
  }
}

// --- client tools: names and parameters must match the agent config in docs/voice.md exactly ---

function toolError(e) {
  return JSON.stringify({ error: (e && e.message) || "The store couldn't do that right now." });
}

const clientTools = {
  async get_catalog() {
    try {
      const data = await api.get("/api/voice/catalog");
      return JSON.stringify(data.products);
    } catch (e) { return toolError(e); }
  },

  async get_cart() {
    try {
      const data = await api.get("/api/store/current");
      if (!data || !data.session) {
        return JSON.stringify({ in_store: false, items: [], note: "The shopper has not entered the store yet." });
      }
      const cart = data.cart;
      return JSON.stringify({
        in_store: true,
        items: cart.items.map((i) => ({ name: i.name, qty: i.qty, line_total_usd: i.line_total_usd })),
        total_usd: cart.total_usd,
        budget_usd: cart.budget_usd,
        over_budget: cart.over_budget,
      });
    } catch (e) { return toolError(e); }
  },

  async make_plan({ goal_text } = {}) {
    try {
      const plan = await api.post("/api/intent", { text: String(goal_text || "").slice(0, 300) });
      page().renderPlan(plan); // same cards as the text flow; the backend already lit the bays
      return JSON.stringify({
        goal_summary: plan.goal_summary,
        items: plan.items.map((i) => ({ name: i.name, qty: i.qty, unit_price_usd: i.unit_price_usd, reason: i.reason })),
        est_total_usd: plan.est_total_usd,
        budget_usd: plan.budget_usd,
        fits_budget: plan.fits_budget,
        blinking_on_shelf: plan.items.length > 0 && plan.bays.length > 0,
      });
    } catch (e) { return toolError(e); }
  },

  async clear_plan() {
    try {
      await fetch("/api/intent", { method: "DELETE", credentials: "same-origin" });
      page().showAsk();
      return JSON.stringify({ ok: true });
    } catch (e) { return toolError(e); }
  },
};

// --- session ---

async function micAllowed() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return "unsupported";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop()); // the SDK opens its own stream
    return "ok";
  } catch (e) {
    return e && (e.name === "NotAllowedError" || e.name === "SecurityError") ? "denied" : "error";
  }
}

async function startConversation() {
  active = true;
  page().showError("");
  $("voice-transcript").textContent = "";
  $("voice-transcript").hidden = true;
  setState("connecting");
  try {
    // Ask for the mic first, inside the tap, so iOS Safari shows its prompt.
    const mic = await micAllowed();
    if (!active) return; // tapped End while the mic prompt was up
    if (mic === "denied") return fail("Microphone is blocked. Allow it in your browser settings, or type below.");
    if (mic !== "ok") return fail("This browser can't use the microphone here. Please type what you need below.");

    let session, sdk;
    try {
      [session, sdk] = await Promise.all([api.get("/api/voice/session"), loadSdk()]);
    } catch (e) {
      if (e instanceof api.ApiError) return fail(e.message);
      return fail("Voice couldn't load on this network. Please type what you need below.");
    }
    if (!active) return;

    stopTimer = setTimeout(endConversation, MAX_SESSION_MS);
    const started = await sdk.Conversation.startSession({
      signedUrl: session.signed_url,
      connectionType: "websocket",
      dynamicVariables: {
        first_name: session.member_first_name,
        budget_usd: Number(session.budget_usd),
        dietary: session.dietary || "none",
      },
      clientTools,
      onModeChange: ({ mode }) => { if (active) setState(mode === "speaking" ? "speaking" : "listening"); },
      onStatusChange: ({ status }) => { if (status === "connected" && active) setState("listening"); },
      onMessage: ({ message, role, source }) => addTurn(role || (source === "user" ? "user" : "agent"), message),
      onError: (message) => {
        console.warn("voice", message);
        fail("Voice had a problem. Please type what you need below.");
      },
      onDisconnect: (details) => {
        if (details && details.reason === "error") {
          fail("Voice disconnected. Please type what you need below.");
          return;
        }
        if (active) endConversation(); // the agent hung up or the session hit its max duration
      },
    });
    if (!active) { started.endSession().catch(() => {}); return; } // tapped End, or it failed, while connecting
    conversation = started;
  } catch (e) {
    console.warn("voice", e);
    fail("Voice couldn't start. Please type what you need below.");
  }
}

async function init() {
  try {
    const cfg = await api.config();
    if (!cfg.features || !cfg.features.voice) return; // flag off: no button, text flow only
    await api.get("/api/me"); // signed out: intent.js shows the sign-in card, no voice
  } catch (e) {
    return;
  }
  $("voice").hidden = false;
  setState("idle");
  $("voice-btn").addEventListener("click", () => {
    if (active) endConversation(); else startConversation();
  });
  loadSdk().catch(() => { /* retried on tap; the tap path shows the friendly message */ });
}

init();
