/* ==========================================================================
   Delta Trading — candlestick chart (Phase 2)
   TradingView Lightweight Charts v4. Loads historical candles over REST and
   applies live candle updates pushed from the backend WebSocket.
   Exposes window.DeltaChart for app.js to feed live candles.
   ========================================================================== */
(function () {
  "use strict";

  var chart = null;
  var candleSeries = null;
  var volumeSeries = null;
  var container = null;
  var state = { symbol: "BTCUSD", resolution: "5m", lastBarTime: 0 };

  var UP = "#3FB950";
  var DOWN = "#F85149";

  function volColor(c) {
    return c.close >= c.open ? "rgba(63,185,80,0.5)" : "rgba(248,81,73,0.5)";
  }

  function initChart() {
    container = document.getElementById("chart-container");
    if (!container || !window.LightweightCharts) {
      console.error("Lightweight Charts not available");
      return;
    }

    chart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: 500,
      layout: {
        background: { color: "#161B22" },
        textColor: "#C9D1D9",
        fontFamily: "Inter, sans-serif",
      },
      grid: {
        vertLines: { color: "#30363D" },
        horzLines: { color: "#30363D" },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#30363D" },
      timeScale: { borderColor: "#30363D", timeVisible: true, secondsVisible: false },
    });

    candleSeries = chart.addCandlestickSeries({
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });

    // Volume as an overlay histogram pinned to the bottom ~18% of the pane.
    volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "",
      color: "rgba(63,185,80,0.5)",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    wireTimeframeButtons();
    window.addEventListener("resize", handleResize);
    loadCandles(state.symbol, state.resolution);
  }

  function loadCandles(symbol, resolution) {
    state.symbol = symbol;
    state.resolution = resolution;
    setChartTitle();
    setLoading(true);

    var url =
      "/api/candles?symbol=" + encodeURIComponent(symbol) +
      "&resolution=" + encodeURIComponent(resolution) + "&limit=300";

    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.candles) return;
        var candles = data.candles;
        candleSeries.setData(
          candles.map(function (c) {
            return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
          })
        );
        volumeSeries.setData(
          candles.map(function (c) {
            return { time: c.time, value: c.volume || 0, color: volColor(c) };
          })
        );
        state.lastBarTime = candles.length ? candles[candles.length - 1].time : 0;
        chart.timeScale().fitContent();
        setLoading(false);
      })
      .catch(function (e) {
        console.error("loadCandles failed", e);
        setLoading(false);
      });
  }

  // Live candle update pushed from the backend WebSocket.
  function onCandle(msg) {
    if (!candleSeries) return;
    if (msg.symbol !== state.symbol || msg.resolution !== state.resolution) return;
    if (msg.time == null || msg.time < state.lastBarTime) return; // never rewrite older bars
    candleSeries.update({
      time: msg.time, open: msg.open, high: msg.high, low: msg.low, close: msg.close,
    });
    volumeSeries.update({ time: msg.time, value: msg.volume || 0, color: volColor(msg) });
    state.lastBarTime = msg.time;
  }

  function changeTimeframe(resolution) {
    if (resolution === state.resolution) return;
    setActiveButton(resolution);
    loadCandles(state.symbol, resolution);
  }

  function wireTimeframeButtons() {
    var btns = document.querySelectorAll(".tf-btn");
    Array.prototype.forEach.call(btns, function (b) {
      b.addEventListener("click", function () {
        changeTimeframe(b.getAttribute("data-res"));
      });
    });
    setActiveButton(state.resolution);
  }

  function setActiveButton(resolution) {
    Array.prototype.forEach.call(document.querySelectorAll(".tf-btn"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-res") === resolution);
    });
  }

  function setChartTitle() {
    var t = document.getElementById("chart-title-res");
    if (t) t.textContent = state.resolution.toUpperCase();
  }

  function setLoading(on) {
    var l = document.getElementById("chart-loading");
    if (l) l.style.display = on ? "flex" : "none";
  }

  function handleResize() {
    if (chart && container) chart.applyOptions({ width: container.clientWidth });
  }

  window.DeltaChart = {
    init: initChart,
    loadCandles: loadCandles,
    onCandle: onCandle,
    changeTimeframe: changeTimeframe,
    state: state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initChart);
  } else {
    initChart();
  }
})();
