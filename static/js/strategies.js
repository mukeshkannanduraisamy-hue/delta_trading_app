/* Phase 4 — strategy engine control panel */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v) => (v === null || v === undefined ? "—" : "$" + fmt(v, 2));

  let strategiesRendered = false;

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    return r.json();
  }

  function renderStrategyCards(strats) {
    const wrap = $("strat-cards");
    wrap.innerHTML = "";
    strats.forEach((s) => {
      const card = document.createElement("div");
      card.className = "scard" + (s.enabled ? " on" : "");
      card.innerHTML = `
        <div class="scard-head">
          <span class="scard-title">${s.title}</span>
          <span class="scard-badges">
            <span class="badge tf">${s.timeframe}</span>
            <span class="badge basis-${s.basis}">${s.basis === "premium" ? "premium" : "underlying"}</span>
          </span>
          <label class="switch" style="margin-left:8px;">
            <input type="checkbox" data-slug="${s.slug}" ${s.enabled ? "checked" : ""}>
            <span class="slider"></span>
          </label>
        </div>
        <div class="scard-desc">${s.description}</div>`;
      wrap.appendChild(card);
    });
    wrap.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.addEventListener("change", async (e) => {
        const slug = e.target.dataset.slug;
        await post("/api/strategy/toggle", { slug, enabled: e.target.checked });
        e.target.closest(".scard").classList.toggle("on", e.target.checked);
        refresh();
      });
    });
    strategiesRendered = true;
  }

  function renderPositions(positions) {
    const body = $("pos-body");
    if (!positions || !positions.length) {
      body.innerHTML = `<tr><td colspan="10" class="empty">No open positions.</td></tr>`;
      return;
    }
    body.innerHTML = positions
      .map((p) => {
        const up = p.unrealized_pnl;
        const cls = up > 0 ? "pos" : up < 0 ? "neg" : "";
        const age = p.age_sec ? Math.round(p.age_sec) + "s" : "—";
        return `<tr>
          <td>${p.strategy}</td>
          <td><span class="tag ${p.direction === "CE" ? "ce" : "pe"}">${p.direction}</span></td>
          <td style="color:var(--text-dim)">${p.symbol}</td>
          <td>${fmt(p.strike, 0)}</td>
          <td>${fmt(p.entry_price, 2)}</td>
          <td>${fmt(p.current_price, 2)}</td>
          <td class="pos">${fmt(p.target, 2)}</td>
          <td class="neg">${fmt(p.stop, 2)}</td>
          <td class="${cls}">${up === null || up === undefined ? "—" : money(up)}</td>
          <td style="color:var(--text-dim)">${age}</td>
        </tr>`;
      })
      .join("");
  }

  function describeEvent(e) {
    switch (e.kind) {
      case "open":
        return `${e.strategy} → BUY ${e.direction} ${e.symbol} @ ${fmt(e.entry_price, 2)} (tgt ${fmt(e.target, 2)} / stop ${fmt(e.stop, 2)}) — ${e.reason}`;
      case "close":
        return `${e.strategy} CLOSE ${e.symbol} @ ${fmt(e.exit_price, 2)} · net ${money(e.net_pnl)} [${e.why}]`;
      case "order_error":
        return `ORDER ERROR ${e.symbol || ""}: ${e.error}`;
      case "error":
        return `ERROR ${e.strategy || e.scope || ""}: ${e.error}`;
      case "skip":
        return `skip ${e.strategy} ${e.direction || ""}: ${e.reason}`;
      case "config":
        return `${e.strategy} ${e.enabled ? "enabled" : "disabled"}`;
      case "engine":
        return `engine ${e.event}`;
      default:
        return JSON.stringify(e);
    }
  }

  function renderFeed(events) {
    const feed = $("feed");
    if (!events || !events.length) {
      feed.innerHTML = `<div class="empty">No events yet.</div>`;
      return;
    }
    feed.innerHTML = events
      .map((e) => {
        const t = new Date((e.ts || 0) * 1000).toLocaleTimeString("en-GB");
        return `<div class="frow">
          <span class="fkind ${e.kind}">${e.kind}</span>
          <span class="ftime">${t}</span>
          <span class="ftext">${describeEvent(e)}</span>
        </div>`;
      })
      .join("");
  }

  async function refresh() {
    let st;
    try {
      st = await (await fetch("/api/strategy/status")).json();
    } catch {
      return;
    }
    const cfg = st.config || {};

    // Engine state
    $("estate-dot").classList.toggle("on", st.running);
    $("estate-text").textContent = st.running ? "Running" : "Stopped";
    $("btn-start").disabled = st.running;
    $("btn-stop").disabled = !st.running;

    const live = cfg.live_demo;
    const badge = $("mode-badge");
    badge.textContent = live ? "LIVE DEMO" : "PAPER";
    badge.className = "badge " + (live ? "live" : "paper");

    const note = $("mode-note");
    if (live) {
      note.innerHTML = "⚠ <strong>LIVE DEMO mode</strong> — real orders are placed on the Delta testnet demo book. No real money, but real order flow.";
    } else {
      note.innerHTML = "🧪 <strong>PAPER mode</strong> — fills are simulated against the live premium; no orders are sent. Set <code>EXECUTION_MODE=live_demo</code> in .env to place real demo orders.";
    }

    $("m-asset").textContent = cfg.asset || "—";
    $("m-spot").textContent = st.spot ? fmt(st.spot, 1) : "—";
    $("m-cycles").textContent = st.cycles ?? 0;
    $("m-signals").textContent = st.signals_generated ?? 0;

    // Stats
    const s = st.stats || {};
    $("s-balance").textContent = money(s.balance);
    const pnlEl = $("s-pnl");
    pnlEl.textContent = money(s.realized_pnl);
    pnlEl.className = "v " + (s.realized_pnl > 0 ? "pos" : s.realized_pnl < 0 ? "neg" : "");
    $("s-winrate").textContent = s.win_rate === null || s.win_rate === undefined ? "—" : s.win_rate + "%";
    $("s-open").textContent = s.open_positions ?? 0;
    $("s-closed").textContent = s.closed_trades ?? 0;
    $("s-wl").textContent = `${s.wins ?? 0} / ${s.losses ?? 0}`;

    // Strategies (render once, then just sync the count)
    if (!strategiesRendered) renderStrategyCards(st.strategies || []);
    const on = (st.strategies || []).filter((x) => x.enabled).length;
    $("enabled-count").textContent = `${on} of ${(st.strategies || []).length} enabled`;

    renderPositions(st.positions);

    // Feed
    try {
      const j = await (await fetch("/api/strategy/journal?limit=60")).json();
      renderFeed(j.events);
    } catch {}
  }

  async function loadAccount() {
    const body = $("acct-body");
    body.innerHTML = `<div class="empty">Loading…</div>`;
    try {
      const a = await (await fetch("/api/account")).json();
      if (a.balances_error) {
        body.innerHTML = `<div class="empty">Balances error: ${a.balances_error}</div>`;
        return;
      }
      const rows = (a.balances || [])
        .map((b) => `<tr><td>${b.asset}</td><td>${fmt(b.balance, 4)}</td><td style="color:var(--text-dim)">${fmt(b.available, 4)}</td></tr>`)
        .join("");
      body.innerHTML = `<table class="t"><thead><tr><th>Asset</th><th>Balance</th><th>Available</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="3" class="empty">No non-zero balances.</td></tr>`}</tbody></table>`;
    } catch (e) {
      body.innerHTML = `<div class="empty">Failed to load account.</div>`;
    }
  }

  // Wire controls
  $("btn-start").addEventListener("click", async () => { await post("/api/strategy/start"); refresh(); });
  $("btn-stop").addEventListener("click", async () => { await post("/api/strategy/stop"); refresh(); });
  $("btn-flatten").addEventListener("click", async () => {
    if (confirm("Close all open positions now?")) { await post("/api/strategy/flatten"); refresh(); }
  });
  $("btn-acct").addEventListener("click", loadAccount);

  refresh();
  setInterval(refresh, 4000);
})();
