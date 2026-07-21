/* Phase 7 — system health strip + critical-event desktop notifications */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  if (!$("health")) return;

  let notifiedIds = new Set();
  let firstPoll = true;

  function notify(title, body) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      new Notification(title, { body, icon: "/static/favicon.ico" });
    } catch { /* notifications are best-effort */ }
  }

  function eventKey(e) {
    return `${e.ts}|${e.kind}|${e.symbol || ""}|${e.strategy || ""}`;
  }

  async function poll() {
    let d;
    try {
      d = await (await fetch("/api/health")).json();
    } catch {
      return;
    }

    const eng = d.engine || {};
    const auth = d.auth || {};
    const iv = d.iv_collection || {};

    // Overall state
    const dot = $("h-dot");
    const state = $("h-state");
    let cls = "ok", label = "All systems normal";
    if (!auth.ok) { cls = "bad"; label = "API auth failing"; }
    else if (eng.last_error) { cls = "warn"; label = "Engine reported an error"; }
    else if (d.problem_count > 0) { cls = "warn"; label = `${d.problem_count} recent issue(s)`; }
    dot.className = "dot " + cls;
    state.textContent = label;

    $("h-engine").textContent = eng.running
      ? `running · ${eng.enabled_strategies}/8 on`
      : "stopped";
    $("h-engine").style.color = eng.running ? "var(--success)" : "var(--text-dim)";
    $("h-mode").textContent = (eng.mode || "—").replace("_", " ");
    $("h-mode").style.color = eng.mode === "live_demo" ? "var(--danger)" : "var(--primary)";
    $("h-pos").textContent = `${eng.open_positions ?? 0} / ${eng.pending_orders ?? 0}`;
    $("h-iv").textContent = iv.rows ? `${iv.rows} · ${iv.span_days}d` : "0";
    $("h-trades").textContent = d.trades_recorded ?? 0;

    // Warning line
    const warn = $("h-warn");
    if (!auth.ok) {
      warn.style.display = "block";
      warn.className = "hwarn bad";
      warn.innerHTML = `<strong>Auth failed:</strong> ${auth.client_ip ? `IP <code>${auth.client_ip}</code> is not whitelisted. ` : ""}${auth.hint || auth.error || ""}`;
    } else if (eng.mode === "live_demo" && eng.running) {
      warn.style.display = "block";
      warn.className = "hwarn";
      warn.innerHTML = "<strong>Live demo trading is active</strong> — real orders are being placed on the testnet book.";
    } else {
      warn.style.display = "none";
    }

    // Desktop notifications for new critical events (skip the first poll so we
    // don't replay history on page load).
    const problems = d.recent_problems || [];
    if (firstPoll) {
      problems.forEach((e) => notifiedIds.add(eventKey(e)));
      firstPoll = false;
    } else {
      problems.forEach((e) => {
        const k = eventKey(e);
        if (notifiedIds.has(k)) return;
        notifiedIds.add(k);
        const what =
          e.kind === "order_error" ? "Order error"
          : e.kind === "reconcile_mismatch" ? "Position mismatch"
          : "Engine error";
        notify(`Delta Trading — ${what}`, (e.error || e.note || "").slice(0, 160));
      });
      if (notifiedIds.size > 300) notifiedIds = new Set([...notifiedIds].slice(-150));
    }
  }

  // Ask once, only on a real user gesture (browsers ignore unprompted requests).
  if ("Notification" in window && Notification.permission === "default") {
    document.addEventListener(
      "click",
      () => Notification.requestPermission().catch(() => {}),
      { once: true }
    );
  }

  // Live health frames pushed by the server (~every 5s) — no polling.
  AppWS.on("health", (h) => {
    if (!h) return;
    const eng = h.engine || {};
    const iv = h.iv_collection || {};
    $("h-engine").textContent = eng.running
      ? `running · ${eng.enabled_strategies}/9 on` : "stopped";
    $("h-engine").style.color = eng.running ? "var(--success)" : "var(--text-dim)";
    $("h-mode").textContent = (eng.mode || "—").replace("_", " ");
    $("h-mode").style.color = eng.mode === "live_demo" ? "var(--danger)" : "var(--primary)";
    $("h-pos").textContent = `${eng.open_positions ?? 0} / ${eng.pending_orders ?? 0}`;
    $("h-iv").textContent = iv.rows ? `${iv.rows} · ${iv.span_days}d` : "0";
    $("h-trades").textContent = h.trades_recorded ?? 0;
  });

  // Live-feed connection state drives the status dot.
  AppWS.onStatus((s) => {
    const dot = $("h-dot"), state = $("h-state");
    if (!dot || !state) return;
    if (s === "connected") return;             // /api/health poll sets the real state
    dot.className = "dot " + (s === "disconnected" ? "bad" : "warn");
    state.textContent = s === "reconnecting" ? "Reconnecting to live feed…"
      : s === "disconnected" ? "Live feed lost" : "Connecting…";
  });

  // One authoritative poll for auth status (needs a signed API probe), then push.
  poll();
  setInterval(poll, 60000);
})();
