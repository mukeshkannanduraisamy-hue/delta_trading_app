/* Phase 5+ — strategy backtest runner */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

  function verdict(exp) {
    if (exp === null || exp === undefined) return `<span class="tag">n/a</span>`;
    if (exp > 0.02) return `<span class="verdict pos">edge</span>`;
    if (exp < -0.02) return `<span class="verdict neg">no edge</span>`;
    return `<span class="tag">flat</span>`;
  }

  async function run() {
    const days = $("days").value;
    const maxhold = $("maxhold").value;
    $("run").disabled = true;
    $("status").textContent = `Running ${days}-day backtest… (fetching history + replaying, may take 10–40s)`;
    let d;
    try {
      d = await (await fetch(`/api/backtest?days=${days}&max_hold=${maxhold}`)).json();
    } catch (e) {
      $("status").textContent = "Failed to run backtest.";
      $("run").disabled = false;
      return;
    }

    const bars = Object.entries(d.bars_by_timeframe || {}).map(([k, v]) => `${k}:${v}`).join("  ");
    $("status").textContent = `Done — ${d.days}d, bars ${bars}`;

    const rows = d.results || [];
    $("rows").innerHTML = rows.length
      ? rows
          .map((s) => {
            const exp = s.expectancy_r;
            const cls = exp > 0 ? "pos" : exp < 0 ? "neg" : "";
            return `<tr>
              <td>${s.title}</td>
              <td>${s.timeframe}</td>
              <td>${s.trades}</td>
              <td>${s.win_rate ?? "—"}</td>
              <td class="${cls}"><strong>${fmt(exp, 3)}</strong></td>
              <td class="${s.total_r > 0 ? "pos" : "neg"}">${fmt(s.total_r, 1)}</td>
              <td class="${s.profit_factor >= 1 ? "pos" : "neg"}">${s.profit_factor ?? "—"}</td>
              <td class="pos">${s.avg_win_r ?? "—"}</td>
              <td class="neg">${s.avg_loss_r ?? "—"}</td>
              <td>${verdict(exp)}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="10" class="empty">No results.</td></tr>`;

    const skipped = d.skipped || [];
    $("skipped").innerHTML = skipped.length
      ? "Skipped: " + skipped.map((s) => `${s.strategy} (${s.reason})`).join("; ")
      : "";

    $("run").disabled = false;
  }

  $("run").addEventListener("click", run);
})();
