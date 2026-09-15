/* GoldenFibo chart client — RENDERS backend state only. No ladder math. */
(function () {
  const el = document.getElementById("chart");
  const statusEl = document.getElementById("connStatus");
  const hudPrice = document.getElementById("hudPrice");
  const hudCycle = document.getElementById("hudCycle");
  const hudStep = document.getElementById("hudStep");
  const hudP0 = document.getElementById("hudP0");
  const hudTp = document.getElementById("hudTp");
  const hudPhase = document.getElementById("hudPhase");
  const hudAmb = document.getElementById("hudAmb");
  const progressBar = document.getElementById("progressBar");
  const progressFill = document.getElementById("progressFill");
  const progressText = document.getElementById("progressText");
  const histNote = document.getElementById("histNote");
  let currentMode = "LIVE";

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
  /** @type {any[]} */
  let lastMarkers = [];
  let lastMetricMsg = null;
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
    P0: { color: "#f0b90b", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "P0", priority: 100 },
    filled: { color: "#5b8def", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "", priority: 10 },
    current: { color: "#ffffff", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "P(n)", priority: 90 },
    tp: { color: "#0ecb81", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "TP", priority: 95 },
    tp_prev: { color: "#0ecb81", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "P(n-1)", priority: 50 },
    next: { color: "#f6465d", lineWidth: 2, lineStyle: 1, axisLabelVisible: true, title: "P(n+1)", priority: 80 },
    further: { color: "#f6465d", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "P(n+2)", priority: 70 },
  };

  function nearlyEqual(a, b, eps) {
    const x = Number(a);
    const y = Number(b);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
    const tol = eps != null ? eps : Math.max(1e-8, Math.abs(x) * 1e-10);
    return Math.abs(x - y) <= tol;
  }

  function clearPriceLines() {
    Object.keys(priceLines).forEach((k) => {
      try {
        candleSeries.removePriceLine(priceLines[k]);
      } catch (_) {}
      delete priceLines[k];
    });
  }

  function clearMetricLines() {
    ["ladder_vwap", "active_step_vwap", "vwap", "ladder_poc", "active_step_poc", "poc"].forEach((k) => {
      if (priceLines[k]) {
        try {
          candleSeries.removePriceLine(priceLines[k]);
        } catch (_) {}
        delete priceLines[k];
      }
    });
  }

  /**
   * One horizontal line per distinct price. Prefer higher-priority roles
   * (P0 / TP / current) so we never stack duplicate P0 labels.
   */
  function applyLevels(levels) {
    if (!Array.isArray(levels)) return;
    // Keep metric lines; only rebuild ladder lines.
    const metricKeys = new Set(["ladder_vwap", "active_step_vwap", "vwap", "ladder_poc", "active_step_poc", "poc"]);
    Object.keys(priceLines).forEach((k) => {
      if (metricKeys.has(k)) return;
      try {
        candleSeries.removePriceLine(priceLines[k]);
      } catch (_) {}
      delete priceLines[k];
    });

    const chosen = [];
    levels.forEach((lv) => {
      const price = Number(lv.price);
      if (!Number.isFinite(price)) return;
      const role = lv.role || "filled";
      if (role === "tp_prev" && levels.some((x) => x.role === "tp")) return;
      const style = ROLE_STYLE[role] || ROLE_STYLE.filled;
      const pri = style.priority || 0;
      const existing = chosen.find((c) => nearlyEqual(c.price, price));
      if (existing) {
        if (pri > existing.pri) {
          existing.lv = lv;
          existing.role = role;
          existing.style = style;
          existing.pri = pri;
        }
        return;
      }
      chosen.push({ price, lv, role, style, pri });
    });

    chosen.forEach((c) => {
      const title = c.style.title || (c.role === "P0" ? "P0" : c.lv.id || "");
      // Single clean label: role only on axis (price shown by scale); avoid "P0 P0 price"
      const label = title || "";
      const line = candleSeries.createPriceLine({
        price: c.price,
        color: c.style.color,
        lineWidth: c.style.lineWidth,
        lineStyle: c.style.lineStyle,
        axisLabelVisible: c.style.axisLabelVisible || !!title,
        title: label,
      });
      priceLines[c.lv.id || `${c.role}-${c.price}`] = line;
    });
  }

  /**
   * Merge Step/Ladder VWAP and POC when values match (step 0 / same window).
   * When they diverge, draw separate lines again.
   */
  function applyMetrics(msg) {
    if (!msg) return;
    lastMetricMsg = msg;
    clearMetricLines();

    const pairs = [
      {
        aKey: "ladder_vwap",
        bKey: "active_step_vwap",
        aVal: msg.ladder_vwap,
        bVal: msg.active_step_vwap,
        color: "#f2c500",
        mergedTitle: "VWAP",
        aTitle: "L-VWAP",
        bTitle: "S-VWAP",
        bColor: "#c9a227",
        mergeKey: "vwap",
      },
      {
        aKey: "ladder_poc",
        bKey: "active_step_poc",
        aVal: msg.ladder_poc,
        bVal: msg.active_step_poc,
        color: "#26a69a",
        mergedTitle: "POC",
        aTitle: "L-POC",
        bTitle: "S-POC",
        bColor: "#66bb6a",
        mergeKey: "poc",
      },
    ];

    pairs.forEach((p) => {
      const a = Number(p.aVal);
      const b = Number(p.bVal);
      const aOk = Number.isFinite(a);
      const bOk = Number.isFinite(b);
      if (aOk && bOk && nearlyEqual(a, b)) {
        priceLines[p.mergeKey] = candleSeries.createPriceLine({
          price: a,
          color: p.color,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: p.mergedTitle,
        });
        return;
      }
      if (aOk) {
        priceLines[p.aKey] = candleSeries.createPriceLine({
          price: a,
          color: p.color,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: p.aTitle,
        });
      }
      if (bOk) {
        priceLines[p.bKey] = candleSeries.createPriceLine({
          price: b,
          color: p.bColor,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: p.bTitle,
        });
      }
    });
  }

  /** Keep one P0 seed marker per timestamp; drop duplicate P0 texts. */
  function dedupeMarkers(markers) {
    if (!Array.isArray(markers)) return [];
    const out = [];
    const p0Times = new Set();
    markers.forEach((m) => {
      const text = String(m.text || "");
      const isP0 = text === "P0" || /^P0\b/.test(text);
      if (isP0) {
        const t = m.time;
        if (p0Times.has(t)) return;
        p0Times.add(t);
        out.push(Object.assign({}, m, { text: "P0" }));
        return;
      }
      out.push(m);
    });
    return out;
  }

  function applyMarkers(markers) {
    lastMarkers = dedupeMarkers(markers || []);
    try {
      candleSeries.setMarkers(lastMarkers);
    } catch (_) {}
  }

  function applyHud(msg) {
    if (msg.price != null) hudPrice.textContent = msg.price;
    if (msg.cycle_id != null) hudCycle.textContent = String(msg.cycle_id);
    if (msg.n != null) hudStep.textContent = "P" + msg.n;
    if (msg.p0 != null) hudP0.textContent = msg.p0;
    if (msg.shared_tp != null) hudTp.textContent = msg.shared_tp;
    if (msg.phase != null && hudPhase) hudPhase.textContent = msg.phase;
    if (msg.ambiguity_count != null && hudAmb) hudAmb.textContent = String(msg.ambiguity_count);
    if (msg.note_historical && histNote) histNote.textContent = msg.note_historical;
  }

  function applyPhase(msg) {
    if (msg.phase && hudPhase) hudPhase.textContent = msg.phase;
    if (msg.ambiguity_count != null && hudAmb) hudAmb.textContent = String(msg.ambiguity_count);
    const p = msg.progress || {};
    const pct = p.pct != null ? Number(p.pct) : null;
    if (progressBar && pct != null && msg.phase && msg.phase !== "live" && msg.phase !== "backtest_done") {
      progressBar.hidden = false;
      progressFill.style.width = Math.min(100, pct) + "%";
      const from = p.from_t || "";
      const to = p.to_t || "";
      progressText.textContent = `Replaying ${from} → ${to} · ${pct.toFixed(0)}% · bars ${p.bars_done || p.events_done || "?"} / ${p.bars_total || p.events_total || "?"}`;
    } else if (progressBar && (msg.phase === "live" || msg.phase === "backtest_done")) {
      if (msg.phase === "backtest_done") {
        progressBar.hidden = false;
        progressFill.style.width = "100%";
        progressText.textContent = `Backtest done · ${msg.bars_processed || 0} bars · ${msg.ambiguity_count || 0} ambiguous intrabar events`;
      } else {
        progressBar.hidden = true;
      }
    }
  }

  function applyOverlayState(msg) {
    if (msg.levels) applyLevels(msg.levels);
    if (
      msg.ladder_vwap !== undefined ||
      msg.active_step_vwap !== undefined ||
      msg.ladder_poc !== undefined ||
      msg.active_step_poc !== undefined
    ) {
      applyMetrics(msg);
    } else if (lastMetricMsg) {
      applyMetrics(lastMetricMsg);
    }
    applyHud(msg);
    if (Array.isArray(msg.markers)) applyMarkers(msg.markers);
  }

  function applySnapshot(msg) {
    if (Array.isArray(msg.candles) && msg.candles.length) {
      candleSeries.setData(msg.candles);
      chart.timeScale().fitContent();
    }
    applyOverlayState(msg);
    if (msg.side) document.getElementById("side").value = msg.side;
    if (msg.percentage) document.getElementById("percentage").value = msg.percentage;
    if (msg.symbol) document.getElementById("symbol").value = msg.symbol;
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
          applyPhase(msg);
          if (msg.mode) { currentMode = msg.mode; syncModeUi(); }
          break;
        case "candle_update":
          if (msg.candle) candleSeries.update(msg.candle);
          break;
        case "price_update":
          if (msg.price != null) hudPrice.textContent = msg.price;
          break;
        case "phase":
          applyPhase(msg);
          break;
        case "run_complete":
          applyPhase(Object.assign({ phase: "backtest_done" }, msg));
          break;
        case "handoff":
          if (hudPhase) hudPhase.textContent = "live (handoff)";
          break;
        case "error":
          setStatus(msg.error || "error", false);
          break;
        case "engine_event":
          if (msg.state) {
            // Merge fragment onto last metric context so VWAP/POC survive level redraws
            const merged = Object.assign({}, lastMetricMsg || {}, msg.state);
            if (Array.isArray(msg.state.markers) && msg.state.markers.length) {
              merged.markers = dedupeMarkers(lastMarkers.concat(msg.state.markers));
            } else {
              merged.markers = lastMarkers;
            }
            applyOverlayState(merged);
          }
          break;
        default:
          break;
      }
    };
  }

  
  function toIsoLocalInput(val) {
    if (!val) return null;
    // datetime-local has no Z; treat as UTC wall clock by appending Z
    const s = val.length === 16 ? val + ":00" : val;
    return s.replace(" ", "T") + "Z";
  }

  function syncModeUi() {
    document.querySelectorAll(".mode").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === currentMode);
    });
    const needStart = currentMode !== "LIVE";
    const needEnd = currentMode === "BACKTEST";
    document.getElementById("startWrap").hidden = !needStart;
    document.getElementById("endWrap").hidden = !needEnd;
    document.getElementById("applyBtn").textContent =
      currentMode === "LIVE" ? "Start LIVE" : currentMode === "BACKTEST" ? "Run BACKTEST" : "Start REPLAY→LIVE";
  }

  document.querySelectorAll(".mode").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      currentMode = btn.dataset.mode;
      syncModeUi();
    });
  });
  syncModeUi();

  document.getElementById("applyBtn").addEventListener("click", () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const payload = {
      op: "start",
      mode: currentMode,
      symbol: document.getElementById("symbol").value.trim(),
      timeframe: document.getElementById("timeframe").value,
      side: document.getElementById("side").value,
      percentage: document.getElementById("percentage").value,
    };
    if (currentMode !== "LIVE") {
      payload.start_time = toIsoLocalInput(document.getElementById("startTime").value);
    }
    if (currentMode === "BACKTEST") {
      payload.end_time = toIsoLocalInput(document.getElementById("endTime").value);
    }
    ws.send(JSON.stringify(payload));
  });

  // patch message handler additions via replace of switch cases

  connect();
})();
