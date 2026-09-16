(() => {
  const $ = (id) => document.getElementById(id);
  const exchangeEl = $("exchange");
  const accountEl = $("account");
  const symbolEl = $("symbol");
  const tfEl = $("tf");
  const statusLine = $("statusLine");
  const nativeSym = $("nativeSym");
  const lastPrice = $("lastPrice");
  const candleCount = $("candleCount");
  const positionsBody = $("positionsBody");
  const suggestions = $("symbolSuggestions");

  let candleSeries = null;
  let chart = null;
  let pollTimer = null;
  let nativeInstrument = null;
  let requestSeq = 0;

  function setStatus(msg, isError) {
    statusLine.textContent = msg || "";
    statusLine.className = "status-line" + (isError ? " error" : "");
  }

  async function api(path) {
    const res = await fetch(path, { credentials: "same-origin" });
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
      layout: {
        background: { color: "#0a0d12" },
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

  function dash(v) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v);
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
      setStatus("No exchanges discovered.", true);
      return;
    }
    // Prefer hyperliquid or first
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
      setStatus(`No accounts for ${ex}.`, true);
      return;
    }
    if (selectFirst || !accounts.some((a) => a.account === prev)) {
      accountEl.selectedIndex = 0;
    } else {
      accountEl.value = prev;
    }
  }

  async function resolveSymbol() {
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    const sym = symbolEl.value.trim() || "BTCUSD";
    const { data } = await api(
      `/api/instruments/resolve?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(sym)}`
    );
    suggestions.innerHTML = "";
    if (data.candidates && Array.isArray(data.candidates)) {
      for (const c of data.candidates) {
        const opt = document.createElement("option");
        const value = c.symbol || c.display_name || "";
        opt.value = value;
        suggestions.appendChild(opt);
      }
    }
    if (data.success && data.instrument) {
      nativeInstrument = data.instrument.symbol;
      nativeSym.textContent = nativeInstrument;
      return nativeInstrument;
    }
    nativeInstrument = sym;
    nativeSym.textContent = `${sym} (unresolved)`;
    if (data.error) setStatus(data.error.message || "Instrument resolve failed", true);
    return nativeInstrument;
  }

  async function loadCandles() {
    const seq = ++requestSeq;
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    const sym = symbolEl.value.trim() || "BTCUSD";
    const tf = tfEl.value;
    setStatus(`Loading ${ex} ${sym} ${tf}…`);
    const native = await resolveSymbol();
    if (seq !== requestSeq) return;
    const { res, data } = await api(
      `/api/candles?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(native || sym)}&tf=${encodeURIComponent(tf)}`
    );
    if (seq !== requestSeq) return;
    if (!data.success) {
      candleSeries.setData([]);
      candleCount.textContent = "0";
      setStatus((data.error && data.error.message) || "Candles unavailable", true);
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
    nativeSym.textContent = data.native_symbol || native || sym;
    setStatus(`OK · ${ex} / ${acct} · ${data.native_symbol || native} · ${tf}`);
  }

  async function loadQuote() {
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    const sym = nativeInstrument || symbolEl.value.trim() || "BTCUSD";
    try {
      const { data } = await api(
        `/api/quote?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}&symbol=${encodeURIComponent(sym)}`
      );
      if (data.success && data.price != null) {
        lastPrice.textContent = String(data.price);
      }
    } catch (_) {
      /* ignore poll errors */
    }
  }

  async function loadPositions() {
    const ex = exchangeEl.value;
    const acct = accountEl.value;
    const { data } = await api(
      `/api/positions?exchange=${encodeURIComponent(ex)}&account=${encodeURIComponent(acct)}`
    );
    const rows = data.positions || [];
    if (!rows.length) {
      positionsBody.innerHTML = `<tr><td colspan="8">${data.success === false ? dash(data.error && data.error.message) : "No open positions"}</td></tr>`;
      return;
    }
    positionsBody.innerHTML = rows
      .map((p) => {
        const side = String(p.side || "").toLowerCase();
        const sideClass = side === "long" || side === "buy" ? "side-long" : side === "short" || side === "sell" ? "side-short" : "";
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

  async function refreshAll() {
    try {
      await loadCandles();
      await loadPositions();
      await loadQuote();
    } catch (e) {
      if (String(e.message) !== "unauthorized") setStatus(String(e.message || e), true);
    }
  }

  function schedulePoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      loadQuote();
      loadPositions();
    }, 15000);
  }

  exchangeEl.addEventListener("change", async () => {
    await loadAccounts(true);
    await refreshAll();
  });
  accountEl.addEventListener("change", refreshAll);
  tfEl.addEventListener("change", loadCandles);
  symbolEl.addEventListener("change", refreshAll);
  $("reloadBtn").addEventListener("click", refreshAll);

  initChart();
  loadExchanges()
    .then(refreshAll)
    .then(schedulePoll)
    .catch((e) => setStatus(String(e.message || e), true));
})();
