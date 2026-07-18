/* ==========================================================================
   Delta Trading — dashboard client (Phase 1)
   Connects to the backend market WebSocket, updates ticker cards in
   real time, flashes on price change, and drives the status bar.
   ========================================================================== */
(function () {
  "use strict";

  // ---- State -------------------------------------------------------------
  var ws = null;
  var wsConnected = false;      // browser <-> backend socket
  var upstreamConnected = false; // backend <-> Delta socket
  var reconnectTimer = null;
  var updateCount = 0;
  var lastLatencyMs = null;
  var prevSpot = {};            // symbol -> previous spot price
  var trades = [];              // recent trades for the chart symbol (newest first)
  var MAX_TRADES = 40;

  // ---- DOM handles -------------------------------------------------------
  var el = {
    connDot: document.getElementById("conn-dot"),
    connLabel: document.getElementById("conn-label"),
    statusBar: document.getElementById("status-bar"),
    statusWs: document.getElementById("status-ws"),
    statusLast: document.getElementById("status-last"),
    statusCount: document.getElementById("status-count"),
    statusLatency: document.getElementById("status-latency"),
  };

  // ---- Formatting helpers ------------------------------------------------
  function formatPrice(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var abs = Math.abs(v);
    var opts =
      abs >= 1000 ? { minimumFractionDigits: 2, maximumFractionDigits: 2 }
      : abs >= 1  ? { minimumFractionDigits: 2, maximumFractionDigits: 3 }
      :             { minimumFractionDigits: 4, maximumFractionDigits: 4 };
    return v.toLocaleString("en-US", opts);
  }

  function formatChange(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var sign = v > 0 ? "+" : v < 0 ? "" : "";
    var arrow = v > 0 ? " ▲" : v < 0 ? " ▼" : "";
    return sign + v.toFixed(2) + "%" + arrow;
  }

  function formatCompact(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var abs = Math.abs(v);
    if (abs >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (v / 1e3).toFixed(2) + "K";
    return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function clockTime(dt) {
    return dt.toLocaleTimeString("en-US", { hour12: false });
  }

  // ---- Flash animation on an element -------------------------------------
  function flash(node, dir) {
    if (!node) return;
    node.classList.remove("flash-up", "flash-down");
    // Force reflow so the animation can retrigger on rapid updates.
    void node.offsetWidth;
    node.classList.add(dir > 0 ? "flash-up" : "flash-down");
  }

  // ---- Ticker card update ------------------------------------------------
  function updateCard(t) {
    var card = document.getElementById("card-" + t.symbol);
    if (!card) return;

    var spot = t.spot_price;
    var prev = prevSpot[t.symbol];
    var dir = 0;
    if (typeof prev === "number" && typeof spot === "number") {
      if (spot > prev) dir = 1;
      else if (spot < prev) dir = -1;
    }
    if (typeof spot === "number") prevSpot[t.symbol] = spot;

    // Card border + arrow reflect tick direction.
    card.classList.remove("up", "down", "flat");
    card.classList.add(dir > 0 ? "up" : dir < 0 ? "down" : "flat");

    var arrowEl = card.querySelector('[data-field="arrow"]');
    if (arrowEl) {
      arrowEl.textContent = dir > 0 ? "▲" : dir < 0 ? "▼" : "—";
      arrowEl.classList.remove("up", "down");
      if (dir !== 0) arrowEl.classList.add(dir > 0 ? "up" : "down");
    }

    // Fields
    setField(card, "spot_price", formatPrice(t.spot_price));
    setField(card, "mark_price", formatPrice(t.mark_price));
    setField(card, "high", formatPrice(t.high));
    setField(card, "low", formatPrice(t.low));
    setField(card, "best_bid", formatPrice(t.best_bid));
    setField(card, "best_ask", formatPrice(t.best_ask));
    setField(card, "volume", formatCompact(t.volume));
    setField(card, "oi", formatCompact(t.oi));

    // 24h change with color class
    var changeEl = card.querySelector('[data-field="change_24h"]');
    if (changeEl) {
      changeEl.textContent = formatChange(t.change_24h);
      changeEl.classList.remove("up", "down", "flat");
      var c = t.change_24h;
      changeEl.classList.add(c > 0 ? "up" : c < 0 ? "down" : "flat");
    }

    // Updated time
    var when = t.timestamp ? new Date(t.timestamp / 1000) : new Date();
    setField(card, "updated", clockTime(when));

    // Flash the spot price on change.
    if (dir !== 0) flash(card.querySelector('[data-field="spot_price"]'), dir);
  }

  function setField(card, field, value) {
    var node = card.querySelector('[data-field="' + field + '"]');
    if (node) node.textContent = value;
  }

  // ---- Status bar / connection indicator ---------------------------------
  function refreshStatus() {
    var live = wsConnected && upstreamConnected;

    if (el.connDot) el.connDot.classList.toggle("connected", live);
    if (el.connLabel) el.connLabel.textContent = live ? "Connected" : "Disconnected";

    if (el.statusBar) {
      el.statusBar.classList.toggle("connected", live);
      el.statusBar.classList.toggle("disconnected", !live);
    }
    if (el.statusWs) {
      el.statusWs.textContent = !wsConnected
        ? "DISCONNECTED"
        : upstreamConnected
        ? "CONNECTED"
        : "WAITING FOR FEED";
    }
    if (el.statusCount) el.statusCount.textContent = String(updateCount);
    if (el.statusLatency) {
      el.statusLatency.textContent =
        lastLatencyMs === null ? "— ms" : lastLatencyMs + " ms";
    }
  }

  function markUpdate(t) {
    updateCount += 1;
    if (el.statusLast) el.statusLast.textContent = clockTime(new Date());
    if (t.timestamp) {
      var lat = Date.now() - t.timestamp / 1000; // ts is microseconds
      lastLatencyMs = Math.max(0, Math.round(lat));
    }
    refreshStatus();
  }

  // ---- Message handling --------------------------------------------------
  function onMessage(ev) {
    var msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (e) {
      return;
    }

    if (msg.type === "ticker") {
      updateCard(msg);
      markUpdate(msg);
      if (msg.symbol === "BTCUSD") updateMarketStats(msg);
    } else if (msg.type === "status") {
      upstreamConnected = msg.upstream === "connected";
      refreshStatus();
    } else if (msg.type === "candle") {
      if (window.DeltaChart && window.DeltaChart.onCandle) window.DeltaChart.onCandle(msg);
    } else if (msg.type === "trade") {
      addTrade(msg);
    } else if (msg.type === "trade_snapshot") {
      setTrades(msg.trades);
    }
  }

  // ---- Market stats panel (BTCUSD) --------------------------------------
  function setStat(field, value) {
    var n = document.querySelector('#market-stats [data-field="' + field + '"]');
    if (n) n.textContent = value;
  }
  function updateMarketStats(t) {
    setStat("open", formatPrice(t.open));
    setStat("high", formatPrice(t.high));
    setStat("low", formatPrice(t.low));
    setStat("close", formatPrice(t.close));
    setStat("volume", formatCompact(t.volume));
    setStat("turnover_usd", t.turnover_usd == null ? "—" : "$" + formatCompact(t.turnover_usd));
    setStat("oi", formatCompact(t.oi));
    setStat("oi_value_usd", t.oi_value_usd == null ? "—" : "$" + formatCompact(t.oi_value_usd));
    setStat("band_low", formatPrice(t.price_band_low));
    setStat("band_high", formatPrice(t.price_band_high));
    setStat("funding", t.funding_rate == null ? "—" : t.funding_rate.toFixed(4) + "%");
  }

  // ---- Recent trades tape ------------------------------------------------
  function tradeTime(ts) {
    if (!ts) return "";
    return new Date(ts / 1000).toLocaleTimeString("en-US", { hour12: false });
  }
  function renderTrades() {
    var list = document.getElementById("trades-list");
    if (!list) return;
    var html = "";
    for (var i = 0; i < trades.length && i < 12; i++) {
      var tr = trades[i];
      var cls = tr.side === "buy" ? "buy" : "sell";
      html +=
        '<div class="trade-row ' + cls + '">' +
        '<span class="t-price">' + formatPrice(tr.price) + "</span>" +
        '<span class="t-size">' + formatCompact(tr.size) + "</span>" +
        '<span class="t-time">' + tradeTime(tr.timestamp) + "</span>" +
        "</div>";
    }
    list.innerHTML = html;
  }
  function addTrade(t) {
    trades.unshift(t);
    if (trades.length > MAX_TRADES) trades.length = MAX_TRADES;
    renderTrades();
  }
  function setTrades(list) {
    trades = (list || []).slice(0, MAX_TRADES);
    renderTrades();
  }

  // ---- WebSocket lifecycle ----------------------------------------------
  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws/market";

    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      wsConnected = true;
      refreshStatus();
    };

    ws.onmessage = onMessage;

    ws.onclose = function () {
      wsConnected = false;
      upstreamConnected = false;
      refreshStatus();
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onclose will follow and handle the reconnect.
      try { ws.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, 3000); // retry every 3 seconds
  }

  // ---- Boot --------------------------------------------------------------
  refreshStatus();
  connect();
})();
