// Page script for index.html (11.1, QR 1 · JOIN): signup, passkey setup, "Sign in with Face ID".
// Every WebAuthn call starts inside a button tap handler (Safari needs the user gesture).

(function () {
  const $ = (id) => document.getElementById(id);
  let cfg = { features: {} };
  let me = null; // GET /api/me result while signed in

  const passkeysOn = () => !!cfg.features.passkeys;
  const gatesOn = () => !!cfg.features.gates;

  function show(which) {
    ["loading", "join", "signup-off", "member"].forEach((id) => { $(id).hidden = id !== which; });
    // "Already a member?" only makes sense while signed out, and only with passkeys.
    $("signin").hidden = which === "member" || which === "loading" || !passkeysOn();
  }

  function setError(text) {
    $("error").textContent = text || "";
    $("error").hidden = !text;
  }

  function renderSignedOut() {
    me = null;
    $("join-btn").textContent = passkeysOn() ? "Join with Face ID" : "Join";
    show(cfg.features.signup ? "join" : "signup-off");
    if (passkeysOn()) passkey.prefetch("login", "login");
  }

  function renderMember() {
    const m = me.member;
    const needsPasskey = passkeysOn() && !me.has_passkey;
    $("member-icon").textContent = needsPasskey ? "!" : "✓";
    $("member-icon").className = "big-icon " + (needsPasskey ? "brand" : "ok");
    $("member-title").textContent = needsPasskey
      ? "One more step, " + m.first_name
      : "You're a member" + (m.is_demo ? " (Demo Shopper)." : ".");
    $("card-line").hidden = !m.card_label || needsPasskey;
    $("card-line").textContent = "Card linked: " + m.card_label + ".";

    let next;
    if (needsPasskey) next = "Set up Face ID so the gates know it's you. Your face never leaves your phone.";
    else if (me.active_session_id) next = "You're in the store.";
    else if (gatesOn()) next = "Walk to the entry gate.";
    else next = "Open your cart and tap Start shopping.";
    $("next-line").textContent = next;

    $("passkey-setup").hidden = !needsPasskey;
    $("cart-link").hidden = needsPasskey || (gatesOn() && !me.active_session_id);
    show("member");
    if (needsPasskey) passkey.prefetch("register");
  }

  async function loadMe() {
    try {
      me = await api.get("/api/me");
      renderMember();
    } catch (e) {
      if (e.status === 401) renderSignedOut();
      else throw e;
    }
  }

  // Runs inside a tap handler. Returns true when the passkey was saved.
  async function setupPasskey() {
    setError("");
    try {
      await passkey.register();
      me.has_passkey = true;
      renderMember();
      return true;
    } catch (e) {
      setError(passkey.friendlyError(e));
      $("setup-btn").textContent = "Try Face ID again";
      renderMember();
      return false;
    }
  }

  async function onJoin(ev) {
    ev.preventDefault();
    setError("");
    const name = $("name").value.trim();
    if (!name) { setError("Please enter your first name."); $("name").focus(); return; }
    const btn = $("join-btn");
    btn.disabled = true;
    try {
      const res = await api.post("/api/members/signup", {
        name,
        budget_usd: Number($("budget").value),
        dietary: $("dietary").value,
      });
      me = { member: res.member, has_passkey: false, active_session_id: null };
      if (passkeysOn()) await setupPasskey(); // same tap: Face ID prompt right after joining
      else renderMember();
    } catch (e) {
      setError(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function onSignIn(ev) {
    setError("");
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      await passkey.signIn("login");
      await loadMe();
    } catch (e) {
      setError(passkey.friendlyError(e)); // passkey.js already fetched options for the retry tap
    } finally {
      btn.disabled = false;
    }
  }

  async function onSetup(ev) {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try { await setupPasskey(); } finally { btn.disabled = false; }
  }

  async function onLogout() {
    setError("");
    try { await api.post("/api/logout"); } catch (e) { /* signed out either way */ }
    renderSignedOut();
  }

  async function init() {
    $("budget").addEventListener("input", () => {
      const el = $("budget");
      $("budget-out").textContent = "$" + el.value;
      el.style.setProperty("--range-pct", ((el.value - el.min) / (el.max - el.min) * 100) + "%");  // filled track
    });
    $("join-form").addEventListener("submit", onJoin);
    $("signin-btn").addEventListener("click", onSignIn);
    $("setup-btn").addEventListener("click", onSetup);
    $("logout-btn").addEventListener("click", onLogout);
    try {
      cfg = await api.config();
      if (passkeysOn()) { passkey.load(); passkey.showNotes(); }
      await loadMe();
    } catch (e) {
      show("loading");
      $("loading").textContent = e.message;
    }
  }

  init();
})();
