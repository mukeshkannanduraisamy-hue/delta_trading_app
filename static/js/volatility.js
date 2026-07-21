/* Variance risk premium (VRP) monitor */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const signed = (v) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + fmt(v, 2));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  let LAST_TERM = [];

  function drawSmile(t) {
    const canvas = $("smile");
    if (!canvas || !t || !t.smile) return;
    const pts = t.smile.filter((p) => p.call_iv !== null || p.put_iv !== null);
    if (pts.length < 2) return;

    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const padL = 56, padR = 14, padT = 14, padB = 30;
    const ks = pts.map((p) => p.strike);
    const ivs = pts.flatMap((p) => [p.call_iv, p.put_iv]).filter((v) => v !== null);
    const kMin = Math.min(...ks), kMax = Math.max(...ks);
    let lo = Math.min(...ivs), hi = Math.max(...ivs);
    if (lo === hi) { lo -= 1; hi += 1; }
    const padv = (hi - lo) * 0.12; lo -= padv; hi += padv;

    const X = (k) => padL + ((k - kMin) / (kMax - kMin || 1)) * (w - padL - padR);
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

    // grid + labels
    ctx.strokeStyle = css("--border"); ctx.fillStyle = css("--text-dim");
    ctx.font = "11px Inter, sans-serif"; ctx.lineWidth = 1;
    for (let g = 0; g <= 4; g++) {
      const val = lo + (g / 4) * (hi - lo), yy = Y(val);
      ctx.globalAlpha = 0.5; ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(fmt(val, 1) + "%", 6, yy + 4);
    }
    // spot marker
    if (t.spot >= kMin && t.spot <= kMax) {
      ctx.strokeStyle = css("--text-dim"); ctx.setLineDash([4, 4]); ctx.globalAlpha = 0.7;
      ctx.beginPath(); ctx.moveTo(X(t.spot), padT); ctx.lineTo(X(t.spot), h - padB); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      ctx.fillText("spot", X(t.spot) + 4, padT + 11);
    }
    // x labels
    [kMin, (kMin + kMax) / 2, kMax].forEach((k) => {
      const label = Math.round(k).toLocaleString("en-US");
      const x = X(k);
      ctx.fillText(label, Math.min(Math.max(x - 18, padL), w - padR - 40), h - 10);
    });

    const series = [
      { key: "call_iv", color: css("--success") },
      { key: "put_iv", color: css("--danger") },
    ];
    series.forEach(({ key, color }) => {
      const line = pts.filter((p) => p[key] !== null);
      if (line.length < 2) return;
      ctx.beginPath();
      line.forEach((p, i) => (i ? ctx.lineTo(X(p.strike), Y(p[key])) : ctx.moveTo(X(p.strike), Y(p[key]))));
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = color;
      line.forEach((p) => { ctx.beginPath(); ctx.arc(X(p.strike), Y(p[key]), 2.5, 0, Math.PI * 2); ctx.fill(); });
    });
  }

  async function load() {
    $("refresh").disabled = true;
    let d;
    try {
      d = await (await fetch("/api/volatility")).json();
    } catch {
      $("vrp-text").textContent = "Failed to load volatility data.";
      $("refresh").disabled = false;
      return;
    }
    if (d.error) {
      $("vrp-text").textContent = "Error: " + d.error;
      $("refresh").disabled = false;
      return;
    }

    // Headline
    const avg = d.avg_vrp;
    const el = $("avg-vrp");
    el.textContent = signed(avg);
    el.className = "big " + (avg > 0 ? "pos" : avg < 0 ? "neg" : "");
    const pos = d.expiries_positive, tot = d.expiries_total;
    let verdict;
    if (avg === null) verdict = "No VRP data available.";
    else if (avg > 2) verdict = "Options are priced <strong>above</strong> subsequent movement — a seller's premium is present.";
    else if (avg > 0) verdict = "A <strong>slight</strong> premium — too small to call an edge with confidence.";
    else verdict = "No net premium: options are <strong>not</strong> systematically rich versus realized movement.";
    const skew =
      d.avg_rr25 === null || d.avg_rr25 === undefined
        ? ""
        : `<br>Skew: avg <strong>RR25 ${signed(d.avg_rr25)}</strong> (${
            d.avg_rr25 < 0 ? "puts richer — crash protection bid" : "calls richer — upside bid"
          }), avg BF25 ${signed(d.avg_bf25)}.`;
    $("vrp-text").innerHTML =
      `average VRP in volatility points &nbsp;·&nbsp; positive in <strong>${pos}/${tot}</strong> expiries.<br>${verdict}${skew}`;

    // Realized vol
    $("rv").innerHTML = Object.entries(d.realized_vol || {})
      .map(([k, v]) => `<div class="rv">
          <div class="k">Realized ${k}</div>
          <div class="v">${fmt(v.parkinson)}%</div>
          <div class="sub">close-to-close ${fmt(v.close_to_close)}% · ${v.bars} bars</div>
        </div>`)
      .join("") || `<div class="empty">No realized-vol data.</div>`;

    // Term structure + skew
    const rows = d.term_structure || [];
    $("rows").innerHTML = rows.length
      ? rows
          .map((t) => {
            const cls = t.vrp > 0 ? "pos" : t.vrp < 0 ? "neg" : "";
            const rrCls = t.rr25 > 0 ? "pos" : t.rr25 < 0 ? "neg" : "";
            return `<tr>
              <td>${t.expiry}</td>
              <td>${fmt(t.dte, 1)}</td>
              <td>${fmt(t.atm_iv)}%</td>
              <td>${fmt(t.realized_vol)}% <span style="color:var(--text-dim)">(${t.rv_window})</span></td>
              <td class="${cls}"><strong>${signed(t.vrp)}</strong></td>
              <td>${fmt(t.iv_25d_call)}%</td>
              <td>${fmt(t.iv_25d_put)}%</td>
              <td class="${rrCls}"><strong>${signed(t.rr25)}</strong></td>
              <td>${signed(t.bf25)}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="9" class="empty">No expiries returned.</td></tr>`;

    // Smile chart
    LAST_TERM = rows;
    const sel = $("smile-expiry");
    if (sel.options.length !== rows.length) {
      sel.innerHTML = rows.map((t, i) => `<option value="${i}">${t.expiry} (${fmt(t.dte, 1)}d)</option>`).join("");
      sel.value = Math.min(2, rows.length - 1); // a mid-tenor default
    }
    drawSmile(rows[sel.value | 0]);

    // Collection progress
    const c = d.collection || {};
    $("collection").innerHTML = c.rows
      ? `<strong>Forward series:</strong> ${c.rows} IV snapshots across ${c.expiries} expiries, spanning ${c.span_days} days.
         The app records a snapshot every 30 minutes. A proper VRP test compares IV recorded today against the
         volatility that actually follows — that needs weeks of this data, not a single reading.`
      : `<strong>Forward series:</strong> collecting. The app records an IV snapshot every 30 minutes so a genuine
         VRP test (IV today vs realized vol later) becomes possible over the coming weeks.`;

    $("refresh").disabled = false;
  }

  $("refresh").addEventListener("click", load);
  $("smile-expiry").addEventListener("change", (e) => drawSmile(LAST_TERM[e.target.value | 0]));
  window.addEventListener("resize", () => {
    const i = $("smile-expiry").value | 0;
    if (LAST_TERM[i]) drawSmile(LAST_TERM[i]);
  });
  load();
  setInterval(load, 60000);
})();
