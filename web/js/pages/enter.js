// Page script for enter.html?g=<token> (11.2, QR 2 · ENTER).
// States: not a member, ready, verifying, welcome, occupied, already inside, error.
// The WebAuthn prompt starts inside the button tap (Safari needs the user gesture).

(function () {
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("g") || "";
  let cfg = { features: {} };
  let me = null;

  const passkeysOn = () => !!cfg.features.passkeys;

  function view(o) {
    $("title").textContent = o.title || "";
    $("text").textContent = o.text || "";
    $("icon").className = "big-icon " + (o.icon || "brand");
    $("spinner").hidden = !o.busy;
    $("enter-btn").hidden = !o.button;
    if (o.button) $("enter-btn").textContent = o.button;
    $("cart-link").hidden = !o.cart;
    $("join-link").hidden = !o.join;
  }

  function setError(text) {
    $("error").textContent = text || "";
    $("error").hidden = !text;
  }

  function ready() {
    const label = passkeysOn() ? "Enter with Face ID" : "Enter";
    if (me) {
      view({ title: "Hi " + me.member.first_name, text: "Tap to open the door.", button: label });
    } else if (passkeysOn()) {
      view({ title: "Welcome", text: "Members tap to open the door.", button: label, join: true });
    } else {
      view({ title: "Members only", text: "Join first, then scan this code again.", join: true });
    }
    if (passkeysOn()) passkey.prefetch("login", "enter");
  }

  function welcome(firstName) {
    view({ title: "Welcome in, " + firstName, text: "Opening your cart…", icon: "ok", cart: true });
    $("icon").textContent = "✓";
    setTimeout(() => { location.href = "/store.html"; }, 1500);
  }

  async function onEnter(ev) {
    setError("");
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      let member = me && me.member;
      if (passkeysOn()) {
        member = await passkey.signIn("enter"); // Face ID first, straight from the tap
      }
      view({ title: "Verifying…", busy: true });
      await api.post("/api/gate/enter", { gate_token: token });
      welcome(member.first_name);
    } catch (e) {
      if (e instanceof api.ApiError && e.code === "store_occupied") {
        view({ title: "Someone is shopping right now", text: "Try again in a minute.", icon: "bad",
               button: "Try again" });
      } else if (e instanceof api.ApiError && e.code === "unknown_passkey") {
        view({ title: "We don't know this phone yet", text: "Join first, then scan this code again.",
               icon: "bad", join: true, button: "Try Face ID again" });
      } else {
        setError(e instanceof api.ApiError ? e.message : passkey.friendlyError(e));
        ready();
      }
    } finally {
      btn.disabled = false;
    }
  }

  async function init() {
    $("enter-btn").addEventListener("click", onEnter);
    try {
      cfg = await api.config();
      if (!cfg.features.gates) {
        view({ title: "Gates are off today", text: "Open your cart and tap Start shopping.", cart: true });
        return;
      }
      if (!token) {
        view({ title: "Scan the code again", text: "This link is missing the gate code.", icon: "bad" });
        return;
      }
      if (passkeysOn()) passkey.load();
      try { me = await api.get("/api/me"); } catch (e) { if (e.status !== 401) throw e; me = null; }
      if (me && me.active_session_id) {
        view({ title: "You're already inside", text: "Your cart is live.", icon: "ok", cart: true });
        return;
      }
      ready();
    } catch (e) {
      view({ title: "Can't reach the store", text: e.message, icon: "bad" });
    }
  }

  init();
})();
