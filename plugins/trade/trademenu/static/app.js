(() => {
  const $ = (id) => document.getElementById(id);
  const exchangeEl = $("exchange");
  const accountEl = $("account");
  const symbolEl = $("symbol");
  const tfEl = $("tf");
  const nativeSym = $("nativeSym");
  const lastPrice = $("lastPrice");
  const candleCount = $("candleCount");
  const positionsBody = $("positionsBody");
  const ordersBody = $("ordersBody");
  const suggestions = $("symbolSuggestions");
  const chartStatus = $("chartStatus");
  const positionsStatus = $("positionsStatus");
  const ordersStatus = $("ordersStatus");
  const panelPositions = $("panelPositions");
  const panelOrders = $("panelOrders");
  const tabPositions = $("tabPositions");
  const tabOrders = $("tabOrders");

  let candleSeries = null;
  let chart = null;
  let positionsTimer = null;
  let ordersTimer = null;
  let quoteTimer = null;
  let nativeInstrument = null;
  let resolvedOk = false;
  let activeTab = "positions";
  let ordersLoadedOnce = false;

  // Per-stream request tokens so stale responses never overwrite newer selection.
  let chartReq = 0;
  let positionsReq = 0;
  let ordersReq = 0;
  let resolveReq = 0;

  const controllers = { chart: null, positions: null, orders: null, resolve: null };

  function selectionKey() {
    return `${exchangeEl.value}|${accountEl.value}|${(symbolEl.value || "").trim()}|${tfEl.value}`;
  }

  function setLine(el, msg, kind) {
    el.textContent = msg || "";
    el.className = "status-line" + (kind ? ` ${kind}` : "");
  }

  function dash(v) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v);
  }

  function abort(name) {
    if (controllers[name]) {
      try { controllers[name].abort(); } catch (_) {}
    }
    controllers[name] = new AbortController();
    return controllers[name].signal;
  }

  async function api(path, signal) {
    const res = await fetch(path, { credentials: "same-origin", signal });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("unauthorized");
    }
    const data = await res.json();
    return { res, data };
  }

  function initChart() {
    const el = $("chart");
    chart = LightweightCharts.createChart(el, {
      layout: { background: { color: "#0a0d12" }, textColor: "#c5d0e0" },
      grid: { vertLines: { color: "#1a2030" }, horzLines: { color: "#1a2030" } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1e2630" },
      timeScale: { borderColor: "#1e2630", timeVisible: true, secondsVisible: false },
    });
    candleSeries = chart.addCandlestickSeries({
      upColor: "#3dd68c",
      downColor: "#ff6b6b",
      borderVisible: false,
      wickUpColor: "#3dd68c",
      wickDownColor: "#ff6b6b",
    });
    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    });
    ro.observe(el);
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }

  async function loadExchanges() {
    const { data } = await api("/api/exchanges");
    const list = data.exchanges || [];
    exchangeEl.innerHTML = "";
    for (const ex of list) {
      const opt = document.createElement("option");
      opt.value = ex;
      opt.textContent = ex;
      exchangeEl.appendChild(opt);
    }
    if (!list.length) {
      setLine(chartStatus, "No exchanges discovered.", "error");
      return;
    }
    if (list.includes("hyperliquid")) exchangeEl.value = "hyperliquid";
    else exchangeEl.selectedIndex = 0;
    await loadAccounts(true);
  }

  async function loadAccounts(selectFirst) {
    const ex = exchangeEl.value;
    const { data } = await api(`/api/accounts?exchange=${encodeURIComponent(ex)}`);
    const accounts = data.accounts || [];
    const prev = accountEl.value;
    accountEl.innerHTML = "";
    for (const a of accounts) {
      const opt = document.createElement("option");
      opt.value = a.account;
      opt.textContent = a.label || a.account;
      accountEl.appendChild(opt);
    }
    if (!accounts.length) {
      setLine(positionsStatus, `No accounts for ${ex}.`, "error");
      return;
    }
    // Prefer FLEX on hyperliquid when present.
    const flex = accounts.find((a) => String(a.account).toUpperCase() === "FLEX");
    if (selectFirst) {
      if (ex === "hyperliquid" && flex) accountEl.value = flex.account;
      else accountEl.selectedIndex = 0;
    } else if (!accounts.some((a) => a.account === prev)) {
      if (ex === "hyperliquid" && flex) accountEl.value = flex.account;
      else accountEl.selectedIndex = 0;
    } else {
      accountEl.value = prev;
    }
  }

  async function resolveSymbol() {
    const reqId = ++resolveReq;
    const key = selectionKey();
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    const sym = (symbolEl.value || "BTCUSD").trim();
    setLine(chartStatus, `Resolving ${sym}…`);
    const signal = abort("resolve");
    const { data } = await api(
      `/api/instruments/resolve?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(sym)}`,
      signal
    );
    if (reqId !== resolveReq || key !== selectionKey()) return null;

    suggestions.innerHTML = "";
    if (Array.isArray(data.candidates)) {
      for (const c of data.candidates) {
        const opt = document.createElement("option");
        opt.value = c.symbol || c.display_name || "";
        suggestions.appendChild(opt);
      }
    }

    if (data.success && data.instrument && data.instrument.symbol) {
      nativeInstrument = data.instrument.symbol;
      resolvedOk = true;
      nativeSym.textContent = data.display || `${sym} → ${nativeInstrument}`;
      const ms = data.timing_ms && data.timing_ms.tradedesk_ms;
      setLine(chartStatus, `Resolved ${nativeSym.textContent}${ms != null ? ` (${ms} ms)` : ""}`, "ok");
      return nativeInstrument;
    }

    nativeInstrument = null;
    resolvedOk = false;
    nativeSym.textContent = data.display || `${sym} → unresolved`;
    setLine(chartStatus, (data.error && data.error.message) || "Instrument unresolved", "error");
    return null;
  }

  async function loadCandles() {
    const reqId = ++chartReq;
    const key = selectionKey();
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    const sym = (symbolEl.value || "BTCUSD").trim();
    const tf = tfEl.value;
    try {
      const native = await resolveSymbol();
      if (reqId !== chartReq || key !== selectionKey()) return;
      if (!native) {
        candleSeries.setData([]);
        candleCount.textContent = "0";
        return;
      }
      setLine(chartStatus, `Loading ${native} ${tf} candles…`);
      const signal = abort("chart");
      const t0 = performance.now();
      const { data } = await api(
        `/api/candles?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(sym)}&tf=${encodeURIComponent(tf)}`,
        signal
      );
      if (reqId !== chartReq || key !== selectionKey()) return;
      const browserMs = Math.round(performance.now() - t0);
      if (!data.success) {
        candleSeries.setData([]);
        candleCount.textContent = "0";
        setLine(chartStatus, (data.error && data.error.message) || "Candles unavailable", "error");
        return;
      }
      const candles = (data.candles || []).map((c) => ({
        time: c.time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));
      candleSeries.setData(candles);
      if (candles.length) {
        chart.timeScale().fitContent();
        lastPrice.textContent = String(candles[candles.length - 1].close);
      }
      candleCount.textContent = String(candles.length);
      if (data.display) nativeSym.textContent = data.display;
      nativeInstrument = data.native_symbol || native;
      resolvedOk = true;
      setLine(
        chartStatus,
        `Chart OK · ${data.native_symbol || native} · ${tf} · ${candles.length} bars · browser ${browserMs} ms`,
        "ok"
      );
    } catch (e) {
      if (e.name === "AbortError") return;
      if (String(e.message) !== "unauthorized") setLine(chartStatus, String(e.message || e), "error");
    }
  }

  async function loadQuote() {
    if (!resolvedOk || !nativeInstrument) return;
    const key = selectionKey();
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    try {
      const { data } = await api(
        `/api/quote?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(nativeInstrument)}`
      );
      if (key !== selectionKey()) return;
      if (data.success && data.price != null) lastPrice.textContent = String(data.price);
    } catch (_) {}
  }

  async function loadPositions() {
    const reqId = ++positionsReq;
    const key = `${exchangeEl.value}|${accountEl.value}`;
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    setLine(positionsStatus, "Loading positions…");
    try {
      const signal = abort("positions");
      const t0 = performance.now();
      const { data } = await api(
        `/api/positions?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}`,
        signal
      );
      if (reqId !== positionsReq || key !== `${exchangeEl.value}|${accountEl.value}`) return;
      const browserMs = Math.round(performance.now() - t0);
      const deskMs = data.timing_ms && data.timing_ms.tradedesk_ms;
      const cacheHit = data.cache_hit ? " cache" : "";
      if (!data.success) {
        positionsBody.innerHTML = `<tr><td colspan="8">${dash(data.error && data.error.message)}</td></tr>`;
        setLine(positionsStatus, (data.error && data.error.message) || "Positions failed", "error");
        return;
      }
      const rows = data.positions || [];
      if (!rows.length) {
        positionsBody.innerHTML = `<tr><td colspan="8">No open positions</td></tr>`;
      } else {
        positionsBody.innerHTML = rows
          .map((p) => {
            const side = String(p.side || "").toLowerCase();
            const sideClass =
              side === "long" || side === "buy" ? "side-long" : side === "short" || side === "sell" ? "side-short" : "";
            return `<tr>
              <td>${dash(p.symbol)}</td>
              <td class="${sideClass}">${dash(p.side)}</td>
              <td>${dash(p.size)}</td>
              <td>${dash(p.entry)}</td>
              <td>${dash(p.mark)}</td>
              <td>${dash(p.pnl)}</td>
              <td>${dash(p.sl)}</td>
              <td>${dash(p.tp)}</td>
            </tr>`;
          })
          .join("");
      }
      setLine(
        positionsStatus,
        `Positions OK · ${rows.length} · browser ${browserMs} ms · desk ${deskMs != null ? deskMs : "—"} ms${cacheHit}`,
        "ok"
      );
    } catch (e) {
      if (e.name === "AbortError") return;
      if (String(e.message) !== "unauthorized") setLine(positionsStatus, String(e.message || e), "error");
    }
  }

  async function loadOrders() {
    const reqId = ++ordersReq;
    const key = `${exchangeEl.value}|${accountEl.value}`;
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    setLine(ordersStatus, "Loading orders…");
    try {
      const signal = abort("orders");
      const t0 = performance.now();
      const { data } = await api(
        `/api/orders?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}`,
        signal
      );
      if (reqId !== ordersReq || key !== `${exchangeEl.value}|${accountEl.value}`) return;
      const browserMs = Math.round(performance.now() - t0);
      const deskMs = data.timing_ms && data.timing_ms.tradedesk_ms;
      const cacheHit = data.cache_hit ? " cache" : "";
      if (!data.success) {
        ordersBody.innerHTML = `<tr><td colspan="6">${dash(data.error && data.error.message)}</td></tr>`;
        setLine(ordersStatus, (data.error && data.error.message) || "Orders failed", "error");
        return;
      }
      const groups = data.groups || [];
      if (!groups.length) {
        ordersBody.innerHTML = `<tr><td colspan="6">No open orders</td></tr>`;
      } else {
        ordersBody.innerHTML = groups
          .map((g) => {
            const side = String(g.side || "").toLowerCase();
            const sideClass = side === "buy" ? "side-buy" : side === "sell" ? "side-sell" : "";
            const sideType = `${String(g.side || "").toUpperCase()} ${String(g.type || "limit").toUpperCase()}`.trim();
            const symLabel = `${dash(g.symbol)} (${dash(g.count)})`;
            let range = "—";
            if (g.min_price && g.max_price && g.min_price !== g.max_price) range = `${g.min_price}–${g.max_price}`;
            else if (g.min_price || g.max_price) range = dash(g.min_price || g.max_price);
            return `<tr>
              <td class="${sideClass}">${symLabel}</td>
              <td class="${sideClass}">${sideType}</td>
              <td>${dash(g.count)}</td>
              <td>${dash(g.total_remaining_size)}</td>
              <td>${range}</td>
              <td>${dash(g.vwap)}</td>
            </tr>`;
          })
          .join("");
      }
      ordersLoadedOnce = true;
      setLine(
        ordersStatus,
        `Orders OK · ${groups.length} groups · open ${dash(data.open_order_count)} · browser ${browserMs} ms · desk ${deskMs != null ? deskMs : "—"} ms${cacheHit}`,
        "ok"
      );
    } catch (e) {
      if (e.name === "AbortError") return;
      if (String(e.message) !== "unauthorized") setLine(ordersStatus, String(e.message || e), "error");
    }
  }

  function setTab(tab) {
    activeTab = tab;
    const isPos = tab === "positions";
    tabPositions.classList.toggle("active", isPos);
    tabOrders.classList.toggle("active", !isPos);
    tabPositions.setAttribute("aria-selected", isPos ? "true" : "false");
    tabOrders.setAttribute("aria-selected", isPos ? "false" : "true");
    panelPositions.hidden = !isPos;
    panelOrders.hidden = isPos;
    schedulePoll();
    if (!isPos && !ordersLoadedOnce) loadOrders();
  }

  function schedulePoll() {
    if (positionsTimer) clearInterval(positionsTimer);
    if (ordersTimer) clearInterval(ordersTimer);
    if (quoteTimer) clearInterval(quoteTimer);
    quoteTimer = setInterval(loadQuote, 15000);
    if (activeTab === "positions") {
      positionsTimer = setInterval(loadPositions, 20000);
    } else {
      ordersTimer = setInterval(loadOrders, 20000);
    }
  }

  function onSelectionChanged() {
    ordersLoadedOnce = false;
    // Fire independently — do not await chain.
    loadCandles();
    loadPositions();
    if (activeTab === "orders") loadOrders();
  }

  exchangeEl.addEventListener("change", async () => {
    await loadAccounts(true);
    onSelectionChanged();
  });
  accountEl.addEventListener("change", onSelectionChanged);
  tfEl.addEventListener("change", () => loadCandles());
  symbolEl.addEventListener("change", onSelectionChanged);
  symbolEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      onSelectionChanged();
    }
  });
  $("reloadBtn").addEventListener("click", onSelectionChanged);
  tabPositions.addEventListener("click", () => setTab("positions"));
  tabOrders.addEventListener("click", () => setTab("orders"));

  initChart();
  loadExchanges()
    .then(() => {
      onSelectionChanged();
      schedulePoll();
    })
    .catch((e) => setLine(chartStatus, String(e.message || e), "error"));
})();
