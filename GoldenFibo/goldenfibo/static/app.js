/* GoldenFibo chart client — RENDERS backend state only. No ladder math. */
(function () {
  const el = document.getElementById("chart");
  const statusEl = document.getElementById("connStatus");
  const hudPrice = document.getElementById("hudPrice");
  const hudCycle = document.getElementById("hudCycle");
  const hudStep = document.getElementById("hudStep");
  const hudP0 = document.getElementById("hudP0");
  const hudTp = document.getElementById("hudTp");

  const chart = LightweightCharts.createChart(el, {
    layout: {
      background: { color: "#0b0e11" },
      textColor: "#c5d0e0",
    },
    grid: {
      vertLines: { color: "#1a2030" },
      horzLines: { color: "#1a2030" },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: "#1e2630" },
    timeScale: { borderColor: "#1e2630", timeVisible: true, secondsVisible: false },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: "#0ecb81",
    downColor: "#f6465d",
    borderVisible: false,
    wickUpColor: "#0ecb81",
    wickDownColor: "#f6465d",
  });

  /** @type {Record<string, any>} */
  const priceLines = {};
  let ws = null;
  let reconnectTimer = null;

  function setStatus(text, ok) {
    statusEl.textContent = text;
    statusEl.className = "status " + (ok === true ? "ok" : ok === false ? "bad" : "");
  }

  function resize() {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }
  window.addEventListener("resize", resize);
  resize();

  const ROLE_STYLE = {
    P0: { color: "#f0b90b", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "P0" },
    filled: { color: "#5b8def", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" },
    current: { color: "#ffffff", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "P(n)" },
    tp: { color: "#0ecb81", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "TP" },
    tp_prev: { color: "#0ecb81", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "P(n-1)" },
    next: { color: "#f6465d", lineWidth: 2, lineStyle: 1, axisLabelVisible: true, title: "P(n+1)" },
    further: { color: "#f6465d", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "P(n+2)" },
  };

  function clearPriceLines() {
    Object.keys(priceLines).forEach((k) => {
      try {
        candleSeries.removePriceLine(priceLines[k]);
      } catch (_) {}
      delete priceLines[k];
    });
  }

  function applyLevels(levels) {
    if (!Array.isArray(levels)) return;
    clearPriceLines();
    levels.forEach((lv) => {
      const price = Number(lv.price);
      if (!Number.isFinite(price)) return;
      const role = lv.role || "filled";
      // Prefer explicit TP line; skip duplicate tp_prev price if TP present
      if (role === "tp_prev" && levels.some((x) => x.role === "tp")) return;
      const style = ROLE_STYLE[role] || ROLE_STYLE.filled;
      const title = style.title || lv.id || "";
      const line = candleSeries.createPriceLine({
        price,
        color: style.color,
        lineWidth: style.lineWidth,
        lineStyle: style.lineStyle,
        axisLabelVisible: style.axisLabelVisible,
        title: title ? `${title} ${price}` : String(price),
      });
      priceLines[lv.id || `${role}-${price}`] = line;
    });
  }

  function applyMetrics(msg) {
    const extras = [
      ["ladder_vwap", msg.ladder_vwap, "#f2c500", "L-VWAP"],
      ["active_step_vwap", msg.active_step_vwap, "#c9a227", "S-VWAP"],
      ["ladder_poc", msg.ladder_poc, "#26a69a", "L-POC"],
      ["active_step_poc", msg.active_step_poc, "#66bb6a", "S-POC"],
    ];
    extras.forEach(([key, val, color, title]) => {
      if (priceLines[key]) {
        try {
          candleSeries.removePriceLine(priceLines[key]);
        } catch (_) {}
        delete priceLines[key];
      }
      const price = Number(val);
      if (!Number.isFinite(price)) return;
      priceLines[key] = candleSeries.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: `${title} ${price}`,
      });
    });
  }

  function applyHud(msg) {
    if (msg.price != null) hudPrice.textContent = msg.price;
    if (msg.cycle_id != null) hudCycle.textContent = String(msg.cycle_id);
    if (msg.n != null) hudStep.textContent = "P" + msg.n;
    if (msg.p0 != null) hudP0.textContent = msg.p0;
    if (msg.shared_tp != null) hudTp.textContent = msg.shared_tp;
  }

  function applySnapshot(msg) {
    if (Array.isArray(msg.candles) && msg.candles.length) {
      candleSeries.setData(msg.candles);
      chart.timeScale().fitContent();
    }
    applyLevels(msg.levels);
    applyMetrics(msg);
    applyHud(msg);
    if (Array.isArray(msg.markers) && msg.markers.length) {
      try {
        candleSeries.setMarkers(msg.markers);
      } catch (_) {}
    }
    if (msg.side) document.getElementById("side").value = msg.side;
    if (msg.percentage) document.getElementById("percentage").value = msg.percentage;
    if (msg.symbol) document.getElementById("symbol").value = msg.symbol;
  }

  function applyEngineEvent(msg) {
    if (msg.state) {
      if (msg.state.levels) applyLevels(msg.state.levels);
      applyHud(Object.assign({}, msg.state, { price: undefined }));
      if (msg.state.ladder_vwap !== undefined) applyMetrics(msg.state);
      if (Array.isArray(msg.state.markers) && msg.state.markers.length) {
        // append-style: backend sends only new markers; keep simple full replace from last snapshot ideally
        try {
          const existing = candleSeries.markers ? candleSeries.markers() : [];
        } catch (_) {}
      }
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    setStatus("connecting…");
    ws = new WebSocket(url);
    ws.onopen = () => setStatus("live · connected", true);
    ws.onclose = () => {
      setStatus("disconnected — retrying", false);
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 2000);
    };
    ws.onerror = () => setStatus("socket error", false);
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      if (!msg || msg.v !== 1) return;
      switch (msg.type) {
        case "state_snapshot":
          applySnapshot(msg);
          break;
        case "candle_update":
          if (msg.candle) candleSeries.update(msg.candle);
          break;
        case "price_update":
          if (msg.price != null) hudPrice.textContent = msg.price;
          break;
        case "engine_event":
          applyEngineEvent(msg);
          // refresh full lines from embedded state
          if (msg.state && msg.state.levels) applyLevels(msg.state.levels);
          if (msg.state) {
            applyHud({
              cycle_id: msg.state.cycle_id,
              n: msg.state.n,
              p0: msg.state.p0,
              shared_tp: msg.state.shared_tp,
            });
          }
          break;
        default:
          break;
      }
    };
  }

  document.getElementById("applyBtn").addEventListener("click", () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(
      JSON.stringify({
        op: "reconfigure",
        symbol: document.getElementById("symbol").value.trim(),
        timeframe: document.getElementById("timeframe").value,
        side: document.getElementById("side").value,
        percentage: document.getElementById("percentage").value,
      })
    );
  });

  connect();
})();
