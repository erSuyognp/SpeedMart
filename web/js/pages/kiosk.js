// Entrance kiosk (web/kiosk.html) for the Dell Venue 10" tablet in landscape.
//
//   /kiosk.html              JOIN + ENTER codes side by side (entrance side)
//   /kiosk.html?side=exit    the EXIT code on its own (exit side)
//
// Renders four things and nothing else: the store name from /api/config/public, the public
// store_status and plan_bays messages from /ws (8.4), QR PNGs from /api/kiosk/qr/{which}, and the shelf
// map from /api/catalog. It never asks for /api/me and never reads a name out of a message, so no
// personal data can reach this screen: plan_bays carries bay numbers only.

(function () {
  var SIDE = new URLSearchParams(location.search).get("side") === "exit" ? "exit" : "entrance";

  // Step numbers match the printed codes from scripts/gen_qr.py: 1 JOIN, 2 ENTER, 3 EXIT.
  var CARDS = {
    entrance: [
      { which: "join", step: "1", label: "JOIN", caption: "New here? Scan to become a member." },
      { which: "enter", step: "2", label: "ENTER", caption: "Already a member? Scan to open the store." }
    ],
    exit: [
      { which: "exit", step: "3", label: "EXIT", caption: "Done shopping? Scan to review and approve." }
    ]
  };

  var STATUS = {
    open: {
      cls: "is-open",
      entrance: { text: "Open", sub: "The store is free. Scan a code to start." },
      exit: { text: "Open", sub: "Scan the exit code when you are done." }
    },
    busy: {
      cls: "is-busy",
      entrance: { text: "Someone is shopping", sub: "One shopper at a time. This takes a minute." },
      exit: { text: "Someone is shopping", sub: "Scan the exit code when you are done." }
    },
    offline: {
      cls: "is-offline",
      entrance: { text: "Reconnecting…", sub: "Waiting for the store." },
      exit: { text: "Reconnecting…", sub: "Waiting for the store." }
    }
  };

  var statusEl = document.getElementById("status");
  var statusText = document.getElementById("status-text");
  var statusSub = document.getElementById("status-sub");
  var codesEl = document.getElementById("codes");
  var SHELF_POLL_MS = 5000; // unit counts: the kiosk gets no cart messages, so it re-reads the catalog
  var shelf = null;

  // --- QR cards ---

  function buildCards() {
    CARDS[SIDE].forEach(function (card) {
      var el = document.createElement("div");
      el.className = "kiosk-code";
      el.innerHTML =
        '<div class="kiosk-step"><span class="num"></span><span class="label"></span></div>' +
        '<div class="kiosk-qr"><img alt=""><p class="kiosk-qr-fallback"></p></div>' +
        '<div class="kiosk-caption"></div>';
      el.querySelector(".num").textContent = card.step;
      el.querySelector(".label").textContent = card.label;
      el.querySelector(".kiosk-caption").textContent = card.caption;
      el.querySelector(".kiosk-qr-fallback").textContent =
        "QR code unavailable. Use the printed " + card.label + " code.";

      var img = el.querySelector("img");
      img.alt = card.label + " QR code";
      // Cache-busted once per page load: the PNG is cached server side, but a restart after a tunnel
      // domain change must not leave a stale code on a kiosk that has been up for hours.
      img.src = "/api/kiosk/qr/" + card.which + "?v=" + Date.now();
      img.onerror = function () { el.classList.add("qr-failed"); };

      codesEl.appendChild(el);
    });
  }

  // --- status banner ---

  var lastKey = null;

  function setStatus(key) {
    if (key === lastKey) return;
    lastKey = key;
    var s = STATUS[key];
    statusEl.classList.remove("is-open", "is-busy", "is-offline");
    statusEl.classList.add(s.cls);
    statusText.textContent = s[SIDE].text;
    statusSub.textContent = s[SIDE].sub;
  }

  // --- shelf map ---

  function setGlow(bays) {
    shelf.setGlow(bays);
    var hint = document.getElementById("shelf-hint");
    var on = bays.length > 0;
    hint.classList.toggle("is-glow", on);
    hint.textContent = on
      ? "Glowing: " + ShelfMap.baysText(bays) + (bays.length > 1 ? " are" : " is") + " on a shopper's list."
      : "Every bay has a number card on the shelf front.";
  }

  // --- clock ---

  function tickClock() {
    var now = new Date();
    document.getElementById("clock-time").textContent =
      now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    document.getElementById("clock-date").textContent =
      now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
    // Re-aim at the next minute boundary so the display never sits a minute behind.
    setTimeout(tickClock, 60000 - (now.getSeconds() * 1000 + now.getMilliseconds()) + 50);
  }

  // --- boot ---

  function start() {
    if (SIDE === "exit") {
      document.body.classList.add("kiosk-exit");
      document.title = "SpeedMart · Exit";
      document.getElementById("tagline").textContent = "Scan to review your cart and approve.";
    }

    buildCards();
    shelf = ShelfMap.create(document.getElementById("shelf-map"), { size: "large" });
    setInterval(function () { shelf.refresh(); }, SHELF_POLL_MS);
    setStatus("offline");
    tickClock();

    api.config().then(function (cfg) {
      if (cfg && cfg.store_name) {
        document.getElementById("store-name").textContent = cfg.store_name;
      }
    }).catch(function () {
      // Keep the markup default; the socket state is what matters on this screen.
    });

    // ws.js reconnects on its own with 0.5 s -> 4 s backoff and resyncs after each connect.
    connectSocket({
      // On (re)connect nothing glows until the server says so: it sends plan_bays first thing if a plan is up.
      onStatus: function (connected) { if (connected) setGlow([]); else setStatus("offline"); },
      onMessage: function (msg) {
        if (msg && msg.type === "store_status" && msg.data) {
          setStatus(msg.data.occupied ? "busy" : "open");
          shelf.refresh();
        } else if (msg && msg.type === "plan_bays" && msg.data) {
          setGlow(msg.data.bays || []);
          shelf.refresh();
        }
      }
    });
  }

  start();
})();
