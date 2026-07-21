/* Application-state WebSocket client.
 *
 * One socket per page carries every state topic (engine, journal, health,
 * alerts), replacing all REST polling. Handles:
 *   - automatic reconnect with exponential backoff + jitter
 *   - heartbeat ping/pong with a stall detector (catches half-open sockets that
 *     TCP happily keeps "open" while no data flows)
 *   - connection-status broadcasting for the UI
 *   - an outbound queue so sends during a reconnect are not lost
 *   - topic subscriptions, so pages only render what they care about
 *
 * Usage:
 *   AppWS.on('engine', d => render(d));
 *   AppWS.onStatus(s => showDot(s));
 */
window.AppWS = (() => {
  "use strict";

  const listeners = new Map();     // topic -> Set(fn)
  const statusFns = new Set();
  const outbox = [];               // queued sends while disconnected

  let ws = null;
  let status = "connecting";
  let attempt = 0;
  let heartbeatTimer = null;
  let stallTimer = null;
  let lastMessageAt = 0;
  let closedByUs = false;

  const HEARTBEAT_MS = 15000;      // ping cadence
  const STALL_MS = 45000;          // no traffic for this long => force reconnect

  function setStatus(s) {
    if (s === status) return;
    status = s;
    statusFns.forEach((fn) => { try { fn(s); } catch {} });
  }

  function emit(topic, data) {
    const set = listeners.get(topic);
    if (!set) return;
    set.forEach((fn) => {
      try { fn(data); } catch (e) { console.error("[AppWS]", topic, e); }
    });
  }

  function flushOutbox() {
    while (outbox.length && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(outbox.shift());
    }
  }

  function startTimers() {
    stopTimers();
    heartbeatTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, HEARTBEAT_MS);
    stallTimer = setInterval(() => {
      // Nothing received in STALL_MS despite pinging: the socket is half-open.
      if (lastMessageAt && Date.now() - lastMessageAt > STALL_MS) {
        console.warn("[AppWS] stalled, forcing reconnect");
        try { ws && ws.close(); } catch {}
      }
    }, 5000);
  }

  function stopTimers() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    if (stallTimer) clearInterval(stallTimer);
    heartbeatTimer = stallTimer = null;
  }

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    setStatus(attempt === 0 ? "connecting" : "reconnecting");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws/app`);
    } catch {
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      attempt = 0;
      lastMessageAt = Date.now();
      setStatus("connected");
      startTimers();
      flushOutbox();
    };

    ws.onmessage = (ev) => {
      lastMessageAt = Date.now();
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "pong") return;
      if (msg.type === "batch" && Array.isArray(msg.frames)) {
        msg.frames.forEach((f) => emit(f.topic, f.data));
      } else if (msg.topic) {
        emit(msg.topic, msg.data);
      }
    };

    ws.onerror = () => { /* onclose always follows; handled there */ };

    ws.onclose = () => {
      stopTimers();
      if (closedByUs) { setStatus("closed"); return; }
      setStatus("disconnected");
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    attempt += 1;
    // Exponential backoff capped at 15s, with jitter so many tabs don't
    // reconnect in lockstep and stampede the server.
    const base = Math.min(15000, 500 * Math.pow(2, Math.min(attempt, 5)));
    const delay = base * (0.7 + Math.random() * 0.6);
    setTimeout(connect, delay);
  }

  // Reconnect immediately when the tab becomes visible again — browsers
  // throttle background timers, so a backgrounded tab can sit disconnected.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && (!ws || ws.readyState !== WebSocket.OPEN)) {
      attempt = 0;
      connect();
    }
  });
  window.addEventListener("online", () => { attempt = 0; connect(); });

  connect();

  return {
    on(topic, fn) {
      if (!listeners.has(topic)) listeners.set(topic, new Set());
      listeners.get(topic).add(fn);
      return () => listeners.get(topic).delete(fn);
    },
    onStatus(fn) { statusFns.add(fn); fn(status); return () => statusFns.delete(fn); },
    send(obj) {
      const s = typeof obj === "string" ? obj : JSON.stringify(obj);
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(s);
      else outbox.push(s);          // queued, flushed on reconnect
    },
    status: () => status,
    close() { closedByUs = true; stopTimers(); try { ws && ws.close(); } catch {} },
  };
})();
