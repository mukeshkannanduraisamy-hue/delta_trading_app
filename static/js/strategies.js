/* Phase 4 — strategy engine control panel */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v) =>
    v === null || v === undefined ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v), 2);

  let strategiesRendered = false;
  let feedCache = [];

  // Most recent /api/strategy/status payload, used by the Start confirmation.
  let lastStatus = null;

  function applyStats(s) {
    // Every headline number here is sourced from the exchange or from the
    // durable SQLite record — never from the engine's in-memory counters,
    // which reset on restart and used to render "$0.00 / 0 closed" beside an
    // account that plainly had a trade history.
    const bal = s.equity_usd ?? s.balance;
    $("s-balance").textContent = bal === null || bal === undefined ? "—" : money(bal);
    const av = $("s-avail");
    if (av) av.textContent = s.available_usd === null || s.available_usd === undefined
      ? "—" : money(s.available_usd);

    const pnlEl = $("s-pnl");
    pnlEl.textContent = money(s.realized_pnl);
    pnlEl.className = "v " + (s.realized_pnl > 0 ? "pos" : s.realized_pnl < 0 ? "neg" : "");

    $("s-winrate").textContent = s.win_rate === null || s.win_rate === undefined ? "—" : s.win_rate + "%";
    $("s-open").textContent = s.open_positions ?? 0;
    $("s-closed").textContent = s.closed_trades ?? 0;
    $("s-wl").textContent = `${s.wins ?? 0} / ${s.losses ?? 0}`;

    const sy = $("s-sync");
    if (sy) {
      const age = s.account_age_sec;
      const stale = s.account_stale;
      sy.textContent = age === null || age === undefined ? "—" : Math.round(age) + "s";
      sy.className = "v " + (stale ? "neg" : "pos");
    }
    const src = $("s-source");
    if (src) {
      const drift = (s.exchange_positions ?? 0) !== (s.open_positions ?? 0);
      src.innerHTML =
        `balance & positions from exchange · P&amp;L from ${s.closed_trades ?? 0} ` +
        `recorded ${s.mode || ""} trade(s)` +
        (drift ? ` · <span style="color:var(--red,#F85149)">drift: exchange holds ` +
          `${s.exchange_positions ?? 0}, engine tracks ${s.open_positions ?? 0}</span>` : "");
    }
  }

  function applyRunning(running) {
    $("estate-dot").classList.toggle("on", running);
    $("estate-text").textContent = running ? "Running" : "Stopped";
    $("btn-start").disabled = running;
    $("btn-stop").disabled = !running;
  }

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    return r.json();
  }

  function aiPanel(ai) {
    const d = ai.last_decision;
    const used = ai.model_used
      ? `<span style="color:var(--success)">${ai.model_used.split("/").pop()}</span>`
      : `<span style="color:var(--text-dim)">not called yet</span>`;
    let decision = `<span style="color:var(--text-dim)">no decision yet</span>`;
    if (d && d.error) {
      decision = `<span style="color:var(--danger)">error: ${String(d.error).slice(0, 70)}</span>`;
    } else if (d) {
      const dirCls = d.direction === "CE" ? "ce" : d.direction === "PE" ? "pe" : "";
      decision =
        `<span class="tag ${dirCls}">${d.direction}</span> ` +
        `conf ${(d.confidence * 100).toFixed(0)}% · ${Math.round(d.age_sec)}s ago` +
        (d.reason ? `<br><span style="color:var(--text-dim)">${d.reason}</span>` : "");
    }
    return `<div style="border-top:1px solid var(--border);margin-top:6px;padding-top:8px;font-size:11.5px;line-height:1.6;">
      <div>model: ${used}${ai.has_key ? "" : ' <span style="color:var(--danger)">(no API key)</span>'}</div>
      <div>calls ${ai.calls} · errors ${ai.errors}</div>
      <div>${decision}</div>
    </div>`;
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
        <div class="scard-desc">${s.description}</div>
        ${s.ai ? aiPanel(s.ai) : ""}`;
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
    // Cached for the Start confirmation, which must echo the real trade size
    // and armed-strategy count rather than guess at them.
    lastStatus = st;

    // Engine state
    applyRunning(st.running);

    // Paper mode was removed from the engine — there is only one execution
    // path now, so the badge is not conditional. Rendering a "PAPER" state the
    // backend can never be in would be worse than useless: it is the exact
    // misreading that gets someone to click Start thinking it is a dry run.
    const badge = $("mode-badge");
    badge.textContent = "LIVE DEMO";
    badge.className = "badge live";
    $("acct-title").textContent = "Delta Account (testnet)";

    // The account arrives by WebSocket push now (server-side sync loop), so no
    // polling timer is needed — one initial paint covers a fresh page load
    // before the first push lands.
    if (!window.__acctSeeded) {
      window.__acctSeeded = true;
      fetch("/api/account").then((r) => r.json()).then(renderAccount).catch(() => {});
    }

    $("mode-note").innerHTML =
      "⚠ <strong>LIVE ONLY</strong> — starting the engine places <strong>real orders</strong> " +
      "on the Delta testnet demo book at <strong>" + (cfg.contracts ?? "?") +
      "</strong> contract(s) per trade. There is no paper mode and no dry run. " +
      "No real money is at risk, but the order flow is genuine.";

    $("m-asset").textContent = cfg.asset || "—";
    $("m-spot").textContent = st.spot ? fmt(st.spot, 1) : "—";
    $("m-cycles").textContent = st.cycles ?? 0;
    $("m-signals").textContent = st.signals_generated ?? 0;

    // Stats
    applyStats(st.stats || {});

    // Strategies: render once, but keep the AI panel live (it updates async).
    if (!strategiesRendered) renderStrategyCards(st.strategies || []);
    else {
      const ai = (st.strategies || []).find((s) => s.ai);
      const card = ai && document.querySelector(`.scard input[data-slug="${ai.slug}"]`)?.closest(".scard");
      if (card) {
        const panel = card.lastElementChild;
        if (panel && panel.style.borderTop !== undefined) panel.outerHTML = aiPanel(ai.ai);
      }
    }
    const on = (st.strategies || []).filter((x) => x.enabled).length;
    $("enabled-count").textContent = `${on} of ${(st.strategies || []).length} enabled`;

    renderPositions(st.positions);

    // Seed the feed once; subsequent events arrive by WebSocket push.
    if (!feedCache.length) {
      try {
        const j = await (await fetch("/api/strategy/journal?limit=60")).json();
        feedCache = j.events || [];
        renderFeed(feedCache);
      } catch {}
    }
  }

  // ---- Delta account (exchange = source of truth) --------------------------
  // Rendered from the `account` WebSocket topic, which the server refreshes on
  // its own schedule whether or not the engine is running. loadAccount() is now
  // only the manual-refresh path.
  function renderAccount(a) {
    const body = $("acct-body");
    if (!a) return;
    if (a.errors && (a.errors.balances || a.errors.positions)) {
      body.innerHTML = `<div class="empty">Sync error: ${
        a.errors.balances || a.errors.positions}</div>`;
      return;
    }
    const age = a.synced_at ? Math.max(0, Math.round(Date.now() / 1000 - a.synced_at)) : null;
    const stale = age === null || age > 60;
    const rows = (a.balances || [])
      .map((b) => `<tr><td>${b.asset}</td><td>${fmt(b.balance, 4)}</td>
        <td style="color:var(--text-dim)">${fmt(b.available, 4)}</td></tr>`)
      .join("");
    const posRows = (a.positions || [])
      .map((p) => {
        const pid = p.product_id || (p.product && p.product.id) || "—";
        const sym = (p.product && p.product.symbol) || p.symbol || pid;
        return `<tr><td>${sym}</td><td>${p.size}</td>
          <td style="color:var(--text-dim)">${fmt(p.entry_price, 2)}</td></tr>`;
      })
      .join("");
    body.innerHTML = `
      <div style="font-size:11px;color:${stale ? "var(--red,#F85149)" : "var(--text-dim)"};margin-bottom:6px">
        ${stale ? "⚠ STALE" : "synced"} ${age !== null ? age + "s ago" : ""}
        · ${a.position_count ?? 0} position(s) · ${a.open_order_count ?? 0} resting order(s)
      </div>
      <table class="t"><thead><tr><th>Asset</th><th>Balance</th><th>Available</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="3" class="empty">No non-zero balances.</td></tr>`}</tbody></table>
      ${posRows ? `<div style="margin-top:8px;font-size:11px;color:var(--text-dim)">Exchange positions</div>
        <table class="t"><thead><tr><th>Contract</th><th>Size</th><th>Entry</th></tr></thead>
          <tbody>${posRows}</tbody></table>` : ""}`;
  }

  async function loadAccount() {
    const body = $("acct-body");
    body.innerHTML = `<div class="empty">Syncing…</div>`;
    try {
      renderAccount(await (await fetch("/api/account?refresh=true")).json());
    } catch (e) {
      body.innerHTML = `<div class="empty">Failed to sync account.</div>`;
    }
  }

  // Wire controls
  $("btn-start").addEventListener("click", async () => {
    // There is no paper mode any more: this click sends real orders. Confirm
    // once, and echo back the size so an unintended ENGINE_CONTRACTS is caught
    // before the first fill rather than after it.
    const cfg = (lastStatus && lastStatus.config) || {};
    const n = cfg.contracts ?? "?";
    const enabled = ((lastStatus && lastStatus.strategies) || [])
      .filter((s) => s.enabled).length;
    if (!confirm(
      "Arm the engine — LIVE ONLY.\n\n" +
      "Real orders will be placed on the Delta testnet demo book.\n" +
      "There is no paper mode and no dry run.\n\n" +
      "  size            : " + n + " contract(s) per trade\n" +
      "  strategies armed: " + enabled + "\n\n" +
      "Continue?")) return;
    const res = await post("/api/strategy/start");
    if (res && res.started === false) {
      // The backend runs a credential preflight and refuses if it cannot
      // trade — surface that instead of silently leaving the engine stopped.
      alert("Engine did NOT start.\n\n" + (res.blocked_reason || "unknown reason"));
    }
    refresh();
  });
  $("btn-stop").addEventListener("click", async () => { await post("/api/strategy/stop"); refresh(); });
  $("btn-flatten").addEventListener("click", async () => {
    if (confirm("Close all open positions now?")) { await post("/api/strategy/flatten"); refresh(); }
  });
  $("btn-acct").addEventListener("click", loadAccount);

  // ---- real-time: state arrives by WebSocket push, no polling -------------
  // The engine frame carries live stats/positions every cycle; journal frames
  // arrive the instant an event is recorded.
  // Delta account state, pushed by the server-side sync loop.
  AppWS.on("account", renderAccount);

  AppWS.on("engine", (d) => {
    if (!d) return;
    if (d.stats) applyStats(d.stats);
    if (d.positions) renderPositions(d.positions);
    if (d.spot !== undefined && d.spot !== null) $("m-spot").textContent = fmt(d.spot, 1);
    if (d.cycles !== undefined) $("m-cycles").textContent = d.cycles;
    if (d.signals_generated !== undefined) $("m-signals").textContent = d.signals_generated;
    if (d.running !== undefined) applyRunning(d.running);
  });

  AppWS.on("journal", (events) => {
    const list = Array.isArray(events) ? events : [events];
    feedCache = [...list].reverse().concat(feedCache).slice(0, 60);
    renderFeed(feedCache);
  });

  AppWS.onStatus((s) => {
    const el = $("estate-text");
    if (el && s !== "connected") el.title = `live feed: ${s}`;
  });

  // One initial REST call to populate config/strategy cards, then push-driven.
  refresh();
})();
