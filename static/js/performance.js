/* Phase 5 — performance analytics + pure-canvas equity curve */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const fmt = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v) =>
    v === null || v === undefined ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v), 2);

  function drawEquity(curve) {
    const canvas = $("equity");
    const empty = $("eq-empty");
    if (!curve || curve.length < 2) {
      canvas.style.display = "none";
      empty.style.display = "block";
      return;
    }
    canvas.style.display = "block";
    empty.style.display = "none";

    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const padL = 64, padR = 16, padT = 16, padB = 28;
    const eqs = curve.map((p) => p.equity);
    let lo = Math.min(...eqs), hi = Math.max(...eqs);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;

    const x = (i) => padL + (i / (curve.length - 1)) * (w - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

    // Grid + y labels
    ctx.strokeStyle = css("--border");
    ctx.fillStyle = css("--text-dim");
    ctx.font = "11px Inter, sans-serif";
    ctx.lineWidth = 1;
    for (let g = 0; g <= 4; g++) {
      const val = lo + (g / 4) * (hi - lo);
      const yy = y(val);
      ctx.globalAlpha = 0.5;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillText("$" + fmt(val, 2), 6, yy + 4);
    }

    // Baseline (starting balance) reference
    const start = curve.length ? curve[0].equity - (curve[0].net || 0) : 0;
    if (start >= lo && start <= hi) {
      ctx.strokeStyle = css("--text-dim");
      ctx.setLineDash([4, 4]);
      ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(padL, y(start)); ctx.lineTo(w - padR, y(start)); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }

    const up = curve[curve.length - 1].equity >= start;
    const line = up ? css("--success") : css("--danger");

    // Area fill
    const grad = ctx.createLinearGradient(0, padT, 0, h - padB);
    grad.addColorStop(0, (up ? "rgba(63,185,80," : "rgba(248,81,73,") + "0.22)");
    grad.addColorStop(1, (up ? "rgba(63,185,80," : "rgba(248,81,73,") + "0.01)");
    ctx.beginPath();
    ctx.moveTo(x(0), y(curve[0].equity));
    curve.forEach((p, i) => ctx.lineTo(x(i), y(p.equity)));
    ctx.lineTo(x(curve.length - 1), h - padB);
    ctx.lineTo(x(0), h - padB);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(x(0), y(curve[0].equity));
    curve.forEach((p, i) => ctx.lineTo(x(i), y(p.equity)));
    ctx.strokeStyle = line;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  function renderPerStrategy(per) {
    const body = $("per-rows");
    if (!per || !per.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">No trades yet.</td></tr>`;
      return;
    }
    const maxAbs = Math.max(...per.map((p) => Math.abs(p.net_pnl)), 1);
    body.innerHTML = per
      .map((p) => {
        const pos = p.net_pnl >= 0;
        const width = (Math.abs(p.net_pnl) / maxAbs) * 100;
        return `<tr>
          <td>${p.strategy}</td>
          <td>${p.trades}</td>
          <td class="pos">${p.wins}</td>
          <td class="neg">${p.losses}</td>
          <td>${p.win_rate === null ? "—" : p.win_rate + "%"}</td>
          <td class="${pos ? "pos" : "neg"}"><strong>${money(p.net_pnl)}</strong></td>
          <td><div class="bar"><span style="${pos ? "left:50%" : "right:50%"};width:${width / 2}%;background:var(${pos ? "--success" : "--danger"})"></span></div></td>
        </tr>`;
      })
      .join("");
  }

  async function load() {
    let d;
    try {
      d = await (await fetch("/api/performance")).json();
    } catch {
      return;
    }
    const pnlEl = $("k-pnl");
    pnlEl.textContent = money(d.total_pnl);
    pnlEl.className = "v " + (d.total_pnl > 0 ? "pos" : d.total_pnl < 0 ? "neg" : "");
    $("k-equity").textContent = "Equity " + money(d.equity);

    $("k-winrate").textContent = d.win_rate === null ? "—" : d.win_rate + "%";
    $("k-wl").textContent = `${d.wins}W / ${d.losses}L`;

    $("k-pf").textContent = d.profit_factor === null ? "—" : fmt(d.profit_factor, 2);
    $("k-avg").textContent = `avg ${money(d.avg_win)} / ${money(d.avg_loss)}`;

    $("k-dd").textContent = money(-d.max_drawdown);
    $("k-trades").textContent = `${d.total_trades} trades · best ${money(d.best_trade)} / worst ${money(d.worst_trade)}`;

    drawEquity(d.equity_curve);
    renderPerStrategy(d.per_strategy);
  }

  load();
  setInterval(load, 6000);
  window.addEventListener("resize", load);
})();
