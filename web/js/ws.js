// WebSocket to /ws with auto-reconnect (8.4): backoff 0.5 s doubling up to 4 s.
// After every (re)connect it calls GET /api/store/current so the page catches up on anything it missed.
//
//   const sock = connectSocket({
//     role: "admin",                 // optional; admin sockets also get shelf + log messages
//     onMessage(msg) {},             // {type, data}
//     onStatus(connected) {},        // live dot
//     onResync(current) {},          // result of GET /api/store/current after each (re)connect
//   });

(function () {
  const MIN_DELAY_MS = 500;
  const MAX_DELAY_MS = 4000;

  function connectSocket(opts) {
    const o = opts || {};
    let delay = MIN_DELAY_MS;
    let ws = null;
    let timer = null;

    function url() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      return proto + "//" + location.host + "/ws" + (o.role ? "?role=" + encodeURIComponent(o.role) : "");
    }

    function open() {
      clearTimeout(timer);
      ws = new WebSocket(url());

      ws.onopen = () => {
        delay = MIN_DELAY_MS;
        if (o.onStatus) o.onStatus(true);
        resync();
      };

      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (o.onMessage) o.onMessage(msg);
      };

      ws.onclose = () => {
        if (o.onStatus) o.onStatus(false);
        timer = setTimeout(open, delay);
        delay = Math.min(delay * 2, MAX_DELAY_MS);
      };

      ws.onerror = () => { try { ws.close(); } catch (e) { /* already closed */ } };
    }

    async function resync() {
      try {
        const current = await api.get("/api/store/current");
        if (o.onResync) o.onResync(current);
      } catch (e) {
        // The socket's own first messages also carry the cart; nothing else to do.
      }
    }

    open();
    return { reconnectNow: () => { if (ws) ws.close(); } };
  }

  window.connectSocket = connectSocket;
})();
