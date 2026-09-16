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
  const positionsUpdated = $("positionsUpdated");
  const ordersUpdated = $("ordersUpdated");
  const panelPositions = $("panelPositions");
  const panelOrders = $("panelOrders");
  const tabPositions = $("tabPositions");
  const tabOrders = $("tabOrders");
  const toast = $("actionToast");
  const modal = $("modal");
  const modalTitle = $("modalTitle");
  const modalBody = $("modalBody");
  const modalFields = $("modalFields");
  const modalInput = $("modalInput");
  const modalError = $("modalError");
  const modalCancel = $("modalCancel");
  const modalConfirm = $("modalConfirm");

  let candleSeries = null;
  let chart = null;
  let positionsTimer = null;
  let ordersTimer = null;
  let quoteTimer = null;
  let nativeInstrument = null;
  let formatMeta = null;
  let resolvedOk = false;
  let activeTab = "positions";
  let ordersLoadedOnce = false;
  let lastPositions = [];
  let lastOrderGroups = [];
  let positionLines = [];
  let previewLines = [];
  let liveOrderLines = [];
  let tradeSide = "buy";
  let tradeMode = "single";
  let activePreview = null; // {id, kind, data}
  let csrfToken = "";
  let modalState = null;

  let chartReq = 0;
  let positionsReq = 0;
  let ordersReq = 0;
  let resolveReq = 0;
  let financialsReq = 0;
  let quoteReq = 0;
  const controllers = { chart: null, positions: null, orders: null, resolve: null, financials: null, quote: null };
  const POS_POLL_MS = 15 * 60 * 1000;
  const ORD_POLL_MS = 15 * 60 * 1000;
  let lastFinancials = null; // { accountKey, data }
  let lastFinancialsStale = false;
  let quoteFresh = false; // true only when Last belongs to current nativeInstrument

  function selectionKey() {
    return `${exchangeEl.value}|${accountEl.value}|${(symbolEl.value || "").trim()}|${tfEl.value}`;
  }
  function accountKey() {
    return `${exchangeEl.value}|${accountEl.value}`;
  }
  function instrumentKey() {
    return `${exchangeEl.value}|${accountEl.value}|${nativeInstrument || ""}|${tfEl.value}`;
  }

  function setLastPrice(value, meta, { fromNative } = {}) {
    if (fromNative && fromNative !== nativeInstrument) return;
    if (value == null || value === "" || value === "—") {
      lastPrice.textContent = "—";
      quoteFresh = false;
      updateTradeEnablement();
      return;
    }
    lastPrice.textContent = fmtPriceClient(value, meta || formatMeta);
    quoteFresh = true;
    updateTradeEnablement();
  }

  function clearMarketState(reason) {
    quoteFresh = false;
    lastPrice.textContent = "—";
    try {
      candleSeries && candleSeries.setData([]);
    } catch (_) {}
    try {
      // Drop prior instrument autoscale (e.g. 1k–1.5k leftover on BTC ~76k).
      if (candleSeries && typeof candleSeries.applyOptions === "function") {
        candleSeries.applyOptions({ autoscaleInfoProvider: undefined });
      }
      if (chart && chart.priceScale) {
        chart.priceScale("right").applyOptions({ autoScale: true });
      }
    } catch (_) {}
    candleCount.textContent = "0";
    clearPositionLines();
    clearPreviewLines();
    invalidateTradePreview();
    updateTradeEnablement(reason);
  }

  function updateTradeEnablement(reason) {
    const ready = !!(resolvedOk && nativeInstrument && quoteFresh);
    const waitingMsg =
      reason ||
      (!resolvedOk
        ? "Waiting for instrument resolution…"
        : !quoteFresh
          ? "Waiting for market data…"
          : "");
    ["previewOrderBtn", "previewLadderBtn", "useCurrentPrice", "ladderStartCurrent"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.disabled = !ready;
      if (!ready && waitingMsg) el.title = waitingMsg;
      else el.removeAttribute("title");
    });
  }
  function setLine(el, msg, kind) {
    el.textContent = msg || "";
    el.className = "status-line" + (kind ? ` ${kind}` : "");
  }
  function dash(v) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v);
  }
  function nowStamp() {
    return new Date().toLocaleTimeString([], { hour12: false });
  }
  function abort(name) {
    if (controllers[name]) {
      try { controllers[name].abort(); } catch (_) {}
    }
    controllers[name] = new AbortController();
    return controllers[name].signal;
  }
  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1") + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function showToast(msg, kind) {
    toast.hidden = !msg;
    toast.textContent = msg || "";
    toast.className = "toast" + (kind ? ` ${kind}` : "");
  }
  function fmtPriceClient(v, meta) {
    if (v === null || v === undefined || v === "" || v === "—") return "—";
    const n = Number(String(v).replace(/,/g, ""));
    if (!Number.isFinite(n)) return String(v);
    let decs = 1;
    if (meta && meta.price_increment != null) {
      const t = String(meta.price_increment);
      if (t.includes(".")) decs = t.split(".")[1].replace(/0+$/, "").length || 0;
      else decs = 0;
    } else if (meta && meta.price_decimals != null) decs = Number(meta.price_decimals) || 1;
    return n.toLocaleString(undefined, { minimumFractionDigits: decs, maximumFractionDigits: decs });
  }

  async function api(path, signal, opts) {
    const headers = Object.assign({}, (opts && opts.headers) || {});
    const method = (opts && opts.method) || "GET";
    if (method !== "GET" && method !== "HEAD") {
      headers["Content-Type"] = headers["Content-Type"] || "application/json";
      if (!headers["X-CSRF-Token"]) {
        headers["X-CSRF-Token"] = csrfToken || readCsrfCookie() || "";
      }
    }
    const res = await fetch(path, {
      credentials: "same-origin",
      signal,
      method,
      headers,
      body: opts && opts.body != null ? (typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body)) : undefined,
    });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("unauthorized");
    }
    const data = await res.json().catch(() => ({}));
    return { res, data };
  }

  function readCsrfCookie() {
    const raw = cookie("trademenu_csrf");
    if (!raw) return "";
    return raw.replace(/^"|"$/g, "").trim();
  }

  async function ensureCsrf(force) {
    if (!force && csrfToken) return csrfToken;
    const { res, data } = await api("/api/session");
    if (res.status === 401) throw new Error("unauthorized");
    const token = (data && data.csrf) || readCsrfCookie() || "";
    if (!token) {
      throw new Error("CSRF token unavailable. Please reload or log in again.");
    }
    csrfToken = token;
    return csrfToken;
  }

  /**
   * Single authenticated write helper for ALL TradeMenu POST mutations.
   * Always refreshes CSRF from /api/session before the write (session binding).
   * Does NOT auto-retry after a write may have reached the handler (no double trade).
   */
  async function apiPost(path, payload, signal) {
    const token = await ensureCsrf(true);
    const { res, data } = await api(path, signal, {
      method: "POST",
      headers: { "X-CSRF-Token": token },
      body: payload || {},
    });
    // Stale CSRF cookie/session (not "maybe executed"): one safe refresh + single retry.
    if (res.status === 403 && data && data.error && data.error.code === "CSRF_FAILED") {
      csrfToken = "";
      const token2 = await ensureCsrf(true);
      if (token2 && token2 !== token) {
        return api(path, signal, {
          method: "POST",
          headers: { "X-CSRF-Token": token2 },
          body: payload || {},
        });
      }
    }
    if (res.status === 403 && data && data.error && data.error.code === "CSRF_SESSION_STALE") {
      csrfToken = "";
      await ensureCsrf(true);
      throw new Error((data.error && data.error.message) || "Session expired. Please retry once.");
    }
    return { res, data };
  }

  function clearPositionLines() {
    for (const line of positionLines) {
      try { candleSeries.removePriceLine(line); } catch (_) {}
    }
    positionLines = [];
  }

  function symbolsMatch(posSym, native, requested, rowNative) {
    const candidates = [
      String(posSym || "").trim(),
      String(rowNative || "").trim(),
    ].filter(Boolean);
    const targets = [
      String(native || "").trim(),
      String(requested || "").trim(),
    ].filter(Boolean);
    if (!candidates.length || !targets.length) return false;
    // Exact native identity first (BTC-USD === BTC-USD) — highest priority.
    for (const c of candidates) {
      const cu = c.toUpperCase();
      for (const t of targets) {
        if (cu === String(t).toUpperCase()) return true;
      }
    }
    const peel = (s) => {
      const u = s.toUpperCase();
      const tail = u.includes(":") ? u.split(":").pop() : u;
      // PERP_ZEC_USDC → ZEC
      let t = tail;
      if (t.startsWith("PERP_") && t.endsWith("_USDC")) t = t.slice(5, -5);
      return t.replace(/[-_/]/g, "").replace(/(USDT|USDC|USD)$/i, "");
    };
    for (const c of candidates) {
      const cu = c.toUpperCase();
      for (const t of targets) {
        const tu = t.toUpperCase();
        if (peel(cu) && peel(cu) === peel(tu)) return true;
      }
    }
    return false;
  }

  function clearPreviewLines() {
    for (const line of previewLines) {
      try { candleSeries.removePriceLine(line); } catch (_) {}
    }
    previewLines = [];
  }
  function clearLiveOrderLines() {
    for (const line of liveOrderLines) {
      try { candleSeries.removePriceLine(line); } catch (_) {}
    }
    liveOrderLines = [];
  }

  function updateOverlay() {
    if (!candleSeries) return;
    clearPositionLines();
    clearLiveOrderLines();
    if (!resolvedOk || !nativeInstrument) return;
    const req = (symbolEl.value || "").trim();
    const match = lastPositions.find((p) =>
      symbolsMatch(p.symbol, nativeInstrument, req, p.native_symbol || p.exchange_instrument)
    );
    if (match) {
      const side = String(match.side || "").toUpperCase();
      const meta = match.format_meta || formatMeta || {};
      const add = (price, color, title) => {
        if (price == null || price === "" || price === "—") return;
        const n = Number(String(price).replace(/,/g, ""));
        if (!Number.isFinite(n)) return;
        const line = candleSeries.createPriceLine({
          price: n,
          color,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title,
        });
        positionLines.push(line);
      };
      add(match.tp, "#3dd68c", `TP ${fmtPriceClient(match.tp, meta)}`);
      add(match.entry, side.includes("SHORT") || side === "SELL" ? "#ff6b6b" : "#4ea1ff",
        `${dash(match.symbol)} ${side.includes("SHORT") || side === "SELL" ? "SHORT" : "LONG"} ENTRY ${fmtPriceClient(match.entry, meta)}`);
      add(match.sl, "#ff9f43", `SL ${fmtPriceClient(match.sl, meta)}`);
    }
    // Live open ladder VWAP overlays for active symbol
    if ($("showOrdersOverlay") && $("showOrdersOverlay").checked) {
      const groups = (lastOrderGroups || []).filter((g) =>
        symbolsMatch(g.symbol, nativeInstrument, req, g.native_symbol || g.exchange_instrument)
      );
      for (const g of groups) {
        if (String(g.classification || "entry_limit") !== "entry_limit") continue;
        const vwap = Number(String(g.vwap || "").replace(/,/g, ""));
        if (!Number.isFinite(vwap)) continue;
        const side = String(g.side || "").toLowerCase();
        const line = candleSeries.createPriceLine({
          price: vwap,
          color: side === "buy" ? "#4ea1ff" : "#ff6b6b",
          lineWidth: 1,
          lineStyle: 0,
          axisLabelVisible: true,
          title: side === "buy" ? `OPEN BUY VWAP ${fmtPriceClient(vwap, formatMeta)}` : `OPEN SELL VWAP ${fmtPriceClient(vwap, formatMeta)}`,
        });
        liveOrderLines.push(line);
      }
    }
  }

  function drawLadderPreview(data) {
    clearPreviewLines();
    if (!candleSeries || !data || !Array.isArray(data.children)) return;
    const children = data.children;
    // Subtle child lines (no per-line labels)
    for (const c of children) {
      const n = Number(String(c.price).replace(/,/g, ""));
      if (!Number.isFinite(n)) continue;
      const line = candleSeries.createPriceLine({
        price: n,
        color: "rgba(61,214,140,0.25)",
        lineWidth: 1,
        lineStyle: 3,
        axisLabelVisible: false,
        title: "",
      });
      previewLines.push(line);
    }
    const vwap = Number(String(data.vwap || "").replace(/,/g, ""));
    if (Number.isFinite(vwap)) {
      const line = candleSeries.createPriceLine({
        price: vwap,
        color: "#9b59b6",
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: `PREVIEW VWAP ${fmtPriceClient(vwap, formatMeta)}`,
      });
      previewLines.push(line);
    }
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

  function renderAccountFinancials(data, { stale = false, loading = false } = {}) {
    const el = $("accountFinancials");
    if (!el) return;
    el.classList.toggle("loading", !!loading);
    el.classList.toggle("stale", !!stale && !loading);
    el.classList.remove("unavailable");
    const fields = (data && Array.isArray(data.fields)) ? data.fields : [];
    const currency = (data && data.currency) ? String(data.currency) : "";
    if (!fields.length) {
      el.classList.add("unavailable");
      el.textContent = "Balance unavailable";
      el.title = (data && data.error && data.error.message) || "Balance unavailable";
      return;
    }
    el.innerHTML = "";
    el.title = stale ? "Account financials may be stale" : (currency ? `Currency: ${currency}` : "");
    for (const f of fields) {
      const item = document.createElement("span");
      item.className = "fin-item";
      const tip = f.title || f.label || "";
      if (tip) item.title = tip;
      const lab = document.createElement("span");
      lab.className = "fin-label";
      lab.textContent = f.short_label || f.label || f.key || "";
      const val = document.createElement("span");
      val.className = "fin-value";
      val.textContent = f.display || f.value || "—";
      item.appendChild(lab);
      item.appendChild(val);
      if (currency && f === fields[0]) {
        const u = document.createElement("span");
        u.className = "fin-unit";
        u.textContent = currency;
        item.appendChild(u);
      }
      el.appendChild(item);
    }
  }

  async function loadAccountFinancials(force) {
    const el = $("accountFinancials");
    const reqId = ++financialsReq;
    const key = accountKey();
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    if (!ex || !acct) {
      if (el) {
        el.classList.add("unavailable");
        el.textContent = "";
      }
      return;
    }
    // Keep prior values for this account while refreshing; clear only on account switch.
    if (lastFinancials && lastFinancials.accountKey === key) {
      renderAccountFinancials(lastFinancials.data, { stale: lastFinancialsStale, loading: true });
    } else if (el) {
      el.classList.remove("stale", "unavailable");
      el.classList.add("loading");
      if (!el.childElementCount) el.textContent = "…";
    }
    try {
      const signal = abort("financials");
      const q = force ? "&force=1" : "";
      const { data } = await api(
        `/api/account/financials?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}${q}`,
        signal
      );
      if (reqId !== financialsReq || key !== accountKey()) return;
      if (data && data.success && Array.isArray(data.fields) && data.fields.length) {
        lastFinancials = { accountKey: key, data };
        lastFinancialsStale = false;
        renderAccountFinancials(data, { stale: false, loading: false });
      } else if (lastFinancials && lastFinancials.accountKey === key) {
        lastFinancialsStale = true;
        renderAccountFinancials(lastFinancials.data, { stale: true, loading: false });
      } else {
        lastFinancials = null;
        lastFinancialsStale = false;
        renderAccountFinancials(data || {}, { stale: false, loading: false });
      }
    } catch (e) {
      if (reqId !== financialsReq || key !== accountKey()) return;
      if (String(e.name || "") === "AbortError") return;
      if (lastFinancials && lastFinancials.accountKey === key) {
        lastFinancialsStale = true;
        renderAccountFinancials(lastFinancials.data, { stale: true, loading: false });
      } else if (el) {
        el.classList.remove("loading", "stale");
        el.classList.add("unavailable");
        el.textContent = "Balance unavailable";
        el.title = String(e.message || e);
      }
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
      formatMeta = data.format_meta || null;
      resolvedOk = true;
      nativeSym.textContent = data.display || `${sym} → ${nativeInstrument}`;
      const ms = data.timing_ms && data.timing_ms.tradedesk_ms;
      const cache = data.cache_hit ? " · cache" : "";
      setLine(chartStatus, `Resolved ${nativeSym.textContent}${ms != null ? ` (${ms} ms${cache})` : ""}`, "ok");
      return nativeInstrument;
    }

    nativeInstrument = null;
    formatMeta = null;
    resolvedOk = false;
    clearPositionLines();
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
    // Invalidate previous instrument Last immediately so BTC cannot stick on ZEC.
    clearMarketState("Loading market data…");
    try {
      setLine(chartStatus, `Loading ${sym} ${tf} candles…`);
      const signal = abort("chart");
      const t0 = performance.now();
      const { data } = await api(
        `/api/candles?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(sym)}&tf=${encodeURIComponent(tf)}`,
        signal
      );
      if (reqId !== chartReq || key !== selectionKey()) return;
      const browserMs = Math.round(performance.now() - t0);
      if (!data.success) {
        try {
          candleSeries.setData([]);
          if (chart && chart.priceScale) {
            chart.priceScale("right").applyOptions({ autoScale: true });
          }
        } catch (_) {}
        candleCount.textContent = "0";
        // Do NOT wipe native identity just because candles failed — quote,
        // overlays, and ticket still bind to the resolved instrument.
        const failNative = data.native_symbol || null;
        if (failNative) {
          nativeInstrument = failNative;
          resolvedOk = true;
          if (data.display) nativeSym.textContent = data.display;
          if (data.format_meta) formatMeta = data.format_meta;
        } else {
          // Resolve independently so overlays/quote can still attach.
          const native = await resolveSymbol();
          if (reqId !== chartReq || key !== selectionKey()) return;
          if (!native) {
            nativeSym.textContent = data.display || `${sym} → unresolved`;
          }
        }
        const label = nativeInstrument || sym;
        const msg =
          (data.error && data.error.message) ||
          `Chart unavailable for ${label} on ${ex}`;
        setLine(chartStatus, `Chart unavailable for ${label} on ${ex}: ${msg}`, "error");
        updateTradeEnablement(msg);
        updateOverlay();
        if (nativeInstrument) {
          await loadQuote(true);
        }
        return;
      }
      const candles = (data.candles || []).map((c) => ({
        time: c.time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));
      formatMeta = data.format_meta || formatMeta;
      if (data.display) nativeSym.textContent = data.display;
      nativeInstrument = data.native_symbol || nativeInstrument;
      resolvedOk = true;
      try {
        candleSeries.setData(candles);
        if (candles.length) {
          chart.timeScale().fitContent();
          if (chart && chart.priceScale) {
            chart.priceScale("right").applyOptions({ autoScale: true });
          }
          setLastPrice(candles[candles.length - 1].close, formatMeta, { fromNative: nativeInstrument });
        } else {
          candleSeries.setData([]);
          if (chart && chart.priceScale) {
            chart.priceScale("right").applyOptions({ autoScale: true });
          }
          setLastPrice(null);
          setLine(
            chartStatus,
            `Chart unavailable for ${nativeInstrument || sym} on ${ex}: 0 candles`,
            "error"
          );
        }
      } catch (_) {}
      candleCount.textContent = String(candles.length);
      // Overlays use native identity + lastPositions — independent of candle count.
      updateOverlay();
      updateTradeEnablement();
      const rMs = data.resolve_timing_ms && data.resolve_timing_ms.tradedesk_ms;
      const rCache = data.resolve_cache_hit ? " resolve-cache" : "";
      if (candles.length) {
        setLine(
          chartStatus,
          `Chart OK · ${data.native_symbol || "—"} · ${tf} · ${candles.length} bars · browser ${browserMs} ms · resolve ${rMs != null ? rMs : "—"} ms${rCache}`,
          "ok"
        );
      }
      // Authoritative quote for Current button (may refine Last).
      loadQuote(true);
    } catch (e) {
      if (e.name === "AbortError") return;
      if (String(e.message) !== "unauthorized") setLine(chartStatus, String(e.message || e), "error");
    }
  }

  async function loadQuote(force) {
    if (!resolvedOk || !nativeInstrument) return;
    const reqId = ++quoteReq;
    const key = selectionKey();
    const nativeAtStart = nativeInstrument;
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    try {
      const signal = abort("quote");
      const { data } = await api(
        `/api/quote?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(nativeAtStart)}`,
        signal
      );
      if (reqId !== quoteReq || key !== selectionKey()) return;
      if (nativeInstrument !== nativeAtStart) return;
      if (data.success && data.price != null) {
        setLastPrice(data.price, formatMeta, { fromNative: nativeAtStart });
      } else if (force && !quoteFresh) {
        setLastPrice(null);
      }
    } catch (e) {
      if (e && e.name === "AbortError") return;
    }
  }

  function openModal(cfg) {
    modalState = cfg;
    modalTitle.textContent = cfg.title || "Confirm";
    modalBody.textContent = cfg.body || "";
    modalError.hidden = true;
    modalError.textContent = "";
    modalConfirm.disabled = false;
    modalConfirm.textContent = cfg.confirmLabel || "Confirm";
    modalConfirm.className = cfg.danger ? "btn-danger" : "";
    modalCancel.disabled = false;
    modalCancel.textContent = cfg.cancelLabel || "Cancel";
    // Prefetch CSRF while the user fills the modal (avoids empty token on submit).
    ensureCsrf(true).catch(() => {});
    if (cfg.input != null) {
      modalFields.hidden = false;
      $("modalFieldLabel").firstChild.textContent = cfg.inputLabel || "New value";
      modalInput.value = cfg.input === true ? "" : String(cfg.input);
      setTimeout(() => modalInput.focus(), 0);
    } else {
      modalFields.hidden = true;
      modalInput.value = "";
    }
    modal.hidden = false;
  }

  function closeModal() {
    modal.hidden = true;
    modalState = null;
    modalConfirm.disabled = false;
  }

  async function runWrite(path, body, selSnapshot) {
    showToast("Processing…", "busy");
    const { data, res } = await apiPost(path, body);
    if (selSnapshot && selSnapshot !== accountKey()) {
      showToast("Stale response ignored (selection changed).", "error");
      return null;
    }
    if (res && res.status === 401) throw new Error("unauthorized");
    return data;
  }

  async function confirmProtectionAfterWrite(kind, symbol, price) {
    showToast(`${kind} request accepted at ${price}. Confirming…`, "busy");
    // Invalidate short TTL so we don't re-read stale pre-write cache.
    // (server invalidates on write; force client re-fetch)
    await loadPositions(true);
    await loadOrders(true);
    loadAccountFinancials(true);
    const row = (lastPositions || []).find((p) => symbolsMatch(p.symbol, symbol, symbol));
    const field = kind === "TP" ? "tp" : "sl";
    const got = row ? String(row[field] ?? "").replace(/,/g, "") : "";
    const want = String(price).replace(/,/g, "");
    if (got && want && Number(got) === Number(want)) {
      showToast(`${kind} confirmed at ${got}`, "ok");
      return true;
    }
    // One brief readback retry (eventual consistency); READ only.
    await new Promise((r) => setTimeout(r, 800));
    await loadPositions(true);
    const row2 = (lastPositions || []).find((p) => symbolsMatch(p.symbol, symbol, symbol));
    const got2 = row2 ? String(row2[field] ?? "").replace(/,/g, "") : "";
    if (got2 && want && Number(got2) === Number(want)) {
      showToast(`${kind} confirmed at ${got2}`, "ok");
      return true;
    }
    showToast(
      `${kind} request was accepted, but current protection could not be confirmed from exchange state.`,
      "error"
    );
    return false;
  }

  function wirePositionActions() {
    positionsBody.querySelectorAll("[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const act = btn.getAttribute("data-act");
        const sym = btn.getAttribute("data-symbol");
        const side = btn.getAttribute("data-side") || "";
        const size = btn.getAttribute("data-size") || "";
        const mark = btn.getAttribute("data-mark") || "";
        const curTp = btn.getAttribute("data-tp") || "—";
        const curSl = btn.getAttribute("data-sl") || "—";
        const snap = accountKey();
        if (act === "tp") {
          openModal({
            title: `${sym} ${String(side).toUpperCase()}`,
            body: `Current TP: ${curTp}`,
            input: true,
            inputLabel: "New TP",
            confirmLabel: "Set TP",
            onConfirm: async (price) => {
              if (!price) throw new Error("Enter a TP price.");
              // second confirm
              const ok = window.confirm(`Set ${sym} ${String(side).toUpperCase()} TP to ${price}?`);
              if (!ok) return { aborted: true };
              modalConfirm.disabled = true;
              modalConfirm.textContent = "Setting TP…";
              const data = await runWrite("/api/position/set_tp", {
                exchange: exchangeEl.value,
                account: accountEl.value,
                symbol: sym,
                price,
              }, snap);
              if (!data) return { aborted: true };
              if (!data.success) throw new Error((data.error && data.error.message) || "Failed to update TP");
              await confirmProtectionAfterWrite("TP", sym, price);
              return data;
            },
          });
        } else if (act === "sl") {
          openModal({
            title: `${sym} ${String(side).toUpperCase()}`,
            body: `Current SL: ${curSl}`,
            input: true,
            inputLabel: "New SL",
            confirmLabel: "Set SL",
            onConfirm: async (price) => {
              if (!price) throw new Error("Enter an SL price.");
              const ok = window.confirm(`Set ${sym} ${String(side).toUpperCase()} stop loss to ${price}?`);
              if (!ok) return { aborted: true };
              modalConfirm.disabled = true;
              modalConfirm.textContent = "Setting SL…";
              const data = await runWrite("/api/position/set_sl", {
                exchange: exchangeEl.value,
                account: accountEl.value,
                symbol: sym,
                price,
              }, snap);
              if (!data) return { aborted: true };
              if (!data.success) throw new Error((data.error && data.error.message) || "Failed to update SL");
              await confirmProtectionAfterWrite("SL", sym, price);
              return data;
            },
          });
        } else if (act === "close") {
          openModal({
            title: `Close ${sym} ${String(side).toUpperCase()}?`,
            body: `Size: ${size}\nMark: ${mark}\n\nThis will close the full position.`,
            danger: true,
            confirmLabel: "Close Position",
            onConfirm: async () => {
              modalConfirm.disabled = true;
              modalConfirm.textContent = "Closing…";
              const data = await runWrite("/api/position/close", {
                exchange: exchangeEl.value,
                account: accountEl.value,
                symbol: sym,
              }, snap);
              if (!data) return { aborted: true };
              if (!data.success) throw new Error((data.error && data.error.message) || "Close failed");
              showToast(`${sym} close confirmed`, "ok");
              await loadPositions(true);
              await loadOrders(true);
              loadAccountFinancials(true);
              return data;
            },
          });
        }
      });
    });
  }

  function activateInstrument(display, native) {
    const d = String(display || "").trim();
    const n = String(native || "").trim();
    if (!d && !n) return;
    // Prefer venue-native id when present (BTC-USD, PERP_ZEC_USDC, xyz:SP500).
    // Never invent ZECUSD by concatenating USD onto ZEC when native is known.
    if (n) {
      symbolEl.value = n;
    } else if (d.includes(":")) {
      symbolEl.value = d;
    } else {
      symbolEl.value = d;
    }
    onSelectionChanged();
  }

  function wireSymbolClicks(root) {
    root.querySelectorAll("button.link-sym[data-symbol]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const display = btn.getAttribute("data-symbol") || "";
        const native = btn.getAttribute("data-native") || "";
        activateInstrument(display, native);
      });
    });
  }

  function activeTradeSymbol() {
    // Prefer last resolved native so preview/execute share the chart identity.
    if (resolvedOk && nativeInstrument) return nativeInstrument;
    return (symbolEl.value || "").trim();
  }

  function wireOrderActions() {
    ordersBody.querySelectorAll("[data-cancel-group]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sym = btn.getAttribute("data-symbol");
        const side = btn.getAttribute("data-side");
        const count = btn.getAttribute("data-count");
        const vol = btn.getAttribute("data-vol");
        const range = btn.getAttribute("data-range");
        const vwap = btn.getAttribute("data-vwap");
        const classification = btn.getAttribute("data-classification") || "entry_limit";
        const displayType = btn.getAttribute("data-display-type") || "";
        const trigger = btn.getAttribute("data-trigger") || "";
        const limit = btn.getAttribute("data-limit") || "";
        const idsRaw = btn.getAttribute("data-order-ids") || "";
        const orderIds = idsRaw
          ? idsRaw.split(",").map((x) => x.trim()).filter(Boolean).map((x) => Number(x)).filter((n) => Number.isFinite(n))
          : [];
        const snap = accountKey();
        const isTp = classification === "take_profit";
        const isSl = classification === "stop_loss";
        let title = `Cancel ${count} ${sym} ${displayType || String(side).toUpperCase()} orders?`;
        let body = `Total volume: ${vol}\nPrice range: ${range}\nVWAP: ${vwap}`;
        let cancelLabel = "Keep Orders";
        let confirmLabel = `Cancel ${count} Orders`;
        if (isSl) {
          title = `Cancel ${sym} Stop Loss?`;
          body =
            `Stop price: ${trigger || limit || range}\n` +
            `Orders: ${count}\nSize: ${vol}\n\n` +
            `This position may remain open without stop-loss protection.`;
          cancelLabel = "Keep Stop Loss";
          confirmLabel = "Cancel Stop Loss";
        } else if (isTp) {
          title = `Cancel ${sym} Take Profit?`;
          body = `TP price: ${trigger || limit || range}\nOrders: ${count}\nSize: ${vol}`;
          cancelLabel = "Keep Take Profit";
          confirmLabel = "Cancel Take Profit";
        }
        openModal({
          title,
          body,
          danger: true,
          cancelLabel,
          confirmLabel,
          onConfirm: async () => {
            modalConfirm.disabled = true;
            modalConfirm.textContent = "Cancelling…";
            const data = await runWrite(
              "/api/orders/cancel_group",
              {
                exchange: exchangeEl.value,
                account: accountEl.value,
                symbol: sym,
                side,
                type: displayType || "limit",
                classification,
                order_ids: orderIds,
              },
              snap
            );
            if (!data) return { aborted: true };
            if (!data.success) throw new Error((data.error && data.error.message) || "Cancel failed");
            const msg = data.message || `Cancelled ${data.cancelled || count} orders.`;
            showToast(msg, data.partial ? "error" : "ok");
            await loadOrders(true);
            await loadPositions(true);
            loadAccountFinancials(true);
            return data;
          },
        });
      });
    });
  }

  modalCancel.addEventListener("click", closeModal);
  modal.addEventListener("click", (ev) => {
    if (ev.target === modal) closeModal();
  });
  modalConfirm.addEventListener("click", async () => {
    if (!modalState || !modalState.onConfirm) return;
    if (modalConfirm.disabled) return;
    modalError.hidden = true;
    modalError.textContent = "";
    const prevLabel = modalState.confirmLabel || "Confirm";
    modalConfirm.disabled = true;
    modalConfirm.textContent = "Processing…";
    try {
      const price = modalFields.hidden ? undefined : (modalInput.value || "").trim();
      const result = await modalState.onConfirm(price);
      if (result && result.aborted) {
        modalConfirm.disabled = false;
        modalConfirm.textContent = prevLabel;
        return;
      }
      closeModal();
    } catch (e) {
      modalConfirm.disabled = false;
      modalConfirm.textContent = prevLabel;
      modalError.hidden = false;
      modalError.textContent = String(e.message || e);
      showToast(String(e.message || e), "error");
    }
  });

  async function loadPositions(force) {
    const reqId = ++positionsReq;
    const key = accountKey();
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    if (!force) setLine(positionsStatus, "Loading positions…");
    try {
      const signal = abort("positions");
      const t0 = performance.now();
      const { data } = await api(
        `/api/positions?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}`,
        signal
      );
      if (reqId !== positionsReq || key !== accountKey()) return;
      const browserMs = Math.round(performance.now() - t0);
      const deskMs = data.timing_ms && data.timing_ms.tradedesk_ms;
      const cacheHit = data.cache_hit ? " cache" : "";
      if (!data.success) {
        positionsBody.innerHTML = `<tr><td colspan="9">${dash(data.error && data.error.message)}</td></tr>`;
        setLine(positionsStatus, (data.error && data.error.message) || "Positions failed", "error");
        lastPositions = [];
        updateOverlay();
        return;
      }
      const rows = data.positions || [];
      lastPositions = rows;
      if (!rows.length) {
        positionsBody.innerHTML = `<tr><td colspan="9">No open positions</td></tr>`;
      } else {
        positionsBody.innerHTML = rows
          .map((p) => {
            const side = String(p.side || "").toLowerCase();
            const sideClass =
              side === "long" || side === "buy" ? "side-long" : side === "short" || side === "sell" ? "side-short" : "";
            const d = p.display || {};
            const sym = dash(p.symbol);
            const esc = (s) => String(s ?? "").replace(/"/g, "&quot;");
            const native = esc(p.native_symbol || p.exchange_instrument || "");
            return `<tr>
              <td><button type="button" class="link-sym" data-symbol="${esc(p.symbol)}" data-native="${native}">${sym}</button></td>
              <td class="${sideClass}">${dash(p.side)}</td>
              <td class="num">${dash(d.size || p.size)}</td>
              <td class="num">${dash(d.entry || p.entry)}</td>
              <td class="num">${dash(d.mark || p.mark)}</td>
              <td class="num">${dash(d.pnl || p.pnl)}</td>
              <td class="num">${dash(d.sl || p.sl)}</td>
              <td class="num">${dash(d.tp || p.tp)}</td>
              <td class="actions">
                <button type="button" class="btn-ghost" data-act="tp" data-symbol="${esc(p.symbol)}" data-native="${native}" data-side="${esc(p.side)}" data-tp="${esc(d.tp || p.tp || "—")}">TP</button>
                <button type="button" class="btn-ghost" data-act="sl" data-symbol="${esc(p.symbol)}" data-native="${native}" data-side="${esc(p.side)}" data-sl="${esc(d.sl || p.sl || "—")}">SL</button>
                <button type="button" class="btn-danger" data-act="close" data-symbol="${esc(p.symbol)}" data-native="${native}" data-side="${esc(p.side)}" data-size="${esc(d.size || p.size)}" data-mark="${esc(d.mark || p.mark)}">Close</button>
              </td>
            </tr>`;
          })
          .join("");
        wirePositionActions();
        wireSymbolClicks(positionsBody);
      }
      positionsUpdated.textContent = `Last updated: ${nowStamp()}`;
      setLine(
        positionsStatus,
        `Positions OK · ${rows.length} · browser ${browserMs} ms · desk ${deskMs != null ? deskMs : "—"} ms${cacheHit}`,
        "ok"
      );
      updateOverlay();
    } catch (e) {
      if (e.name === "AbortError") return;
      if (String(e.message) !== "unauthorized") setLine(positionsStatus, String(e.message || e), "error");
    }
  }

  async function loadOrders(force) {
    const reqId = ++ordersReq;
    const key = accountKey();
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    if (!force) setLine(ordersStatus, "Loading orders…");
    try {
      const signal = abort("orders");
      const t0 = performance.now();
      const { data } = await api(
        `/api/orders?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}`,
        signal
      );
      if (reqId !== ordersReq || key !== accountKey()) return;
      const browserMs = Math.round(performance.now() - t0);
      const deskMs = data.timing_ms && data.timing_ms.tradedesk_ms;
      const cacheHit = data.cache_hit ? " cache" : "";
      if (!data.success) {
        ordersBody.innerHTML = `<tr><td colspan="7">${dash(data.error && data.error.message)}</td></tr>`;
        setLine(ordersStatus, (data.error && data.error.message) || "Orders failed", "error");
        return;
      }
      const groups = data.groups || [];
      lastOrderGroups = groups;
      if (!groups.length) {
        ordersBody.innerHTML = `<tr><td colspan="7">No open orders</td></tr>`;
      } else {
        ordersBody.innerHTML = groups
          .map((g) => {
            const side = String(g.side || "").toLowerCase();
            const classification = String(g.classification || "entry_limit");
            const isProt = classification === "take_profit" || classification === "stop_loss";
            const sideClass =
              classification === "take_profit"
                ? "side-tp"
                : classification === "stop_loss"
                ? "side-sl"
                : side === "buy"
                ? "side-buy"
                : side === "sell"
                ? "side-sell"
                : "";
            const d = g.display || {};
            const typeLabel = g.display_type || g.type || `${String(side).toUpperCase()} LIMIT`;
            const symLabel = `${dash(g.symbol)} (${dash(g.count)})`;
            let range = d.range || "—";
            if (g.trigger_price && g.limit_price && String(g.trigger_price) !== String(g.limit_price)) {
              range = `trig ${d.trigger_price || g.trigger_price} / lim ${d.limit_price || g.limit_price}`;
            } else if (g.trigger_price && isProt) {
              range = dash(d.trigger_price || g.trigger_price);
            }
            const esc = (s) => String(s ?? "").replace(/"/g, "&quot;");
            const ids = Array.isArray(g.order_ids) ? g.order_ids.join(",") : "";
            const native = esc(g.native_symbol || g.exchange_instrument || "");
            const cancelLabel = isProt
              ? classification === "take_profit"
                ? "Cancel TP"
                : "Cancel SL"
              : "Cancel";
            return `<tr data-class="${esc(classification)}">
              <td class="${sideClass}"><button type="button" class="link-sym" data-symbol="${esc(g.symbol)}" data-native="${native}">${symLabel}</button></td>
              <td class="${sideClass}">${esc(typeLabel)}${g.reduce_only ? " · RO" : ""}</td>
              <td class="num">${dash(g.count)}</td>
              <td class="num">${dash(d.total_remaining_size || g.total_remaining_size)}</td>
              <td class="num">${range}</td>
              <td class="num">${dash(d.vwap || g.vwap)}</td>
              <td class="actions">
                <button type="button" class="${isProt ? "btn-danger" : "btn-danger"}" data-cancel-group="1"
                  data-symbol="${esc(g.symbol)}" data-native="${native}" data-side="${esc(side)}" data-count="${esc(g.count)}"
                  data-vol="${esc(d.total_remaining_size || g.total_remaining_size)}"
                  data-range="${esc(range)}" data-vwap="${esc(d.vwap || g.vwap)}"
                  data-classification="${esc(classification)}" data-display-type="${esc(typeLabel)}"
                  data-order-ids="${esc(ids)}" data-reduce-only="${g.reduce_only ? "1" : "0"}"
                  data-trigger="${esc(g.trigger_price || "")}" data-limit="${esc(g.limit_price || g.min_price || "")}"
                  >${cancelLabel}</button>
              </td>
            </tr>`;
          })
          .join("");
        wireOrderActions();
        wireSymbolClicks(ordersBody);
      }
      ordersLoadedOnce = true;
      ordersUpdated.textContent = `Last updated: ${nowStamp()}`;
      setLine(
        ordersStatus,
        `Orders OK · ${groups.length} groups · open ${dash(data.open_order_count)} · browser ${browserMs} ms · desk ${deskMs != null ? deskMs : "—"} ms${cacheHit}`,
        "ok"
      );
      updateOverlay();
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
    quoteTimer = setInterval(loadQuote, 30000);
    positionsTimer = setInterval(() => loadPositions(false), POS_POLL_MS);
    ordersTimer = setInterval(() => {
      if (activeTab === "orders" || ordersLoadedOnce) loadOrders(false);
    }, ORD_POLL_MS);
  }

  function invalidateTradePreview() {
    activePreview = null;
    clearPreviewLines();
    const box = $("previewBox");
    if (box) box.hidden = true;
    const pe = $("previewError");
    if (pe) { pe.hidden = true; pe.textContent = ""; }
    const place = $("placeBtn");
    if (place) {
      place.disabled = false;
      place.textContent = "Place";
      place.onclick = null;
    }
    // Restore mode editor after leaving preview.
    applyModeVisibility();
    updateTradeCtx();
    updateSingleNotional();
  }

  function applyModeVisibility() {
    const single = $("singleForm");
    const ladder = $("ladderForm");
    const preview = $("previewBox");
    const inPreview = !!(preview && !preview.hidden && activePreview);
    if (single) {
      single.hidden = tradeMode !== "single" || inPreview;
    }
    if (ladder) {
      ladder.hidden = tradeMode !== "ladder" || inPreview;
    }
    const ms = $("modeSingle");
    const ml = $("modeLadder");
    if (ms) {
      ms.classList.toggle("active", tradeMode === "single");
      ms.setAttribute("aria-pressed", tradeMode === "single" ? "true" : "false");
      ms.textContent = tradeMode === "single" ? "SINGLE" : "Single";
    }
    if (ml) {
      ml.classList.toggle("active", tradeMode === "ladder");
      ml.setAttribute("aria-pressed", tradeMode === "ladder" ? "true" : "false");
      ml.textContent = tradeMode === "ladder" ? "LADDER" : "Ladder";
    }
  }

  function updateTradeCtx() {
    const el = $("tradeCtx");
    if (!el) return;
    const native = nativeInstrument || "—";
    el.textContent = `${(symbolEl.value || "").trim() || "—"} · ${exchangeEl.value || "—"} · ${accountEl.value || "—"} → ${native}`;
  }

  function updateSingleNotional() {
    const el = $("singleNotional");
    if (!el) return;
    if (tradeMode !== "single") {
      el.hidden = true;
      return;
    }
    const px = Number(String($("orderPrice").value || "").replace(/,/g, ""));
    const sz = Number(String($("orderSize").value || "").replace(/,/g, ""));
    if (!Number.isFinite(px) || !Number.isFinite(sz) || px <= 0 || sz <= 0) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    const n = px * sz;
    el.hidden = false;
    el.textContent = `≈ ${n.toLocaleString(undefined, { maximumFractionDigits: 2 })} (est. notional)`;
  }

  function setTradeSide(side) {
    tradeSide = side;
    $("sideBuy").classList.toggle("active", side === "buy");
    $("sideBuy").classList.toggle("buy", true);
    $("sideBuy").setAttribute("aria-pressed", side === "buy" ? "true" : "false");
    $("sideSell").classList.toggle("active", side === "sell");
    $("sideSell").classList.toggle("sell", true);
    $("sideSell").setAttribute("aria-pressed", side === "sell" ? "true" : "false");
    invalidateTradePreview();
  }

  function setTradeMode(mode) {
    if (mode !== "single" && mode !== "ladder") return;
    const prev = tradeMode;
    tradeMode = mode;
    // Mode switch always kills any active preview (and chart PREVIEW lines).
    if (prev !== mode || activePreview) {
      activePreview = null;
      clearPreviewLines();
      const box = $("previewBox");
      if (box) box.hidden = true;
      const pe = $("previewError");
      if (pe) { pe.hidden = true; pe.textContent = ""; }
    }
    applyModeVisibility();
    updateTradeCtx();
    updateSingleNotional();
    setLine($("tradeStatus"), mode === "ladder" ? "Ladder mode" : "Single order mode");
  }

  function showPreview(data) {
    // Guard: never show a preview for the inactive mode.
    if (data.kind === "ladder" && tradeMode !== "ladder") {
      invalidateTradePreview();
      return;
    }
    if (data.kind === "order" && tradeMode !== "single") {
      invalidateTradePreview();
      return;
    }
    activePreview = { id: data.preview_id, kind: data.kind, data };
    $("previewBox").hidden = false;
    $("previewError").hidden = true;
    $("previewError").textContent = "";
    applyModeVisibility(); // hide the editor while preview is open
    const place = $("placeBtn");
    place.disabled = false;
    place.onclick = null;
    if (data.kind === "ladder") {
      place.textContent = `Place ${data.order_count || (data.children || []).length} Orders`;
      const kids = data.children || [];
      let notional = 0;
      for (const c of kids) {
        const p = Number(String(c.price || "").replace(/,/g, ""));
        const s = Number(String(c.size || "").replace(/,/g, ""));
        if (Number.isFinite(p) && Number.isFinite(s)) notional += p * s;
      }
      const lines = [
        `${String(data.side || "").toUpperCase()} ${data.native_symbol} LADDER`,
        `Distribution: ${data.distribution === "half_gaussian" ? "Half-Gaussian" : "Uniform"}`,
        `Orders: ${data.order_count}`,
        `Total Size: ${data.total_size}`,
        `Price Range: ${data.min_price} – ${data.max_price}`,
        `VWAP: ${data.vwap}`,
        notional > 0 ? `Estimated ladder notional: ≈ ${notional.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "",
        lastPrice.textContent && lastPrice.textContent !== "—" ? `Current Price: ${lastPrice.textContent}` : "",
      ].filter(Boolean);
      $("previewText").textContent = lines.join("\n");
      $("childTableWrap").hidden = !kids.length;
      $("childBody").innerHTML = kids
        .map((c, i) => `<tr><td>${i + 1}</td><td class="num">${c.price}</td><td class="num">${c.size}</td></tr>`)
        .join("");
      drawLadderPreview(data);
    } else {
      place.textContent = "Place Order";
      const rp = data.requested_price;
      const fp = data.final_price;
      const rs = data.requested_size;
      const fs = data.final_size;
      const lines = [
        `${String(data.side || "").toUpperCase()} ${data.native_symbol} LIMIT`,
        rp !== fp ? `Requested Price: ${rp}\nFinal Price:     ${fp}` : `Price: ${fp}`,
        rs !== fs ? `Requested Size:  ${rs}\nFinal Size:      ${fs}` : `Size: ${fs}`,
        `Estimated Notional: ${data.notional}`,
      ];
      $("previewText").textContent = lines.join("\n");
      $("childTableWrap").hidden = true;
      clearPreviewLines();
    }
    updateOverlay();
  }

  async function doPreviewOrder() {
    if (tradeMode !== "single") {
      setLine($("tradeStatus"), "Switch to Single mode to preview an order.", "error");
      return;
    }
    const price = ($("orderPrice").value || "").trim();
    const size = ($("orderSize").value || "").trim();
    if (!price || !size) {
      setLine($("tradeStatus"), "Enter Limit Price and Size.", "error");
      return;
    }
    setLine($("tradeStatus"), "Building order preview…");
    try {
      const { data } = await apiPost("/api/trade/preview_order", {
        exchange: exchangeEl.value,
        account: accountEl.value,
        symbol: activeTradeSymbol(),
        side: tradeSide,
        order_type: "limit",
        size,
        price,
      });
      if (!data.success) throw new Error((data.error && data.error.message) || "Preview failed");
      showPreview(data);
      setLine($("tradeStatus"), data.summary || "Preview ready", "ok");
    } catch (e) {
      invalidateTradePreview();
      setLine($("tradeStatus"), String(e.message || e), "error");
    }
  }

  async function doPreviewLadder() {
    if (tradeMode !== "ladder") {
      setLine($("tradeStatus"), "Switch to Ladder mode to preview a ladder.", "error");
      return;
    }
    const start = ($("ladderStart").value || "").trim();
    const end = ($("ladderEnd").value || "").trim();
    const total = ($("ladderTotal").value || "").trim();
    const count = ($("ladderCount").value || "").trim();
    if (!start || !end || !total || !count) {
      setLine($("tradeStatus"), "Enter Start, End, Orders, and Total Size.", "error");
      return;
    }
    setLine($("tradeStatus"), "Building ladder preview…");
    try {
      const { data } = await apiPost("/api/trade/preview_ladder", {
        exchange: exchangeEl.value,
        account: accountEl.value,
        symbol: activeTradeSymbol(),
        side: tradeSide,
        distribution: $("ladderDist").value,
        order_count: count,
        total_size: total,
        start_price: start,
        end_price: end,
      });
      if (!data.success) throw new Error((data.error && data.error.message) || "Preview failed");
      showPreview(data);
      setLine($("tradeStatus"), data.summary || "Ladder preview ready", "ok");
    } catch (e) {
      invalidateTradePreview();
      setLine($("tradeStatus"), String(e.message || e), "error");
    }
  }

  async function doPlace() {
    if (!activePreview || !activePreview.id) return;
    const place = $("placeBtn");
    if (place.disabled) return;
    place.disabled = true;
    place.textContent = "Placing…";
    $("previewError").hidden = true;
    const snap = accountKey();
    const previewId = activePreview.id;
    try {
      const { data } = await apiPost("/api/trade/execute", { preview_id: previewId });
      if (snap !== accountKey()) {
        showToast("Selection changed; ignored stale execute result.", "error");
        return;
      }
      const accepted = Number(data.accepted || 0);
      const requested = Number(data.requested || 0);
      const partial = !!(data.partial || (requested && accepted > 0 && accepted < requested));
      // Partial ladder: accepted > 0 must refresh Orders even if overall success=false.
      // Never auto-retry — preview_id is one-shot.
      if (data.success || (partial && accepted > 0)) {
        const msg =
          data.message ||
          (partial
            ? `Ladder partially placed: ${accepted} / ${requested} accepted`
            : "Submitted");
        showToast(msg, partial ? "error" : "ok");
        setLine($("tradeStatus"), msg, partial ? "error" : "ok");
        invalidateTradePreview();
        await loadOrders(true);
        await loadPositions(true);
        loadAccountFinancials(true);
        return;
      }
      throw new Error((data.error && data.error.message) || data.message || "Execution failed");
    } catch (e) {
      $("previewError").hidden = false;
      $("previewError").textContent = String(e.message || e);
      // Do not re-enable place with same preview_id — it may be consumed.
      place.textContent = "Preview again";
      place.disabled = false;
      place.onclick = () => {
        invalidateTradePreview();
        if (tradeMode === "ladder") doPreviewLadder();
        else doPreviewOrder();
      };
      showToast(String(e.message || e), "error");
    }
  }

  function onSelectionChanged() {
    ordersLoadedOnce = false;
    lastPositions = [];
    lastOrderGroups = [];
    clearPositionLines();
    invalidateTradePreview();
    // Drop previous instrument quote/chart immediately (BTC must not stick on ZEC).
    resolvedOk = false;
    nativeInstrument = null;
    formatMeta = null;
    quoteFresh = false;
    lastPrice.textContent = "—";
    try { candleSeries && candleSeries.setData([]); } catch (_) {}
    candleCount.textContent = "0";
    updateTradeCtx();
    updateTradeEnablement("Loading market data…");
    loadCandles();
    loadPositions();
    if (activeTab === "orders") loadOrders();
    loadAccountFinancials(true);
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
  $("refreshPanelBtn").addEventListener("click", () => {
    if (activeTab === "orders") loadOrders(true);
    else loadPositions(true);
  });
  tabPositions.addEventListener("click", () => setTab("positions"));
  tabOrders.addEventListener("click", () => setTab("orders"));

  // Trade panel
  if ($("sideBuy")) {
    $("sideBuy").addEventListener("click", () => setTradeSide("buy"));
    $("sideSell").addEventListener("click", () => setTradeSide("sell"));
    $("modeSingle").addEventListener("click", () => setTradeMode("single"));
    $("modeLadder").addEventListener("click", () => setTradeMode("ladder"));
    $("previewOrderBtn").addEventListener("click", doPreviewOrder);
    $("previewLadderBtn").addEventListener("click", doPreviewLadder);
    $("previewBackBtn").addEventListener("click", invalidateTradePreview);
    $("placeBtn").addEventListener("click", doPlace);
    $("useCurrentPrice").addEventListener("click", () => {
      if (!quoteFresh || !lastPrice.textContent || lastPrice.textContent === "—") {
        showToast("No fresh market price for the active instrument.", "error");
        return;
      }
      $("orderPrice").value = String(lastPrice.textContent).replace(/,/g, "");
      updateSingleNotional();
    });
    $("ladderStartCurrent").addEventListener("click", () => {
      if (!quoteFresh || !lastPrice.textContent || lastPrice.textContent === "—") {
        showToast("No fresh market price for the active instrument.", "error");
        return;
      }
      $("ladderStart").value = String(lastPrice.textContent).replace(/,/g, "");
    });
    ["orderPrice", "orderSize", "ladderStart", "ladderEnd", "ladderCount", "ladderTotal", "ladderDist"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("input", () => {
        if (id === "orderPrice" || id === "orderSize") updateSingleNotional();
        if (activePreview) invalidateTradePreview();
      });
      if (el) el.addEventListener("change", () => {
        if (id === "orderPrice" || id === "orderSize") updateSingleNotional();
        if (activePreview) invalidateTradePreview();
      });
    });
    if ($("showOrdersOverlay")) {
      $("showOrdersOverlay").addEventListener("change", updateOverlay);
    }
    setTradeSide("buy");
    setTradeMode("single");
  }

  initChart();
  ensureCsrf()
    .then(() => loadExchanges())
    .then(() => {
      onSelectionChanged();
      schedulePoll();
    })
    .catch((e) => setLine(chartStatus, String(e.message || e), "error"));
})();
