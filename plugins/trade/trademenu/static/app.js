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
  let positionLines = [];
  let csrfToken = "";
  let modalState = null;

  let chartReq = 0;
  let positionsReq = 0;
  let ordersReq = 0;
  let resolveReq = 0;
  const controllers = { chart: null, positions: null, orders: null, resolve: null };
  const POS_POLL_MS = 15 * 60 * 1000;
  const ORD_POLL_MS = 15 * 60 * 1000;

  function selectionKey() {
    return `${exchangeEl.value}|${accountEl.value}|${(symbolEl.value || "").trim()}|${tfEl.value}`;
  }
  function accountKey() {
    return `${exchangeEl.value}|${accountEl.value}`;
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

  function symbolsMatch(posSym, native, requested) {
    const a = String(posSym || "").trim().toUpperCase();
    const b = String(native || "").trim().toUpperCase();
    const c = String(requested || "").trim().toUpperCase();
    if (!a) return false;
    if (b && (a === b || a.endsWith(":" + b) || b.endsWith(":" + a))) return true;
    const peel = (s) => s.replace(/[-_/]/g, "").replace(/(USDT|USDC|USD)$/i, "");
    const pa = peel(a);
    if (b && pa === peel(b)) return true;
    if (c && pa === peel(c)) return true;
    return false;
  }

  function updateOverlay() {
    if (!candleSeries) return;
    clearPositionLines();
    if (!resolvedOk || !nativeInstrument) return;
    const req = (symbolEl.value || "").trim();
    const match = lastPositions.find((p) => symbolsMatch(p.symbol, nativeInstrument, req));
    if (!match) return;
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
    try {
      // Candles endpoint resolves server-side (cached). Skip a separate resolve
      // round-trip unless we need native for overlay matching sooner.
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
        candleSeries.setData([]);
        candleCount.textContent = "0";
        resolvedOk = false;
        nativeInstrument = null;
        clearPositionLines();
        nativeSym.textContent = data.display || `${sym} → unresolved`;
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
      formatMeta = data.format_meta || formatMeta;
      if (candles.length) {
        chart.timeScale().fitContent();
        lastPrice.textContent = fmtPriceClient(candles[candles.length - 1].close, formatMeta);
      }
      candleCount.textContent = String(candles.length);
      if (data.display) nativeSym.textContent = data.display;
      nativeInstrument = data.native_symbol || nativeInstrument;
      resolvedOk = true;
      updateOverlay();
      const rMs = data.resolve_timing_ms && data.resolve_timing_ms.tradedesk_ms;
      const rCache = data.resolve_cache_hit ? " resolve-cache" : "";
      setLine(
        chartStatus,
        `Chart OK · ${data.native_symbol || "—"} · ${tf} · ${candles.length} bars · browser ${browserMs} ms · resolve ${rMs != null ? rMs : "—"} ms${rCache}`,
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
      if (data.success && data.price != null) lastPrice.textContent = fmtPriceClient(data.price, formatMeta);
    } catch (_) {}
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
              showToast(`TP updated to ${price}`, "ok");
              await loadPositions(true);
              if (activeTab === "orders") await loadOrders(true);
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
              showToast(`SL updated to ${price}`, "ok");
              await loadPositions(true);
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
              return data;
            },
          });
        }
      });
    });
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
        const snap = accountKey();
        openModal({
          title: `Cancel ${count} ${sym} ${String(side).toUpperCase()} LIMIT orders?`,
          body: `Total volume: ${vol}\nPrice range: ${range}\nVWAP: ${vwap}`,
          danger: true,
          cancelLabel: "Keep Orders",
          confirmLabel: `Cancel ${count} Orders`,
          onConfirm: async () => {
            modalConfirm.disabled = true;
            modalConfirm.textContent = "Cancelling…";
            const data = await runWrite("/api/orders/cancel_group", {
              exchange: exchangeEl.value,
              account: accountEl.value,
              symbol: sym,
              side,
              type: "limit",
            }, snap);
            if (!data) return { aborted: true };
            if (!data.success) throw new Error((data.error && data.error.message) || "Cancel failed");
            const msg = data.message || `Cancelled ${data.cancelled || count} orders.`;
            showToast(msg, data.partial ? "error" : "ok");
            await loadOrders(true);
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
            return `<tr>
              <td>${sym}</td>
              <td class="${sideClass}">${dash(p.side)}</td>
              <td class="num">${dash(d.size || p.size)}</td>
              <td class="num">${dash(d.entry || p.entry)}</td>
              <td class="num">${dash(d.mark || p.mark)}</td>
              <td class="num">${dash(d.pnl || p.pnl)}</td>
              <td class="num">${dash(d.sl || p.sl)}</td>
              <td class="num">${dash(d.tp || p.tp)}</td>
              <td class="actions">
                <button type="button" class="btn-ghost" data-act="tp" data-symbol="${esc(p.symbol)}" data-side="${esc(p.side)}" data-tp="${esc(d.tp || p.tp || "—")}">TP</button>
                <button type="button" class="btn-ghost" data-act="sl" data-symbol="${esc(p.symbol)}" data-side="${esc(p.side)}" data-sl="${esc(d.sl || p.sl || "—")}">SL</button>
                <button type="button" class="btn-danger" data-act="close" data-symbol="${esc(p.symbol)}" data-side="${esc(p.side)}" data-size="${esc(d.size || p.size)}" data-mark="${esc(d.mark || p.mark)}">Close</button>
              </td>
            </tr>`;
          })
          .join("");
        wirePositionActions();
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
      if (!groups.length) {
        ordersBody.innerHTML = `<tr><td colspan="7">No open orders</td></tr>`;
      } else {
        ordersBody.innerHTML = groups
          .map((g) => {
            const side = String(g.side || "").toLowerCase();
            const sideClass = side === "buy" ? "side-buy" : side === "sell" ? "side-sell" : "";
            const sideType = `${String(g.side || "").toUpperCase()} ${String(g.type || "limit").toUpperCase()}`.trim();
            const d = g.display || {};
            const symLabel = `${dash(g.symbol)} (${dash(g.count)})`;
            const range = d.range || "—";
            const esc = (s) => String(s ?? "").replace(/"/g, "&quot;");
            return `<tr>
              <td class="${sideClass}">${symLabel}</td>
              <td class="${sideClass}">${sideType}</td>
              <td class="num">${dash(g.count)}</td>
              <td class="num">${dash(d.total_remaining_size || g.total_remaining_size)}</td>
              <td class="num">${range}</td>
              <td class="num">${dash(d.vwap || g.vwap)}</td>
              <td class="actions">
                <button type="button" class="btn-danger" data-cancel-group="1"
                  data-symbol="${esc(g.symbol)}" data-side="${esc(side)}" data-count="${esc(g.count)}"
                  data-vol="${esc(d.total_remaining_size || g.total_remaining_size)}"
                  data-range="${esc(range)}" data-vwap="${esc(d.vwap || g.vwap)}">Cancel</button>
              </td>
            </tr>`;
          })
          .join("");
        wireOrderActions();
      }
      ordersLoadedOnce = true;
      ordersUpdated.textContent = `Last updated: ${nowStamp()}`;
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
    quoteTimer = setInterval(loadQuote, 30000);
    positionsTimer = setInterval(() => loadPositions(false), POS_POLL_MS);
    ordersTimer = setInterval(() => {
      if (activeTab === "orders" || ordersLoadedOnce) loadOrders(false);
    }, ORD_POLL_MS);
  }

  function onSelectionChanged() {
    ordersLoadedOnce = false;
    lastPositions = [];
    clearPositionLines();
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
  $("refreshPanelBtn").addEventListener("click", () => {
    if (activeTab === "orders") loadOrders(true);
    else loadPositions(true);
  });
  tabPositions.addEventListener("click", () => setTab("positions"));
  tabOrders.addEventListener("click", () => setTab("orders"));

  initChart();
  ensureCsrf()
    .then(() => loadExchanges())
    .then(() => {
      onSelectionChanged();
      schedulePoll();
    })
    .catch((e) => setLine(chartStatus, String(e.message || e), "error"));
})();
