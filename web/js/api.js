// Fetch helpers and error toasts. Every error from the backend is {"error": code, "message": text}.

(function () {
  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function request(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(0, "network", "Can't reach the store. Check the WiFi.");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      throw new ApiError(res.status, (data && data.error) || "http_" + res.status,
        (data && data.message) || "Something went wrong (" + res.status + ").");
    }
    return data;
  }

  let toastTimer = null;
  function toast(text) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast hide";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.remove("hide");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hide"), 3500);
  }

  let configPromise = null;
  function config() {
    if (!configPromise) {
      configPromise = request("GET", "/api/config/public").catch((e) => { configPromise = null; throw e; });
    }
    return configPromise;
  }

  function money(usd) {
    return "$" + Number(usd).toFixed(2);
  }

  window.api = {
    ApiError,
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body === undefined ? {} : body),
    toast,
    config,
    money,
  };
})();
