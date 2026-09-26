// Wraps SimpleWebAuthnBrowser (F7, 9.8). Bundle: @simplewebauthn/browser 13.3.0,
// dist/bundle/index.umd.min.js on jsDelivr (checked against the package's dist folder), global SimpleWebAuthnBrowser.
//
//   passkey.load()                       page init, only when features.passkeys is on
//   passkey.prefetch("register")         fetch options ahead of the tap
//   passkey.prefetch("login", purpose)   purpose: "enter" | "exit" | "login"
//   await passkey.register()             call inside a tap handler: Face ID prompt, then saves the passkey
//   await passkey.signIn(purpose)        call inside a tap handler: returns the member
//   passkey.friendlyError(err)           text for the error strip
//   passkey.showNotes()                  fills every [data-verify-note] next to a verification button
//
// Safari only shows the Face ID prompt from a user gesture, and a slow fetch between the tap and the
// prompt can use the gesture up. So options are fetched when the page loads (and again after every
// attempt); the tap then goes straight to the browser prompt.

(function () {
  const BUNDLE = "https://cdn.jsdelivr.net/npm/@simplewebauthn/browser@13.3.0/dist/bundle/index.umd.min.js";
  const OPTIONS_MAX_AGE_MS = 4 * 60 * 1000; // server keeps a challenge for 5 min
  // Face ID on iPhone, fingerprint or face on Android, a PIN on a laptop: the check happens on the device and
  // the backend only ever sees a signed yes (9.8).
  const VERIFY_NOTE = "Your face, fingerprint, or PIN stays on your phone. SpeedMart only receives a yes or no.";

  let loading = null;
  const cache = {}; // key -> {promise, at}

  function load() {
    if (window.SimpleWebAuthnBrowser) return Promise.resolve(window.SimpleWebAuthnBrowser);
    if (!loading) {
      loading = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = BUNDLE;
        s.async = true;
        s.onload = () => (window.SimpleWebAuthnBrowser ? resolve(window.SimpleWebAuthnBrowser)
          : reject(loadError()));
        s.onerror = () => { loading = null; s.remove(); reject(loadError()); };
        document.head.appendChild(s);
      });
      loading.catch(() => {}); // reported when a button is tapped
    }
    return loading;
  }

  function loadError() {
    const e = new Error("Face ID could not load. Check your connection and reload the page.");
    e.name = "LoadError";
    return e;
  }

  function optionsRequest(kind, purpose) {
    return kind === "register"
      ? api.post("/api/passkey/register/options")
      : api.post("/api/passkey/login/options", { purpose });
  }

  // One outstanding set of options per kind ("register" / "login"): the server keeps only the newest
  // challenge of each kind in the cookie, so a second prefetch would make the first set fail verification.
  function fresh(hit, purpose) {
    return hit && hit.purpose === purpose && Date.now() - hit.at < OPTIONS_MAX_AGE_MS;
  }

  function prefetch(kind, purpose) {
    const hit = cache[kind];
    if (fresh(hit, purpose)) return hit.promise;
    const p = optionsRequest(kind, purpose);
    p.catch(() => {});
    cache[kind] = { promise: p, at: Date.now(), purpose };
    return p;
  }

  // Prefetched options if fresh, else a new request. Each set of options is used once.
  function takeOptions(kind, purpose) {
    const hit = cache[kind];
    delete cache[kind];
    if (fresh(hit, purpose)) return hit.promise.catch(() => optionsRequest(kind, purpose));
    return optionsRequest(kind, purpose);
  }

  async function lib() {
    const swa = await load();
    if (!swa.browserSupportsWebAuthn()) {
      const e = new Error("unsupported");
      e.name = "NotSupportedError";
      throw e;
    }
    return swa;
  }

  // After a failure, options for the retry tap are fetched right away. Never after a success: the session
  // lives in the cookie, and a late prefetch response could overwrite the cookie of the request that
  // follows (e.g. bring back a fresh verification that approve just used up).
  async function register() {
    try {
      const swa = await lib();
      const optionsJSON = await takeOptions("register");
      const credential = await swa.startRegistration({ optionsJSON });
      await api.post("/api/passkey/register/verify", { credential });
    } catch (e) {
      prefetch("register");
      throw e;
    }
  }

  async function signIn(purpose) {
    try {
      const swa = await lib();
      const optionsJSON = await takeOptions("login", purpose);
      const credential = await swa.startAuthentication({ optionsJSON });
      const res = await api.post("/api/passkey/login/verify", { credential, purpose });
      return res.member;
    } catch (e) {
      prefetch("login", purpose);
      throw e;
    }
  }

  // The DOMException name, whether SimpleWebAuthn wrapped it (WebAuthnError.cause) or passed it through.
  function domName(err) {
    if (!err) return "";
    if (err.cause && err.cause.name) return err.cause.name;
    return err.name || "";
  }

  function friendlyError(err) {
    if (!err) return "Something went wrong. Please try again.";
    if (err instanceof api.ApiError) return err.message; // backend already speaks human
    const code = err.code || "";
    const name = domName(err);
    if (name === "LoadError") return err.message;
    if (code === "ERROR_CEREMONY_ABORTED" || name === "NotAllowedError" || name === "AbortError") {
      return "Face ID was cancelled. Tap the button to try again.";
    }
    if (code === "ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED" || name === "InvalidStateError") {
      return "This phone already has a passkey for you. Use \"Sign in with Face ID\".";
    }
    if (code === "ERROR_INVALID_DOMAIN" || code === "ERROR_INVALID_RP_ID" || name === "SecurityError") {
      return "Face ID only works on the store's https link. Scan the QR code again.";
    }
    if (name === "NotSupportedError" || code.indexOf("ERROR_AUTHENTICATOR_MISSING") === 0 ||
        code === "ERROR_AUTHENTICATOR_NO_SUPPORTED_PUBKEYCREDPARAMS_ALG") {
      return "This phone has no screen lock set up, use the demo account.";
    }
    return "Face ID didn't work. Tap the button to try again, or use the demo account.";
  }

  // Pages call this only when features.passkeys is on; with passkeys off the buttons say "Confirm" and the
  // notes stay empty (and hidden by app.css).
  function showNotes() {
    document.querySelectorAll("[data-verify-note]").forEach((el) => { el.textContent = VERIFY_NOTE; });
  }

  window.passkey = { load, prefetch, register, signIn, friendlyError, showNotes, VERIFY_NOTE };
})();
