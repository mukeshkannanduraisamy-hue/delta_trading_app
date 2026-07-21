/* Walk-forward signal research screen */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 3) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

  async function run() {
    const days = $("days").value;
    const maxhold = $("maxhold").value;
    $("run").disabled = true;
    $("status").textContent = `Screening ${days} days across 5m / 15m / 1h… (20–60s)`;
    let d;
    try {
      d = await (await fetch(`/api/research?days=${days}&max_hold=${maxhold}`)).json();
    } catch {
      $("status").textContent = "Failed to run screen.";
      $("run").disabled = false;
      return;
    }

    const bars = Object.entries(d.bars_by_timeframe || {}).map(([k, v]) => `${k}:${v}`).join("  ");
    $("status").textContent = `Done — ${d.days}d, bars ${bars}, ${d.combos_tested} combos`;

    // Headline
    const hl = $("headline");
    hl.style.display = "flex";
    const n = d.survivors;
    $("surv-count").textContent = n;
    $("surv-count").className = "big " + (n > 0 ? "pos" : "neg");
    $("surv-text").innerHTML =
      n > 0
        ? `of ${d.combos_tested} combos stayed profitable in <strong>both</strong> the train and test halves.<br>Worth investigating — but still excludes option spread and theta.`
        : `of ${d.combos_tested} combos stayed profitable in <strong>both</strong> halves.<br>No signal family showed durable directional edge on BTC over this window.`;

    const rows = d.results || [];
    $("rows").innerHTML = rows.length
      ? rows
          .map((r) => {
            const teCls = r.test_exp > 0 ? "pos" : "neg";
            const trCls = r.train_exp > 0 ? "pos" : "neg";
            return `<tr>
              <td>${r.family}</td>
              <td>${r.timeframe}</td>
              <td>${r.train_trades}</td>
              <td class="${trCls}">${fmt(r.train_exp)}</td>
              <td>${r.test_trades}</td>
              <td class="${teCls}"><strong>${fmt(r.test_exp)}</strong></td>
              <td class="${r.test_pf >= 1 ? "pos" : "neg"}">${r.test_pf ?? "—"}</td>
              <td>${r.survives ? '<span class="tag yes">survives</span>' : '<span class="tag">no</span>'}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="8" class="empty">No combos met the minimum trade count.</td></tr>`;

    $("run").disabled = false;
  }

  $("run").addEventListener("click", run);
})();
