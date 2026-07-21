/* Phase 5 — durable trade journal */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v) =>
    v === null || v === undefined ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v), 2);
  const timeStr = (ts) => (ts ? new Date(ts * 1000).toLocaleString("en-GB") : "—");

  let strategiesLoaded = false;

  async function load() {
    const strategy = $("f-strategy").value;
    const outcome = $("f-outcome").value;
    const mode = $("f-mode").value;
    const limit = $("f-limit").value;
    const qs = new URLSearchParams({ strategy, outcome, mode, limit });
    let d;
    try {
      d = await (await fetch("/api/trades?" + qs)).json();
    } catch {
      return;
    }

    if (!strategiesLoaded && d.strategies) {
      const sel = $("f-strategy");
      d.strategies.forEach((s) => {
        const o = document.createElement("option");
        o.value = s;
        o.textContent = s;
        sel.appendChild(o);
      });
      strategiesLoaded = true;
    }

    $("count").textContent = `${d.trades.length} shown · ${d.total} total`;

    const body = $("rows");
    if (!d.trades.length) {
      body.innerHTML = `<tr><td colspan="12" class="empty">No trades match these filters.</td></tr>`;
      return;
    }
    body.innerHTML = d.trades
      .map((t) => {
        const net = t.net_pnl;
        const gross = t.gross_pnl;
        return `<tr>
          <td>${timeStr(t.ts_close)}</td>
          <td>${t.strategy || "—"}</td>
          <td><span class="tag ${t.direction === "CE" ? "ce" : "pe"}">${t.direction || "—"}</span></td>
          <td style="color:var(--text-dim)">${t.symbol || "—"}</td>
          <td>${t.strike ? fmt(t.strike, 0) : "—"}</td>
          <td>${fmt(t.entry_price)}</td>
          <td>${fmt(t.exit_price)}</td>
          <td>${t.contracts ?? "—"}${t.partial ? " ⁄p" : ""}</td>
          <td class="${gross > 0 ? "pos" : gross < 0 ? "neg" : ""}">${money(gross)}</td>
          <td class="${net > 0 ? "pos" : net < 0 ? "neg" : ""}"><strong>${money(net)}</strong></td>
          <td class="why">${t.why || "—"}</td>
          <td><span class="tag ${t.mode || "paper"}">${(t.mode || "paper").replace("_", " ")}</span></td>
        </tr>`;
      })
      .join("");
  }

  ["f-strategy", "f-outcome", "f-mode", "f-limit"].forEach((id) =>
    $(id).addEventListener("change", load)
  );

  load();
  setInterval(load, 6000);
})();
