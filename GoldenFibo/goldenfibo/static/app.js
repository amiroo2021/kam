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
  let currentMode = "REPLAY_TO_LIVE"; // form default; server may run LIVE separately
  let serverMode = "LIVE";
  let lastPhase = "";
  let lastProgress = {};
  let wsConnected = false;
  const showEventsEl = document.getElementById("showEvents");
  let showEvents = !!(showEventsEl && showEventsEl.checked);

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

  /** @type {any[]} */
  let lastMarkers = [];
  let lastOverlayMsg = null;
  let ws = null;
  let reconnectTimer = null;

  /** Display-only 2dp; never used for GoldenFibo math. */
  function format2(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    return n.toFixed(2);
  }

  function defaultReplayStartLocal() {
    // UTC today minus 2 calendar days at 00:00 → datetime-local value
    const now = new Date();
    const y = now.getUTCFullYear();
    const m = now.getUTCMonth();
    const d = now.getUTCDate();
    const start = new Date(Date.UTC(y, m, d - 2, 0, 0, 0));
    const pad = (x) => String(x).padStart(2, "0");
    return (
      start.getUTCFullYear() +
      "-" +
      pad(start.getUTCMonth() + 1) +
      "-" +
      pad(start.getUTCDate()) +
      "T00:00"
    );
  }

  function setStatus(text, ok) {
    statusEl.textContent = text;
    statusEl.className = "status " + (ok === true ? "ok" : ok === false ? "bad" : "");
  }

  /** Mode/phase-aware status — do not imply BACKTEST is "live". */
  function progressLabel() {
    const p = lastProgress || {};
    const done = p.bars_done != null ? p.bars_done : (p.bars_processed != null ? p.bars_processed : null);
    const est = p.bars_est != null ? p.bars_est : p.bars_total;
    const pct = p.pct != null ? Number(p.pct).toFixed(1) : null;
    const pages = p.pages != null ? p.pages : null;
    if (done != null && est != null) {
      let s = done.toLocaleString() + " / ~" + Number(est).toLocaleString() + " bars";
      if (pct != null) s += " · " + pct + "%";
      if (pages != null) s += " · page " + pages;
      return s;
    }
    if (done != null) {
      let s = done.toLocaleString() + " bars";
      if (pages != null) s += " · page " + pages;
      return s;
    }
    if (pages != null) return "page " + pages;
    return "";
  }

  function refreshConnectionStatus() {
    if (!wsConnected) return;
    const mode = (serverMode || currentMode || "LIVE").toUpperCase();
    const phase = (lastPhase || "").toLowerCase();
    const prog = progressLabel();
    let text = "connected";
    let ok = true;
    if (mode === "BACKTEST") {
      if (phase === "backtest_done") text = "backtest · done";
      else if (phase === "downloading_history") text = "backtest · downloading" + (prog ? " · " + prog : "");
      else if (phase === "replaying" || phase === "loading_history") text = "backtest · replaying" + (prog ? " · " + prog : "");
      else if (phase === "error") { text = "backtest · error"; ok = false; }
      else text = "backtest · connected";
    } else if (mode === "REPLAY_TO_LIVE") {
      if (phase === "live") text = "live · connected";
      else if (phase === "downloading_history") text = "replay · downloading" + (prog ? " · " + prog : "");
      else if (phase === "replaying" || phase === "loading_history" || phase === "catching_up")
        text = "replay · catching up" + (prog ? " · " + prog : "");
      else if (phase === "error") { text = "replay · error"; ok = false; }
      else text = "replay · connected";
    } else {
      if (phase === "error") { text = "live · error"; ok = false; }
      else text = "live · connected";
    }
    setStatus(text, ok);
  }

  function resize() {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }
  window.addEventListener("resize", resize);
  resize();


  /** Line series for current-ladder segments + metrics (not full-width price lines). */
  const ladderSeries = {};
  const metricSeries = {};

  function clearSeriesMap(map) {
    Object.keys(map).forEach((k) => {
      try {
        chart.removeSeries(map[k]);
      } catch (_) {}
      delete map[k];
    });
  }

  function sideColors(side) {
    const s = String(side || "BUY").toUpperCase();
    if (s === "SELL") {
      return {
        activated: "#ff8a80", // light red/pink
        current: "#f44336", // strong red
        projected: "#ef5350",
      };
    }
    return {
      activated: "#90caf9", // light blue
      current: "#1e88e5", // strong blue
      projected: "#42a5f5",
    };
  }

  function rightEdgeTime(msg) {
    const t = Number(msg.last_candle_time || msg.metric_windows?.latest_candle_time);
    if (Number.isFinite(t)) return t + 3600 * 6; // extend visually to the right
    return Math.floor(Date.now() / 1000) + 3600;
  }

  function latestTime(msg) {
    const t = Number(msg.last_candle_time || msg.metric_windows?.latest_candle_time);
    if (Number.isFinite(t)) return t;
    return Math.floor(Date.now() / 1000);
  }

  /**
   * Current ladder only: one horizontal segment per backend level.
   * Y = backend price; X from activation_time → right edge.
   * No separate TP series — TP is the (TP) label on P(n-1).
   */
  function applyLadderLevels(msg) {
    clearSeriesMap(ladderSeries);
    const levels = msg.levels;
    if (!Array.isArray(levels) || !levels.length) return;
    const colors = sideColors(msg.side);
    const tRight = rightEdgeTime(msg);

    levels.forEach((lv) => {
      const price = Number(lv.price);
      const t0 = Number(lv.activation_time);
      if (!Number.isFinite(price) || !Number.isFinite(t0)) return;
      const kind = lv.kind || "activated";
      let color = colors.activated;
      let width = 2;
      let style = 0; // solid
      if (kind === "current") {
        color = colors.current;
        width = 3;
      } else if (kind === "projected_next" || kind === "projected_further") {
        color = colors.projected;
        width = 2;
        style = 2; // dashed
      }
      const series = chart.addLineSeries({
        color,
        lineWidth: width,
        lineStyle: style,
        lastValueVisible: true,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
        title: lv.label || lv.id || "",
      });
      series.setData([
        { time: t0, value: price },
        { time: Math.max(t0 + 1, tRight), value: price },
      ]);
      ladderSeries[lv.id || lv.label] = series;
    });
  }

  /**
   * Metrics: segments from latest candle → right edge only (not drawn backward).
   * Values & windows come from backend; JS does not recompute VWAP/POC.
   */
  function applyMetricSegments(msg) {
    clearSeriesMap(metricSeries);
    const t0 = latestTime(msg);
    const tRight = rightEdgeTime(msg);
    if (!Number.isFinite(t0)) return;

    const specs = [
      { key: "ladder_vwap", title: "L-VWAP", color: "#f2c500", dashed: false },
      { key: "active_step_vwap", title: "S-VWAP", color: "#f2c500", dashed: true },
      { key: "ladder_poc", title: "L-POC", color: "#eceff1", dashed: false },
      { key: "active_step_poc", title: "S-POC", color: "#eceff1", dashed: true },
      { key: "ladder_val", title: "VAL", color: "#7e57c2", dashed: true },
      { key: "ladder_vah", title: "VAH", color: "#7e57c2", dashed: true },
    ];
    // Dedupe equal L/S VWAP and L/S POC for readability
    const lv = Number(msg.ladder_vwap);
    const sv = Number(msg.active_step_vwap);
    const lp = Number(msg.ladder_poc);
    const sp = Number(msg.active_step_poc);
    const skip = new Set();
    if (Number.isFinite(lv) && Number.isFinite(sv) && Math.abs(lv - sv) < 1e-6) {
      skip.add("active_step_vwap");
      specs[0].title = "VWAP";
    }
    if (Number.isFinite(lp) && Number.isFinite(sp) && Math.abs(lp - sp) < 1e-6) {
      skip.add("active_step_poc");
      specs[2].title = "POC";
    }

    specs.forEach((s) => {
      if (skip.has(s.key)) return;
      const price = Number(msg[s.key]);
      if (!Number.isFinite(price)) return;
      const series = chart.addLineSeries({
        color: s.color,
        lineWidth: 1,
        lineStyle: s.dashed ? 2 : 0,
        lastValueVisible: true,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
        title: s.title,
      });
      series.setData([
        { time: t0, value: price },
        { time: Math.max(t0 + 1, tRight), value: price },
      ]);
      metricSeries[s.key] = series;
    });
  }


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
    if (markers != null) {
      lastMarkers = dedupeMarkers(markers || []);
    }
    try {
      candleSeries.setMarkers(showEvents ? lastMarkers : []);
    } catch (_) {}
  }

  function applyOverlayState(msg) {
    lastOverlayMsg = msg;
    applyLadderLevels(msg);
    applyMetricSegments(msg);
    applyHud(msg);
    if (Array.isArray(msg.markers)) applyMarkers(msg.markers);
  }

  function applyHud(msg) {
    if (msg.price != null) hudPrice.textContent = format2(msg.price);
    if (msg.cycle_id != null) hudCycle.textContent = String(msg.cycle_id);
    if (msg.n != null) hudStep.textContent = "P" + msg.n;
    if (msg.p0 != null) hudP0.textContent = format2(msg.p0);
    if (msg.shared_tp != null) hudTp.textContent = format2(msg.shared_tp);
    if (msg.phase != null && hudPhase) hudPhase.textContent = msg.phase;
    if (msg.ambiguity_count != null && hudAmb) hudAmb.textContent = String(msg.ambiguity_count);
    if (msg.note_historical && histNote) histNote.textContent = msg.note_historical;
  }

  function applyPhase(msg) {
    if (msg.phase) {
      lastPhase = msg.phase;
      if (hudPhase) hudPhase.textContent = msg.phase;
      refreshConnectionStatus();
    }
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
    ws.onopen = () => { wsConnected = true; refreshConnectionStatus(); };
    ws.onclose = () => {
      wsConnected = false;
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
          if (msg.mode) { serverMode = msg.mode; }
          applyPhase(msg);
          // Keep form mode (default REPLAY→LIVE); do not force form to server LIVE
          syncModeUi();
          refreshConnectionStatus();
          break;
        case "candle_update":
          if (msg.candle) {
            candleSeries.update(msg.candle);
            if (lastOverlayMsg) {
              lastOverlayMsg.last_candle_time = msg.candle.time;
              if (lastOverlayMsg.metric_windows)
                lastOverlayMsg.metric_windows.latest_candle_time = msg.candle.time;
              applyMetricSegments(lastOverlayMsg);
              // extend ladder segments right edge
              applyLadderLevels(lastOverlayMsg);
            }
          }
          break;
        case "price_update":
          if (msg.price != null) hudPrice.textContent = format2(msg.price);
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
            const merged = Object.assign({}, lastOverlayMsg || {}, msg.state);
            if (msg.state.levels) merged.levels = msg.state.levels;
            if (Array.isArray(msg.state.markers) && msg.state.markers.length) {
              merged.markers = dedupeMarkers(lastMarkers.concat(msg.state.markers));
            } else {
              merged.markers = lastMarkers;
            }
            // keep candle tail time if fragment omits it
            if (merged.last_candle_time == null && lastOverlayMsg)
              merged.last_candle_time = lastOverlayMsg.last_candle_time;
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
    // LIVE: no Start/End. REPLAY→LIVE: Start only. BACKTEST: Start + End.
    const needStart = currentMode === "BACKTEST" || currentMode === "REPLAY_TO_LIVE";
    const needEnd = currentMode === "BACKTEST";
    document.getElementById("startWrap").hidden = !needStart;
    document.getElementById("endWrap").hidden = !needEnd;
    document.getElementById("applyBtn").textContent =
      currentMode === "LIVE" ? "Start LIVE" : currentMode === "BACKTEST" ? "Run BACKTEST" : "Start REPLAY→LIVE";
    refreshConnectionStatus();
  }

  document.querySelectorAll(".mode").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      currentMode = btn.dataset.mode;
      syncModeUi();
    });
  });
  syncModeUi();
  // Prefill Start for REPLAY→LIVE: UTC calendar today-2 @ 00:00
  (function initDefaultStart() {
    const el = document.getElementById("startTime");
    if (el && !el.value) el.value = defaultReplayStartLocal();
  })();

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

  if (showEventsEl) {
    showEventsEl.addEventListener("change", () => {
      showEvents = !!showEventsEl.checked;
      applyMarkers(null); // re-render from lastMarkers
    });
  }

  connect();
})();
