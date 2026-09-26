// Wraps SimpleWebAuthnBrowser (F7). Stub until S3.3: every call fails like an unsupported phone.
(function () {
  function unsupported() {
    const e = new Error("Face ID sign-in is not set up yet. Use the demo account.");
    e.name = "NotSupportedError";
    return Promise.reject(e);
  }
  window.passkey = {
    load() {},
    prefetch() {},
    register: unsupported,
    signIn: unsupported,
    friendlyError: (e) => (e && e.message) || "Something went wrong.",
  };
})();
