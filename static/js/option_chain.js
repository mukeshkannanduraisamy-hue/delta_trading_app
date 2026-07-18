/* ==========================================================================
   Delta Trading — option chain (Phase 3)
   Fetches a structured chain from the backend and renders the table (with
   ITM/OTM + ATM coloring, IV coloring, OI bars), summary stats, an OI bar
   chart and an IV-smile line chart (pure canvas, no external deps).
   ========================================================================== */
(function () {
  "use strict";

  var state = {
    asset: "BTC",
    expiry: "",
    strikes: 20,
    autoRefresh: false,
    interval: 10,
    timer: null,
    data: null,
    loading: false,
  };

  var $ = function (id) { return document.getElementById(id); };

  // ---- Formatting --------------------------------------------------------
  function fmtPrice(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  function fmtInt(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Math.round(v).toLocaleString("en-US");
  }
  function fmtCompact(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return Math.round(v).toString();
  }
  function fmtGreek(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v.toFixed(4);
  }
  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v.toFixed(1) + "%";
  }
  function getIVColor(iv) {
    if (iv === null || iv === undefined || isNaN(iv)) return "var(--text-dim)";
    if (iv < 30) return "#58A6FF";
    if (iv < 50) return "#3FB950";
    if (iv < 70) return "#D29922";
    return "#F85149";
  }

  // ---- Fetch -------------------------------------------------------------
  function setStatus(msg) { var s = $("oc-status"); if (s) s.textContent = msg; }

  function loadExpiries(asset, cb) {
    fetch("/api/expiries?asset=" + encodeURIComponent(asset))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var sel = $("expiry-select");
        sel.innerHTML = "";
        (data.expiries || []).forEach(function (e) {
          var o = document.createElement("option");
          o.value = e.expiry;
          o.textContent = e.expiry;
          sel.appendChild(o);
        });
        state.expiry = sel.value || "";
        if (cb) cb();
      })
      .catch(function (e) { console.error("expiries failed", e); setStatus("Expiry load failed"); });
  }

  function fetchOptionChain(recenter) {
    if (!state.expiry) return;
    state.loading = true;
    setStatus("Loading…");
    var url = "/api/option-chain?asset=" + encodeURIComponent(state.asset) +
              "&expiry=" + encodeURIComponent(state.expiry);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.loading = false;
        if (data.error) { setStatus("Error: " + data.error); return; }
        state.data = data;
        renderAll(data, recenter === true);
        setStatus("Updated " + new Date().toLocaleTimeString("en-US", { hour12: false }));
      })
      .catch(function (e) {
        state.loading = false;
        console.error("option-chain failed", e);
        setStatus("Load failed");
      });
  }

  // ---- Render: everything ------------------------------------------------
  function renderAll(data, recenter) {
    updateSummaryStats(data);
    renderTable(data, recenter);
    renderOIChart(data);
    renderIVSmile(data);
  }

  // Filter strikes to N nearest the ATM (half ITM / half OTM).
  function visibleStrikes(data) {
    var all = (data.strikes || []).slice();
    if (!all.length || data.atm_strike == null) return all;
    var atmIdx = 0, best = Infinity;
    for (var i = 0; i < all.length; i++) {
      var d = Math.abs(all[i].strike - data.atm_strike);
      if (d < best) { best = d; atmIdx = i; }
    }
    var half = Math.floor(state.strikes / 2);
    var lo = Math.max(0, atmIdx - half);
    var hi = Math.min(all.length, atmIdx + half + 1);
    return all.slice(lo, hi);
  }

  // ---- Summary stats -----------------------------------------------------
  function updateSummaryStats(data) {
    function set(k, v, cls) {
      var n = document.querySelector('[data-sum="' + k + '"]');
      if (!n) return;
      n.textContent = v;
      n.classList.remove("pos", "neg");
      if (cls) n.classList.add(cls);
    }
    set("spot", fmtPrice(data.spot_price));
    set("atm", fmtPrice(data.atm_strike));
    set("pcr_oi", data.pcr_oi == null ? "—" : data.pcr_oi.toFixed(2),
        data.pcr_oi == null ? null : (data.pcr_oi > 1 ? "neg" : "pos"));
    set("pcr_vol", data.pcr_volume == null ? "—" : data.pcr_volume.toFixed(2),
        data.pcr_volume == null ? null : (data.pcr_volume > 1 ? "neg" : "pos"));
    set("call_oi", fmtCompact(data.total_call_oi));
    set("put_oi", fmtCompact(data.total_put_oi));
    set("max_pain", fmtPrice(data.max_pain));
    set("atm_iv", data.atm_iv == null ? "—" : data.atm_iv.toFixed(1) + "%");
  }

  // ---- Table -------------------------------------------------------------
  function renderTable(data, recenter) {
    var tbody = $("oc-tbody");
    var rows = visibleStrikes(data);
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="17" class="oc-empty">No option data for this expiry.</td></tr>';
      return;
    }
    var spot = data.spot_price;
    var atm = data.atm_strike;

    var maxOI = 0;
    rows.forEach(function (r) {
      if (r.call && r.call.oi) maxOI = Math.max(maxOI, r.call.oi);
      if (r.put && r.put.oi) maxOI = Math.max(maxOI, r.put.oi);
    });
    maxOI = maxOI || 1;

    var html = "";
    rows.forEach(function (r) {
      var c = r.call || {};
      var p = r.put || {};
      var isATM = (r.strike === atm);
      var callITM = spot != null && r.strike < spot; // call ITM when strike below spot
      var putITM = spot != null && r.strike > spot;  // put ITM when strike above spot

      var callCls = "side-call " + (callITM ? "itm" : "otm");
      var putCls = "side-put " + (putITM ? "itm" : "otm");
      var rowCls = isATM ? "atm-row" : "";

      html += '<tr class="' + rowCls + '">' +
        oiCell(c.oi, maxOI, "call") +
        td(fmtCompact(c.volume), callCls) +
        ivCell(c.iv, callCls) +
        deltaCell(c.delta, true, callCls) +
        td(fmtGreek(c.theta), callCls + " greek") +
        td(fmtPrice(c.bid), callCls) +
        td(fmtPrice(c.ask), callCls) +
        td(fmtPrice(c.mark_price), callCls + " ltp") +
        '<td class="strike-cell">' + fmtPrice(r.strike) + "</td>" +
        td(fmtPrice(p.mark_price), putCls + " ltp") +
        td(fmtPrice(p.bid), putCls) +
        td(fmtPrice(p.ask), putCls) +
        ivCell(p.iv, putCls) +
        deltaCell(p.delta, false, putCls) +
        td(fmtGreek(p.theta), putCls + " greek") +
        td(fmtCompact(p.volume), putCls) +
        oiCell(p.oi, maxOI, "put") +
        "</tr>";
    });
    tbody.innerHTML = html;

    // Center the ATM row within the scrollable table container (container-only
    // scroll — never moves the page). Only on explicit loads, not auto-refresh.
    if (recenter) {
      var atmRow = tbody.querySelector(".atm-row");
      var wrap = document.querySelector(".oc-table-wrap");
      if (atmRow && wrap) {
        wrap.scrollTop = Math.max(0, atmRow.offsetTop - wrap.clientHeight / 2);
      }
    }
  }

  function td(val, cls) { return '<td class="' + (cls || "") + '">' + val + "</td>"; }

  function ivCell(iv, cls) {
    var color = getIVColor(iv);
    return '<td class="' + cls + '" style="color:' + color + ';font-weight:600">' + fmtPct(iv) + "</td>";
  }

  function deltaCell(dv, isCall, cls) {
    if (dv === null || dv === undefined || isNaN(dv)) return td("—", cls);
    var mag = Math.min(1, Math.abs(dv));
    var alpha = (0.35 + 0.65 * mag).toFixed(2);
    var color = isCall ? "rgba(63,185,80," + alpha + ")" : "rgba(248,81,73," + alpha + ")";
    return '<td class="' + cls + '" style="color:' + color + '">' + dv.toFixed(3) + "</td>";
  }

  function oiCell(oi, maxOI, side) {
    var val = (oi === null || oi === undefined || isNaN(oi)) ? "—" : fmtCompact(oi);
    var pct = (oi ? Math.min(100, (oi / maxOI) * 100) : 0).toFixed(1);
    return '<td class="oi-cell ' + side + '">' +
      '<span class="oi-bar" style="width:' + pct + '%"></span>' +
      '<span class="oi-num">' + val + "</span></td>";
  }

  // ---- Canvas helpers ----------------------------------------------------
  function setupCanvas(canvas) {
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || canvas.parentNode.clientWidth || 600;
    var cssH = canvas.getAttribute("height") ? parseInt(canvas.getAttribute("height"), 10) : 300;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    return { ctx: ctx, w: cssW, h: cssH };
  }

  // ---- OI bar chart ------------------------------------------------------
  function renderOIChart(data) {
    var canvas = $("oi-chart");
    if (!canvas) return;
    var c = setupCanvas(canvas);
    var ctx = c.ctx, W = c.w, H = c.h;
    var rows = visibleStrikes(data);
    var pcrLabel = $("oi-pcr-label");
    if (pcrLabel) pcrLabel.textContent = data.pcr_oi != null ? "PCR " + data.pcr_oi.toFixed(2) : "";
    if (!rows.length) return;

    var padL = 44, padR = 10, padT = 14, padB = 46;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var maxOI = 0;
    rows.forEach(function (r) {
      if (r.call && r.call.oi) maxOI = Math.max(maxOI, r.call.oi);
      if (r.put && r.put.oi) maxOI = Math.max(maxOI, r.put.oi);
    });
    maxOI = maxOI || 1;

    // Y gridlines + labels
    ctx.font = "10px Inter, sans-serif";
    ctx.fillStyle = "#8B949E";
    ctx.strokeStyle = "#30363D";
    ctx.lineWidth = 1;
    for (var g = 0; g <= 4; g++) {
      var yv = (maxOI / 4) * g;
      var y = padT + plotH - (yv / maxOI) * plotH;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      ctx.fillText(fmtCompact(yv), padL - 5, y);
    }

    var n = rows.length;
    var slot = plotW / n;
    var barW = Math.min(14, slot * 0.38);
    rows.forEach(function (r, i) {
      var cx = padL + slot * i + slot / 2;
      var callOI = (r.call && r.call.oi) || 0;
      var putOI = (r.put && r.put.oi) || 0;
      var callH = (callOI / maxOI) * plotH;
      var putH = (putOI / maxOI) * plotH;
      // call bar (green) left, put bar (red) right
      ctx.fillStyle = "rgba(63,185,80,0.8)";
      ctx.fillRect(cx - barW - 1, padT + plotH - callH, barW, callH);
      ctx.fillStyle = "rgba(248,81,73,0.8)";
      ctx.fillRect(cx + 1, padT + plotH - putH, barW, putH);
      // ATM marker
      if (r.strike === data.atm_strike) {
        ctx.strokeStyle = "#D29922"; ctx.lineWidth = 1.5;
        ctx.strokeRect(cx - barW - 2, padT, barW * 2 + 4, plotH);
        ctx.strokeStyle = "#30363D"; ctx.lineWidth = 1;
      }
      // x label (sparse)
      if (i % Math.ceil(n / 8) === 0 || r.strike === data.atm_strike) {
        ctx.save();
        ctx.translate(cx, H - padB + 6);
        ctx.rotate(-Math.PI / 4);
        ctx.textAlign = "right"; ctx.textBaseline = "middle";
        ctx.fillStyle = r.strike === data.atm_strike ? "#D29922" : "#8B949E";
        ctx.fillText(fmtCompact(r.strike), 0, 0);
        ctx.restore();
      }
    });

    // legend
    legend(ctx, W - padR - 120, padT + 2, [
      ["rgba(63,185,80,0.8)", "Call OI"],
      ["rgba(248,81,73,0.8)", "Put OI"],
    ]);
  }

  // ---- IV smile ----------------------------------------------------------
  function renderIVSmile(data) {
    var canvas = $("iv-smile-chart");
    if (!canvas) return;
    var c = setupCanvas(canvas);
    var ctx = c.ctx, W = c.w, H = c.h;
    var rows = visibleStrikes(data);
    if (!rows.length) return;

    var padL = 40, padR = 10, padT = 14, padB = 46;
    var plotW = W - padL - padR, plotH = H - padT - padB;

    var ivs = [];
    rows.forEach(function (r) {
      if (r.call && r.call.iv != null) ivs.push(r.call.iv);
      if (r.put && r.put.iv != null) ivs.push(r.put.iv);
    });
    if (!ivs.length) return;
    var minIV = Math.min.apply(null, ivs);
    var maxIV = Math.max.apply(null, ivs);
    var pad = (maxIV - minIV) * 0.15 || 1;
    minIV -= pad; maxIV += pad;

    var n = rows.length;
    function xAt(i) { return padL + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1)); }
    function yAt(v) { return padT + plotH - ((v - minIV) / (maxIV - minIV)) * plotH; }

    // Y grid + labels
    ctx.font = "10px Inter, sans-serif";
    ctx.strokeStyle = "#30363D"; ctx.lineWidth = 1;
    for (var g = 0; g <= 4; g++) {
      var yv = minIV + ((maxIV - minIV) / 4) * g;
      var y = yAt(yv);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
      ctx.fillStyle = "#8B949E"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
      ctx.fillText(yv.toFixed(0) + "%", padL - 4, y);
    }

    // ATM vertical dashed line
    var atmIdx = -1;
    rows.forEach(function (r, i) { if (r.strike === data.atm_strike) atmIdx = i; });
    if (atmIdx >= 0) {
      ctx.save();
      ctx.setLineDash([4, 4]); ctx.strokeStyle = "#D29922"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(xAt(atmIdx), padT); ctx.lineTo(xAt(atmIdx), padT + plotH); ctx.stroke();
      ctx.restore();
    }

    drawLine(ctx, rows, "call", xAt, yAt, "#3FB950");
    drawLine(ctx, rows, "put", xAt, yAt, "#F85149");

    // x labels (sparse)
    rows.forEach(function (r, i) {
      if (i % Math.ceil(n / 8) === 0 || r.strike === data.atm_strike) {
        ctx.save();
        ctx.translate(xAt(i), H - padB + 6);
        ctx.rotate(-Math.PI / 4);
        ctx.textAlign = "right"; ctx.textBaseline = "middle";
        ctx.fillStyle = r.strike === data.atm_strike ? "#D29922" : "#8B949E";
        ctx.fillText(fmtCompact(r.strike), 0, 0);
        ctx.restore();
      }
    });

    legend(ctx, W - padR - 110, padT + 2, [
      ["#3FB950", "Call IV"],
      ["#F85149", "Put IV"],
    ]);
  }

  function drawLine(ctx, rows, side, xAt, yAt, color) {
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.6;
    ctx.beginPath();
    var started = false;
    rows.forEach(function (r, i) {
      var s = r[side];
      if (!s || s.iv == null) return;
      var x = xAt(i), y = yAt(s.iv);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    });
    ctx.stroke();
    rows.forEach(function (r, i) {
      var s = r[side];
      if (!s || s.iv == null) return;
      ctx.beginPath(); ctx.arc(xAt(i), yAt(s.iv), 2, 0, Math.PI * 2); ctx.fill();
    });
  }

  function legend(ctx, x, y, items) {
    ctx.font = "10px Inter, sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    items.forEach(function (it, i) {
      var yy = y + i * 14 + 6;
      ctx.fillStyle = it[0]; ctx.fillRect(x, yy - 4, 10, 8);
      ctx.fillStyle = "#C9D1D9"; ctx.fillText(it[1], x + 15, yy);
    });
  }

  // ---- Auto-refresh ------------------------------------------------------
  function startAutoRefresh() {
    stopAutoRefresh();
    if (state.autoRefresh) {
      state.timer = setInterval(function () { fetchOptionChain(false); }, state.interval * 1000);
    }
  }
  function stopAutoRefresh() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  // ---- Wiring ------------------------------------------------------------
  function init() {
    // Asset toggle
    Array.prototype.forEach.call(document.querySelectorAll(".asset-btn"), function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".asset-btn").forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        state.asset = b.getAttribute("data-asset");
        loadExpiries(state.asset, function () { fetchOptionChain(true); });
      });
    });
    $("expiry-select").addEventListener("change", function (e) {
      state.expiry = e.target.value; fetchOptionChain(true);
    });
    $("strikes-input").addEventListener("change", function (e) {
      var v = parseInt(e.target.value, 10);
      state.strikes = isNaN(v) ? 20 : Math.max(4, Math.min(80, v));
      if (state.data) renderAll(state.data, true);
    });
    $("autorefresh-toggle").addEventListener("change", function (e) {
      state.autoRefresh = e.target.checked; startAutoRefresh();
    });
    $("interval-select").addEventListener("change", function (e) {
      state.interval = parseInt(e.target.value, 10) || 10; startAutoRefresh();
    });
    $("refresh-btn").addEventListener("click", function () { fetchOptionChain(true); });
    window.addEventListener("resize", function () { if (state.data) { renderOIChart(state.data); renderIVSmile(state.data); } });

    loadExpiries(state.asset, function () { fetchOptionChain(true); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
