(() => {
  const state = {
    csrf: null,
    exchanges: [],
    exchange: null,
    account: null,
    marketType: 'futures',
    market: null,
    markets: [],
    accountState: null,
    tradeTab: 'order',
    bottomTab: 'positions',
    mobile: 'markets',
    ladderSide: 'buy',
    orderSide: 'buy',
    chart: null,
    candleSeries: null,
    volumeSeries: null,
    chartReady: false,
    chartPollTimer: null,
    // Monotonic counter incremented on every new chart-load trigger.
    // loadChartHistory() captures the current value at start and drops
    // any response whose captured gen != state.chartLoadGen.
    chartLoadGen: 0,
    selectedTimeframe: '15m',
    defaultTimeframe: '15m',
    priceLines: [],
  };
  const TIMEFRAMES = ['15m', '1h', '4h', '1D'];
  const POLL_INTERVAL_MS = 10000;
  const HISTORY_LIMIT = 240;

  const $ = (sel) => document.querySelector(sel);
  const fmt = (v) => (v === null || v === undefined || v === '' ? '—' : String(v));
  const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : null; };
  const BUY_COLOR = '#3b82f6';
  const SELL_COLOR = '#ef5b67';
  const POS_COLOR = '#16c784';
  const NEG_COLOR = '#ef5b67';

  function formatMoney(v) {
    const n = num(v);
    if (n === null) return '—';
    return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
  }
  function formatSignedMoney(v) {
    const n = num(v);
    if (n === null) return '—';
    const sign = n > 0 ? '+' : '';
    return `${sign}$${n.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
  }
  function formatDynamicPrice(raw) {
    if (raw === null || raw === undefined || raw === '') return '—';
    const n = num(raw);
    if (n === null) return '—';
    const abs = Math.abs(n);
    let digits;
    if (abs >= 1000) digits = 2;
    else if (abs >= 1) digits = 2;
    else digits = 5;
    let out = n.toFixed(digits);
    if (out.includes('.')) {
      const [head, tail] = out.split('.');
      const intPart = Number(head).toLocaleString();
      out = `${intPart}.${tail}`;
      if (abs >= 1) {
        out = out.replace(/0+$/, '').replace(/\.$/, '');
        if (!out) out = '0';
      }
    } else {
      out = Number(out).toLocaleString();
    }
    return out;
  }
  function formatFunding(raw) {
    if (raw === null || raw === undefined || raw === '') return '—';
    const n = num(raw);
    if (n === null) return '—';
    const pct = n * 100;
    return `${pct.toFixed(4)}%`;
  }
  const pnlClass = (v) => { const n = num(v); if (n === null) return 'muted'; return n > 0 ? 'pnl-pos' : (n < 0 ? 'pnl-neg' : ''); };

  async function api(path, options = {}) {
    const headers = Object.assign({ 'Accept': 'application/json' }, options.headers || {});
    if (state.csrf) headers['X-CSRF-Token'] = state.csrf;
    const res = await fetch(path, Object.assign({ credentials: 'same-origin', headers }, options));
    if (!res.ok) throw new Error(`${path} ${res.status}`);
    return res.json();
  }

  function showLogin(show, msg = '') {
    const panel = $('#loginPanel');
    const error = $('#loginError');
    if (panel) panel.hidden = !show;
    if (error) error.textContent = msg;
  }

  function capabilityFor(exchange) {
    const row = state.exchanges.find(x => x.exchange === exchange);
    return row ? row.capabilities || {} : {};
  }
  function supportedMarketTypes(exchange) {
    const types = capabilityFor(exchange).market_types || ['futures'];
    return types.length ? types : ['futures'];
  }
  function preferredMarketType(types) {
    const saved = localStorage.getItem('webtrade2.marketType');
    if (saved && types.includes(saved)) return saved;
    if (types.includes('futures')) return 'futures';
    return types[0];
  }

  function setTradeTab(tab) {
    state.tradeTab = tab;
    document.querySelectorAll('[data-trade-tab]').forEach(btn => {
      const active = btn.dataset.tradeTab === tab;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    });
    const order = $('#orderPanel');
    const ladder = $('#ladderPanel');
    if (order) order.hidden = tab !== 'order';
    if (ladder) ladder.hidden = tab !== 'ladder';
  }
  function setMobileSection(section) {
    state.mobile = section;
    document.querySelectorAll('[data-mobile-target]').forEach(btn => btn.classList.toggle('active', btn.dataset.mobileTarget === section));
    document.querySelectorAll('[data-mobile-section]').forEach(el => el.classList.toggle('mobile-active', el.dataset.mobileSection === section));
    // Switching to the chart panel on mobile changes its effective
    // clientWidth/clientHeight (the panel was display:none moments ago).
    // Wait one frame for the layout to commit, then explicitly drive
    // chart.resize() so the canvas matches the now-visible container.
    requestAnimationFrame(() => {
      if (state.chart && state.chartReady) applyChartResize();
    });
    // And again after two RAFs to cover iOS Safari's deferred layout
    // pass when an element becomes visible (it batches layout). The
    // ResizeObserver will also catch any further change.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (state.chart && state.chartReady) applyChartResize();
    }));
  }

  function setTimeframe(tf) {
    if (!TIMEFRAMES.includes(tf)) return;
    state.selectedTimeframe = tf;
    document.querySelectorAll('[data-timeframe]').forEach(btn => btn.classList.toggle('active', btn.dataset.timeframe === tf));
    if (state.exchange && state.account && state.market?.symbol) {
      // Bump the load generation so any in-flight candle request from
      // the previous timeframe is invalidated and dropped on response.
      state.chartLoadGen = (state.chartLoadGen || 0) + 1;
      loadChartHistory().catch(() => {});
      startChartPolling();
    }
  }

  // ---------------------------- Chart ---------------------------------
  // Lightweight Charts v4.2 standalone + Safari interaction:
  //
  //   - On mobile Safari (and iOS WebKit generally) autoSize:true can
  //     size the canvases to a CSS measurement that was captured too
  //     early, before the chart panel finished laying out. The result
  //     is that LWC paints into the bitmap correctly but the canvas's
  //     CSS box ends up smaller than the chart container, so the user
  //     sees an empty area. The robust fix is: turn autoSize OFF and
  //     drive chart.resize() ourselves using the actual computed
  //     width/height of the container at the moment the chart is
  //     first shown.
  //   - We still let ResizeObserver resync on later layout changes
  //     (e.g. orientation / mobile-nav / scroll-driven resize) but we
  //     do it via chart.resize() with the explicit measurement, not
  //     letting LWC capture a transient 0×0 size.
  //
  // See plugins/trade/webtrade2/static/app.js for the resize logic.
  function initChart() {
    const container = $('#chart');
    if (!container) return false;
    if (!window.LightweightCharts || typeof window.LightweightCharts.createChart !== 'function') return false;
    // Read the container size synchronously. If the panel isn't laid
    // out yet (true on mobile when the page boots with the markets tab
    // active), fall back to a sensible default so createChart doesn't
    // start with a 0×0 internal size.
    let cssWidth = container.clientWidth;
    let cssHeight = container.clientHeight;
    if (!cssWidth || !cssHeight) {
      cssWidth = Math.max(cssWidth || 0, 320);
      cssHeight = Math.max(cssHeight || 0, 280);
    }
    const chart = window.LightweightCharts.createChart(container, {
      width: cssWidth,
      height: cssHeight,
      layout: { background: { color: '#0b0e14' }, textColor: '#e6ecf5', fontFamily: 'Inter, system-ui, sans-serif' },
      grid: { vertLines: { color: '#161d2f' }, horzLines: { color: '#161d2f' } },
      rightPriceScale: { borderColor: '#1c2438', textColor: '#8a9bb5' },
      timeScale: { borderColor: '#1c2438', timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
    });
    const candleSeries = chart.addCandlestickSeries({
      upColor: BUY_COLOR,
      downColor: SELL_COLOR,
      borderUpColor: BUY_COLOR,
      borderDownColor: SELL_COLOR,
      wickUpColor: BUY_COLOR,
      wickDownColor: SELL_COLOR,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    state.chart = chart;
    state.candleSeries = candleSeries;
    state.chartReady = true;

    // Install a ResizeObserver so that when the chart panel actually
    // becomes visible (mobile tab switch, orientation change, etc.) we
    // immediately drive chart.resize() to the current container size.
    // We use ResizeObserver because it is what real Safari supports;
    // we never fall back to autoSize:true because that's what was
    // causing the missing-canvas-render on iPhone.
    if (typeof ResizeObserver !== 'undefined' && container) {
      if (state._chartResizeObserver) {
        try { state._chartResizeObserver.disconnect(); } catch (_) {}
      }
      const ro = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const w = Math.round(entry.contentRect.width);
          const h = Math.round(entry.contentRect.height);
          if (w > 0 && h > 0 && state.chart) {
            state.chart.resize(w, h);
            state.chart.timeScale().fitContent();
          }
        }
      });
      ro.observe(container);
      state._chartResizeObserver = ro;
    }

    // Also schedule one explicit resize on the next animation frame so
    // a freshly-mounted chart (where the panel just became visible)
    // gets sized correctly even before ResizeObserver's first tick.
    requestAnimationFrame(() => {
      if (state.chart && container) {
        const w = Math.round(container.clientWidth || 0);
        const h = Math.round(container.clientHeight || 0);
        if (w > 0 && h > 0) {
          state.chart.resize(w, h);
          state.chart.timeScale().fitContent();
        }
      }
    });

    return true;
  }

  // Public resize API used by loadChartHistory() and by mobile tab
  // switches that change the chart panel's effective size. We
  // idempotently set the chart size to the current #chart client size
  // (rounded) and then run fitContent once.
  function applyChartResize() {
    if (!state.chart || !state.chartReady) return;
    const container = document.getElementById('chart');
    if (!container) return;
    const w = Math.round(container.clientWidth || 0);
    const h = Math.round(container.clientHeight || 0);
    if (w <= 0 || h <= 0) return;
    try { state.chart.resize(w, h); } catch (_) {}
    try { state.chart.timeScale().fitContent(); } catch (_) {}
  }

  function clearChartOverlays() {
    if (!state.candleSeries) return;
    for (const pl of state.priceLines) {
      try { state.candleSeries.removePriceLine(pl); } catch (_) {}
    }
    state.priceLines = [];
  }

  function applyChartPrecision(price) {
    if (!state.chart) return;
    const n = num(price);
    if (n === null) return;
    const abs = Math.abs(n);
    let precision = 2;
    if (abs < 0.01) precision = 5;
    else if (abs < 1) precision = 4;
    else if (abs < 100) precision = 3;
    state.candleSeries.applyOptions({ priceFormat: { type: 'price', precision, minMove: Math.pow(10, -precision) } });
  }

  // showChartMessage(text) — clear or display an error overlay in the
  // chart panel. Used when a candle load fails or returns zero rows so
  // the user never sees stale candles from a previous instrument. This is
  // production UX (not telemetry / debug).
  function showChartMessage(text) {
    let el = document.getElementById('chartMessage');
    if (!el) {
      el = document.createElement('div');
      el.id = 'chartMessage';
      el.style.cssText = 'margin-top:6px;padding:8px 10px;border:1px solid #ef5b67;color:#ef5b67;background:rgba(239,91,103,.08);border-radius:4px;font-size:12px;letter-spacing:.2px';
      const wrap = document.querySelector('.chart-wrap');
      if (wrap && wrap.parentNode) wrap.parentNode.insertBefore(el, wrap.nextSibling);
      else document.body.appendChild(el);
    }
    if (!text) {
      el.hidden = true;
      el.textContent = '';
    } else {
      el.hidden = false;
      el.textContent = text;
    }
  }

  async function loadChartHistory() {
    if (!state.chartReady || !state.market?.symbol || !state.exchange) return;

    // ----------------------------------------------------------------
    // GENERATION / REQUEST-ID
    // ----------------------------------------------------------------
    // Every new selection (symbol, timeframe, exchange, account, market
    // type) bumps state.chartLoadGen. The current call captures myGen at
    // start. After every await we re-check myGen === state.chartLoadGen
    // and silently drop the response if it doesn't match — that's the
    // ONLY way to prevent a slow Apex/HL candle response from
    // overwriting the chart the user already navigated away from.
    //
    // We also clear the previous series synchronously before the network
    // request so that a stale BTC/ETH chart never remains on screen
    // while the user waits for a different instrument's data.
    // ----------------------------------------------------------------
    state.chartLoadGen = (state.chartLoadGen || 0) + 1;
    const myGen = state.chartLoadGen;
    const myExchange = state.exchange;
    const myAccount = state.account;
    const myMarketType = state.marketType;
    const mySymbol = state.market.symbol;
    const myTimeframe = state.selectedTimeframe;

    function isStale() {
      return myGen !== state.chartLoadGen
        || myExchange !== state.exchange
        || myAccount !== state.account
        || myMarketType !== state.marketType
        || mySymbol !== (state.market?.symbol)
        || myTimeframe !== state.selectedTimeframe;
    }

    // Reset chart state immediately so a failed/pending request can't
    // leave stale candles from a previous instrument visible.
    try {
      if (state.candleSeries) state.candleSeries.setData([]);
      if (state.chart && state.chart.timeScale) state.chart.timeScale().setVisibleRange(null);
      if (state.chart && state.chart.priceScale) {
        try { state.chart.priceScale('right').applyOptions({ autoScale: true }); } catch (_) {}
      }
    } catch (_) { /* ignore */ }
    showChartMessage(null);

    // CRITICAL mobile order of operations (Safari explicit resize fix):
    //   1. Force the chart's CSS box to match the container NOW.
    //   2. Then push the new data.
    //   3. Then fitContent ONCE on both time and price scales.
    applyChartResize();

    const requestUrl = `/api/candles?${new URLSearchParams({
      exchange: myExchange,
      account: myAccount,
      symbol: mySymbol,
      interval: myTimeframe,
      limit: String(HISTORY_LIMIT),
      market_type: myMarketType,
    })}`;

    let data;
    let fetchError = null;
    try {
      const resp = await fetch(requestUrl, { credentials: 'same-origin', cache: 'no-store' });
      let body = null;
      try { body = await resp.json(); } catch (e) { body = null; }
      data = body;
    } catch (err) {
      fetchError = err;
    }

    // Drop stale responses BEFORE any state mutation. A stale response
    // arriving after the user switched markets must never paint.
    if (isStale()) {
      return;
    }

    if (fetchError || !data) {
      try { state.candleSeries && state.candleSeries.setData([]); } catch (_) {}
      showChartMessage(`Chart unavailable: ${fetchError ? 'network error' : 'no response'}`);
      return;
    }

    const apiSuccess = data && (data.success === true || data.success === undefined);
    if (!apiSuccess) {
      const errCode = (data && data.error && data.error.code) || 'api_error';
      try { state.candleSeries && state.candleSeries.setData([]); } catch (_) {}
      showChartMessage(`Chart unavailable: ${errCode}`);
      return;
    }

    const dataBlock = data.data || {};
    const candles = Array.isArray(dataBlock.candles) ? dataBlock.candles : [];

    if (candles.length === 0) {
      try { state.candleSeries && state.candleSeries.setData([]); } catch (_) {}
      showChartMessage('Chart unavailable: no candles returned');
      return;
    }

    // LWC v4.2 candle input contract:
    //   { time: UTC seconds (integer), open, high, low, close }
    //   times must be strictly ascending and unique.
    const seen = new Set();
    const series = [];
    for (const c of candles) {
      const t = Math.floor(Number(c.time));
      const o = Number(c.open);
      const h = Number(c.high);
      const l = Number(c.low);
      const cl = Number(c.close);
      if (
        !Number.isFinite(t) || t <= 0 ||
        !Number.isFinite(o) || !Number.isFinite(h) ||
        !Number.isFinite(l) || !Number.isFinite(cl)
      ) {
        continue;
      }
      if (seen.has(t)) continue;
      seen.add(t);
      series.push({ time: t, open: o, high: h, low: l, close: cl });
    }
    series.sort((a, b) => a.time - b.time);
    // Dedup pass: re-validate ascending+unique (server may have emitted
    // duplicates that slipped past the seen-set).
    let ascending = true;
    let dupCount = 0;
    for (let i = 1; i < series.length; i++) {
      if (series[i].time <= series[i-1].time) { ascending = false; dupCount++; }
    }

    if (series.length === 0) {
      try { state.candleSeries && state.candleSeries.setData([]); } catch (_) {}
      showChartMessage('Chart unavailable: no valid candle rows');
      return;
    }

    // One more staleness check right before applying — the user may
    // have clicked a different market while we were normalizing.
    if (isStale()) {
      return;
    }

    // Apply. Container already resize()'d above; layout is stable.
    try {
      const cont = document.getElementById('chart');
      // Final pre-setData wipe (defensive): this is the moment we commit
      // to the new symbol, so any leftover from the previous series is
      // gone.
      state.candleSeries.setData([]);
      state.candleSeries.setData(series);
      applyChartPrecision(series[series.length - 1].close);

      // RESET both scales explicitly after setData so a previous
      // instrument's autoscale state never carries over.
      try { state.chart.timeScale().fitContent(); } catch (_) {}
      try {
        const ps = state.chart.priceScale('right');
        ps.applyOptions({ autoScale: true });
        // Force the price scale to forget any manual margins/scale
        // overrides from the previous instrument.
        try { ps.setMargins({ top: 0.1, bottom: 0.1 }); } catch (_) {}
      } catch (_) {}
      try { state.chart.priceScale('left').applyOptions({ autoScale: true }); } catch (_) {}
      showChartMessage(null);

      // One rAF later (chart has repainted) re-fit time scale if we're
      // still the current load. A stale rAF is harmless because setData
      // is already committed — fitContent is just polish.
      requestAnimationFrame(() => {
        if (myGen !== state.chartLoadGen) return;
        try { state.chart && state.chart.timeScale().fitContent(); } catch (_) {}
      });
    } catch (err) {
      console.error("[chart] setData threw for", state.market.symbol, "tf=", state.selectedTimeframe, err);
    }
    await renderChartOverlays();
  }

  async function renderChartOverlays() {
    if (!state.candleSeries || !state.market?.symbol || !state.exchange) return;
    clearChartOverlays();
    try {
      const data = await api(`/api/positions_orders?${new URLSearchParams({ exchange: state.exchange, account: state.account })}`);
      const positions = (data && data.data && data.data.positions) || [];
      const orders = (data && data.data && data.data.order_groups) || [];
      const here = positions.filter(p => (p.symbol || p.instrument || p.market) === state.market.symbol);
      for (const p of here) {
        const entry = num(p.entry_price || p.entry);
        if (entry !== null) state.priceLines.push(state.candleSeries.createPriceLine({
          price: entry, color: BUY_COLOR, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: `Entry ${entry}`,
        }));
        const liq = num(p.liquidation_price);
        if (liq !== null) state.priceLines.push(state.candleSeries.createPriceLine({
          price: liq, color: NEG_COLOR, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `Liq ${liq}`,
        }));
        const tp = num(p.tp);
        if (tp !== null) state.priceLines.push(state.candleSeries.createPriceLine({
          price: tp, color: POS_COLOR, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: `TP ${tp}`,
        }));
        const sl = num(p.sl);
        if (sl !== null) state.priceLines.push(state.candleSeries.createPriceLine({
          price: sl, color: NEG_COLOR, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: `SL ${sl}`,
        }));
      }
      const hereOrders = orders.filter(o => (o.symbol || o.instrument || o.market) === state.market.symbol);
      for (const o of hereOrders) {
        const px = num(o.price || o.trigger_price);
        if (px !== null) state.priceLines.push(state.candleSeries.createPriceLine({
          price: px, color: '#8a9bb5', lineWidth: 1, lineStyle: 3, axisLabelVisible: false, title: `Order ${px}`,
        }));
      }
    } catch (_) { /* overlay data is best-effort */ }
  }

  // Incremental update: fetch the last 2 candles only, append or replace the last bucket
  async function updateLatestCandle() {
    if (!state.chartReady || !state.market?.symbol || !state.exchange) return;
    // Capture generation/identity at request start so a poll that races
    // a market switch can't push stale candles into the new series.
    const myGen = state.chartLoadGen;
    const myExchange = state.exchange;
    const myAccount = state.account;
    const myMarketType = state.marketType;
    const mySymbol = state.market.symbol;
    const myTimeframe = state.selectedTimeframe;
    const params = new URLSearchParams({
      exchange: myExchange,
      account: myAccount,
      symbol: mySymbol,
      interval: myTimeframe,
      limit: '2',
      market_type: myMarketType,
    });
    let data;
    try { data = await api(`/api/candles?${params}`); } catch (_) { return; }
    // Drop stale poll responses.
    if (myGen !== state.chartLoadGen
      || myExchange !== state.exchange
      || myAccount !== state.account
      || myMarketType !== state.marketType
      || mySymbol !== state.market?.symbol
      || myTimeframe !== state.selectedTimeframe) {
      return;
    }
    const candles = (data && data.data && data.data.candles) || [];
    if (!candles.length) return;
    // /api/candles returns seconds; Lightweight Charts update() takes the
    // same shape. Incremental updates must NEVER call setData() here, or
    // they would wipe the entire loaded history (zoom, scroll position).
    const sorted = candles.slice().sort((a, b) => Number(a.time) - Number(b.time));
    for (const c of sorted) {
      const t = Number(c.time);
      if (!Number.isFinite(t) || t <= 0) continue;
      state.candleSeries.update({
        time: t,
        open: Number(c.open), high: Number(c.high), low: Number(c.low), close: Number(c.close),
      });
    }
  }

  function stopChartPolling() {
    if (state.chartPollTimer) {
      clearInterval(state.chartPollTimer);
      state.chartPollTimer = null;
    }
  }
  function startChartPolling() {
    stopChartPolling();
    state.chartPollTimer = setInterval(() => updateLatestCandle().catch(() => {}), POLL_INTERVAL_MS);
  }

  // ---------------------------- Markets list --------------------------
  //
  // The market browser is intentionally minimal: INSTRUMENT | PRICE.
  // 24h notional volume is internal ranking data only (known-volume rows
  // first, descending; unknown-volume rows after).
  // OI, 24h change, and the sort dropdown are NOT surfaced — WebTrade2
  // is a Web-native trading surface, not a stats dashboard.
  function renderMarkets(rows) {
    const box = $('#markets');
    if (!box) return;
    const visible = (rows || []).slice(0, 100);
    const selectedSymbol = state.market?.symbol;
    box.innerHTML = `
      <div class="market-head">
        <span>Instrument</span>
        <span>Price</span>
      </div>
      <div class="market-list-inner">${visible.map(r => {
        const priceTxt = formatDynamicPrice(r.price);
        const selected = r.symbol === selectedSymbol ? ' selected' : '';
        return `<button class="market-row${selected}" data-symbol="${fmt(r.symbol)}" title="${fmt(r.display_name || r.symbol)}">
          <span class="sym">${fmt(r.symbol)}</span>
          <span class="px">${priceTxt}</span>
        </button>`;
      }).join('') || `<div class="market-row empty"><span class="sym">No markets loaded</span><span class="px">—</span></div>`}</div>
    `;
    box.querySelectorAll('[data-symbol]').forEach(btn => btn.addEventListener('click', () => selectMarket(btn.dataset.symbol)));
  }

  function renderMarketHeader(row, priceData = null) {
    state.market = row || state.market;
    const symbol = state.market?.symbol || $('#instrument')?.value || 'Select market';
    const symEl = $('#marketSymbol');
    if (symEl) symEl.textContent = symbol;
    const priceEl = $('#currentPrice');
    const price = priceData?.price || priceData?.mark_price || state.market?.price;
    if (priceEl) {
      priceEl.textContent = formatDynamicPrice(price);
      priceEl.classList.remove('pos', 'neg');
    }
    // Funding only — no 24h Change, no 24h Volume, no OI.
    const fEl = $('#funding');
    if (!fEl) return;
    if (state.marketType === 'spot') {
      fEl.textContent = '—';
      fEl.className = 'val muted';
      return;
    }
    const fVal = priceData?.funding || state.market?.funding;
    fEl.textContent = formatFunding(fVal);
    fEl.className = 'val';
  }

  function renderAccountState(data) {
    state.accountState = data;
    const summary = data?.account_summary || {};
    const equityEl = $('#equity');
    if (equityEl) equityEl.textContent = formatMoney(summary.equity);
    const availEl = $('#available');
    if (availEl) availEl.textContent = formatMoney(summary.available);
    const upEl = $('#upnl');
    if (upEl) {
      upEl.textContent = formatSignedMoney(summary.unrealized_pnl);
      upEl.classList.remove('upnl-pos', 'upnl-neg');
      const cls = pnlClass(summary.unrealized_pnl);
      if (cls === 'pnl-pos') upEl.classList.add('upnl-pos');
      else if (cls === 'pnl-neg') upEl.classList.add('upnl-neg');
    }
    renderBottom();
  }

  function renderBottom() {
    const box = $('#dataPanel');
    if (!box) return;
    const data = state.accountState || {};
    if (state.bottomTab === 'positions') {
      const rows = data.positions || [];
      box.innerHTML = rows.length ? `<table><thead><tr><th>Instrument</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>PnL</th><th>Liq</th><th>TP</th><th>SL</th><th>Actions</th></tr></thead><tbody>${rows.map((p, idx) => { const side = fmt(p.side).toLowerCase(); const sideCls = side === 'buy' || side === 'long' ? 'side-buy' : (side === 'sell' || side === 'short' ? 'side-sell' : ''); const pnl = p.pnl ?? p.unrealized_pnl; const fmtSize = (() => { const n = num(p.size || p.position_size); if (n === null) return '—'; return n.toLocaleString(undefined, { maximumFractionDigits: 6 }); })(); const fmtPx = (v) => formatDynamicPrice(v); return `<tr><td>${fmt(p.symbol || p.instrument || p.market)}</td><td class="${sideCls}">${fmt(p.side)}</td><td>${fmtSize}</td><td>${fmtPx(p.entry_price || p.entry)}</td><td>${fmtPx(p.mark || p.mark_price)}</td><td class="${pnlClass(pnl)}">${formatSignedMoney(pnl)}</td><td>${fmtPx(p.liquidation_price)}</td><td>${fmtPx(p.tp)}</td><td>${fmtPx(p.sl)}</td><td class="row-actions"><button class="row-action" data-pos-action="tp" data-pos-index="${idx}">TP</button><button class="row-action" data-pos-action="sl" data-pos-index="${idx}">SL</button><button class="row-action destructive" data-pos-action="close" data-pos-index="${idx}">CLOSE</button></td></tr>`; }).join('')}</tbody></table>` : `<div class="empty">No positions</div>`;
      box.querySelectorAll('[data-pos-action]').forEach(btn => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.posIndex, 10);
          const pos = (data.positions || [])[idx];
          if (!pos) return;
          const action = btn.dataset.posAction;
          if (action === 'close') return confirmClose(pos);
          // TP/SL — prompt for price; modal requires explicit confirmation.
          const label = action.toUpperCase();
          const cur = action === 'tp' ? pos.tp : pos.sl;
          const fallback = action === 'tp' ? pos.tp : pos.sl;
          const entered = prompt(`${label} price for ${pos.symbol}${cur ? ` (current ${cur})` : ''}:`, fallback || '');
          if (entered === null) return;
          const price = String(entered).trim();
          if (!price) return;
          if (action === 'tp') return confirmTP(pos, price);
          if (action === 'sl') return confirmSL(pos, price);
        });
      });
    } else if (state.bottomTab === 'orders') {
      const rows = data.order_groups || [];
      box.innerHTML = rows.length ? `<table><thead><tr><th>Instrument</th><th>Side</th><th>Orders</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows.map((o, idx) => { const side = fmt(o.side).toLowerCase(); const sideCls = side === 'buy' || side === 'long' ? 'side-buy' : (side === 'sell' || side === 'short' ? 'side-sell' : ''); return `<tr><td>${fmt(o.symbol || o.instrument || o.market)}</td><td class="${sideCls}">${fmt(o.side)}</td><td>${fmt(o.order_count || o.count || '')}</td><td>${fmt(o.status || 'open')}</td><td class="row-actions"><button class="row-action destructive" data-ord-action="cancel" data-ord-index="${idx}">CANCEL</button></td></tr>`; }).join('')}</tbody></table>` : `<div class="empty">No open orders</div>`;
      box.querySelectorAll('[data-ord-action]').forEach(btn => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.ordIndex, 10);
          const ord = (data.order_groups || [])[idx];
          if (!ord) return;
          if (btn.dataset.ordAction === 'cancel') return confirmCancel(ord);
        });
      });
    } else {
      box.innerHTML = `<div class="empty">Fills are shown when the selected exchange exposes them.</div>`;
    }
  }

  async function loadMarkets() {
    if (!state.exchange || !state.account) return;
    const search = $('#marketSearch')?.value || '';
    // sort=volume (server default) keeps known-volume rows first desc,
    // unknown-volume rows after. Same ranking applied to search/favorites.
    const data = await api(`/api/markets?${new URLSearchParams({ exchange: state.exchange, account: state.account, market_type: state.marketType, search })}`);
    state.markets = data.markets || [];
    renderMarkets(state.markets);
    if (!state.market && state.markets.length) selectMarket(state.markets[0].symbol);
  }

  async function selectMarket(symbol) {
    if (!symbol) return;
    const input = $('#instrument');
    if (input) input.value = symbol;
    // Bump the load generation BEFORE any await so the in-flight
    // loadChartHistory() / poll from the previous selection is
    // immediately marked stale.
    state.chartLoadGen = (state.chartLoadGen || 0) + 1;
    state.market = state.markets.find(m => m.symbol === symbol) || { symbol };
    renderMarketHeader(state.market);
    renderMarkets(state.markets);
    stopChartPolling();
    try {
      const data = await api(`/api/market_price?${new URLSearchParams({ exchange: state.exchange, account: state.account, symbol, market_type: state.marketType })}`);
      const mp = data.market_price || data.data || data;
      renderMarketHeader(state.market, mp);
    } catch (_) {}
    await loadChartHistory();
    startChartPolling();
  }

  async function loadAccountState() {
    if (!state.exchange || !state.account) return;
    try {
      const data = await api(`/api/account/state?${new URLSearchParams({ exchange: state.exchange, account: state.account })}`);
      renderAccountState(data);
      if (state.market?.symbol) await renderChartOverlays();
    } catch (_) {}
  }

  function renderSelectors() {
    const exchangeSel = $('#exchange');
    if (exchangeSel) {
      exchangeSel.innerHTML = state.exchanges.map(x => `<option value="${x.exchange}">${x.exchange}</option>`).join('') || `<option>Exchange</option>`;
      state.exchange = exchangeSel.value = state.exchange || state.exchanges[0]?.exchange || '';
    }
    updateAccountsAndMarketTypes();
  }

  function updateAccountsAndMarketTypes() {
    const row = state.exchanges.find(x => x.exchange === state.exchange) || {};
    const accounts = row.accounts || [];
    const accountSel = $('#account');
    if (accountSel) {
      accountSel.innerHTML = accounts.map(a => {
        const value = typeof a === 'string' ? a : (a.account || a.id || a.name || a.label);
        const label = typeof a === 'string' ? a : (a.label || value);
        return `<option value="${value}">${label}</option>`;
      }).join('') || `<option>Account</option>`;
      state.account = accountSel.value;
    }
    const types = supportedMarketTypes(state.exchange);
    const typeSel = $('#marketType');
    state.marketType = preferredMarketType(types);
    if (typeSel) {
      typeSel.innerHTML = types.map(t => `<option value="${t}">${t === 'spot' ? 'Spot' : 'Futures'}</option>`).join('');
      typeSel.value = state.marketType;
    }
    const reduceWrap = $('#reduceOnlyWrap');
    if (reduceWrap) reduceWrap.hidden = !capabilityFor(state.exchange).features?.reduce_only;
  }

  async function refreshAll() {
    state.market = null;
    renderMarketHeader(null);
    stopChartPolling();
    await Promise.allSettled([loadMarkets(), loadAccountState()]);
    if (state.market?.symbol) startChartPolling();
  }

  async function previewLadder() {
    const symbol = $('#instrument')?.value || state.market?.symbol || '';
    const body = {
      exchange: state.exchange,
      account: state.account,
      market_type: state.marketType,
      symbol,
      side: state.ladderSide,
      start_price: $('#ladderStart')?.value || '0',
      end_price: $('#ladderEnd')?.value || '0',
      total_size: $('#ladderSize')?.value || '0',
      order_count: $('#ladderCount')?.value || '0',
      distribution: $('#ladderDistribution')?.value || 'uniform'
    };
    const out = await api('/api/ladder/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const meta = $('#ladderPreview .preview-meta');
    if (meta) meta.textContent = `${out.side} · ${out.symbol} · ${out.start_price} → ${out.end_price} · ${out.order_count} orders · ${out.total_size} · ${out.distribution} · Ladder VWAP ${out.vwap}`;
    const rows = $('#ladderPreviewRows');
    if (rows) {
      rows.innerHTML = (out.display_children || []).map((c, i) => {
        if (c.ellipsis) return `<tr><td colspan="2" class="muted">...</td></tr>`;
        const label = (out.children || []).length > 10 && i === 0 ? 'FIRST 5' : ((out.children || []).length > 10 && i === 6 ? 'LAST 5' : '');
        return `<tr><td>${label ? `<span class="muted">${label}</span> ` : ''}${fmt(c.price)}</td><td>${fmt(c.size)}</td></tr>`;
      }).join('') || `<tr><td colspan="2">No preview</td></tr>`;
    }
  }

  // ---------------------------- Phase 2 ---------------------------------

  // Local account persistence: ONLY the alias string per exchange.
  const ACCOUNT_STORAGE_KEY = 'webtrade2.accounts.v1';

  function loadAccountMap() {
    try {
      const raw = localStorage.getItem(ACCOUNT_STORAGE_KEY);
      if (!raw) return {};
      const obj = JSON.parse(raw);
      if (!obj || typeof obj !== 'object') return {};
      const out = {};
      for (const k of Object.keys(obj)) {
        if (typeof obj[k] === 'string' && /^[A-Za-z0-9_.-]{1,64}$/.test(obj[k])) {
          out[k] = obj[k];
        }
      }
      return out;
    } catch (_) {
      return {};
    }
  }

  function persistAccount(exchange, account) {
    try {
      const m = loadAccountMap();
      if (!account) {
        delete m[exchange];
      } else {
        m[exchange] = String(account);
      }
      localStorage.setItem(ACCOUNT_STORAGE_KEY, JSON.stringify(m));
    } catch (_) { /* localStorage may be disabled */ }
  }

  function resolveStoredAccount(exchange, validAccounts) {
    const map = loadAccountMap();
    const stored = map[exchange];
    if (stored && validAccounts.includes(stored)) return stored;
    // Fallback to first valid account (safe).
    return validAccounts[0] || null;
  }

  // Modal helpers
  function showModal(title, contextRows, detailsHtml, onConfirm, confirmLabel = 'CONFIRM') {
    const root = $('#modalRoot');
    if (!root) return;
    $('#modalTitle').textContent = title;
    $('#modalContext').innerHTML = contextRows.map(r => `<div class="ctx-row"><span class="ctx-lbl">${r.label}</span><span class="ctx-val">${r.value}</span></div>`).join('');
    $('#modalDetails').innerHTML = detailsHtml;
    const btn = $('#modalConfirm');
    btn.textContent = confirmLabel;
    btn.disabled = false;
    btn.dataset.inflight = '0';
    btn.onclick = async () => {
      if (btn.dataset.inflight === '1') return;
      btn.dataset.inflight = '1';
      btn.disabled = true;
      const origText = btn.textContent;
      btn.textContent = 'SUBMITTING…';
      try {
        const out = await onConfirm();
        if (out !== false) {
          hideModal();
          if (out && typeof out === 'object') showResult(out);
        }
      } finally {
        btn.dataset.inflight = '0';
        btn.textContent = origText;
        btn.disabled = false;
      }
    };
    root.hidden = false;
  }

  function hideModal() {
    const root = $('#modalRoot');
    if (root) root.hidden = true;
  }

  function showResult(payload) {
    const root = $('#resultRoot');
    if (!root) return;
    const status = String(payload.status || (payload.success ? 'OK' : 'ERROR'));
    const statusEl = $('#resultStatus');
    statusEl.className = 'result-status status-' + status.toLowerCase().replace(/_/g, '-');
    statusEl.textContent = status;
    // Pretty-print the full payload, stripping noisy fields for display.
    const display = Object.assign({}, payload);
    delete display.success;
    $('#resultBody').textContent = JSON.stringify(display, null, 2);
    root.hidden = false;
  }

  function hideResult() {
    const root = $('#resultRoot');
    if (root) root.hidden = true;
  }

  // Modal dismiss bindings (any element with data-modal-dismiss)
  document.addEventListener('click', (e) => {
    if (e.target?.hasAttribute?.('data-modal-dismiss')) hideModal();
    if (e.target?.hasAttribute?.('data-result-dismiss')) hideResult();
  });

  function contextRows() {
    return [
      { label: 'Exchange', value: fmt(state.exchange) || '—' },
      { label: 'Account', value: fmt(state.account) || '—' },
      { label: 'Market Type', value: fmt(state.marketType) || 'futures' },
      { label: 'Instrument', value: fmt(state.market?.symbol || $('#instrument')?.value) || '—' },
    ];
  }

  // ---- Preview / confirm / execute -----------------------------------

  let activeOrderPreview = null;     // {preview_id, ...}
  let activeLadderPreview = null;
  let previewCountdownTimer = null;

  // ---- Preview invalidation ------------------------------------------
  // Any change to execution-relevant inputs MUST visibly require
  // PREVIEW AGAIN. We mark the active preview as stale (the server
  // would also reject it because bindings changed) and rewrite the
  // confirm button to read "PREVIEW AGAIN".
  function invalidateActivePreviews(reason) {
    const orderPreview = $('#orderPreview');
    const ladderPreviewBlock = $('#ladderPreview');
    let dirty = false;
    if (activeOrderPreview) {
      activeOrderPreview = null;
      const sum = orderPreview?.querySelector?.('.preview-summary');
      if (sum) {
        const reasonText = reason ? ` — ${reason}` : '';
        sum.innerHTML = `<div class="muted preview-stale">PREVIEW STALE${reasonText}. Click PREVIEW ORDER to refresh.</div>`;
      }
      const exp = orderPreview?.querySelector?.('.preview-expiry');
      if (exp) exp.textContent = '';
      const btn = orderPreview?.querySelector?.('.confirm-action');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'PREVIEW AGAIN';
      }
      clearPreviewCountdown();
      dirty = true;
    }
    if (activeLadderPreview) {
      activeLadderPreview = null;
      // The ladder preview lives in the wizard modal context. The
      // simplest UX is to dismiss any open confirm modal entirely so
      // the user must re-preview.
      hideModal();
      dirty = true;
    }
    return dirty;
  }

  // Wire input-change invalidation. Use both 'input' (typing) and
  // 'change' (committed) for select/checkbox elements.
  ['#orderPrice', '#orderSize', '#reduceOnly'].forEach(sel => {
    document.addEventListener('input', (e) => {
      if (e.target?.matches?.(sel)) invalidateActivePreviews(`${sel.replace('#','')} changed`);
    });
    document.addEventListener('change', (e) => {
      if (e.target?.matches?.(sel)) invalidateActivePreviews(`${sel.replace('#','')} changed`);
    });
  });
  // Order side buttons (#orderBuy / #orderSell) and ladder side buttons
  // (#ladderBuy / #ladderSell) are wired directly below in wire(); this
  // generic delegated listener is no longer needed for side toggling.
  // Ladder inputs
  ['#ladderStart', '#ladderEnd', '#ladderSize', '#ladderCount', '#ladderDistribution'].forEach(sel => {
    document.addEventListener('input', (e) => {
      if (e.target?.matches?.(sel)) invalidateActivePreviews(`${sel.replace('#','')} changed`);
    });
    document.addEventListener('change', (e) => {
      if (e.target?.matches?.(sel)) invalidateActivePreviews(`${sel.replace('#','')} changed`);
    });
  });
  // Instrument selection (typing into #instrument OR clicking a market row)
  ['#instrument'].forEach(sel => {
    document.addEventListener('input', (e) => {
      if (e.target?.matches?.(sel)) invalidateActivePreviews('instrument changed');
    });
    document.addEventListener('change', (e) => {
      if (e.target?.matches?.(sel)) invalidateActivePreviews('instrument changed');
    });
  });
  document.addEventListener('click', (e) => {
    if (e.target?.closest?.('.market-row')) invalidateActivePreviews('market changed');
  });
  // Market type
  document.addEventListener('change', (e) => {
    if (e.target?.id === 'marketType') invalidateActivePreviews('market type changed');
  });

  function clearPreviewCountdown() {
    if (previewCountdownTimer) {
      clearInterval(previewCountdownTimer);
      previewCountdownTimer = null;
    }
  }

  function startPreviewCountdown(expiresAtMs, onExpire) {
    clearPreviewCountdown();
    const tick = () => {
      const remaining = Math.max(0, expiresAtMs - Date.now());
      const els = document.querySelectorAll('.preview-expiry');
      els.forEach(el => { el.textContent = `Preview expires in ${remaining}s`; });
      if (remaining <= 0) {
        clearPreviewCountdown();
        if (typeof onExpire === 'function') onExpire();
      }
    };
    tick();
    previewCountdownTimer = setInterval(tick, 1000);
  }

  // ---- Authoritative order-side state -------------------------------
  //
  // The order BUY/SELL buttons were previously a DOM-as-state anti-pattern:
  // the click handler invalidated previews but never mutated state, and
  // previewOrder() derived side from `$('.side-row .seg.buy.active')` —
  // i.e. from CSS classes on the DOM. Because the .active class was only
  // set in the initial HTML and never toggled, the side was permanently
  // 'buy' regardless of which button the user tapped.
  //
  // Fix: single authoritative state.orderSide = 'buy' | 'sell'; click
  // handlers update state AND DOM in lockstep; previewOrder reads state
  // only — never the DOM.

  function setOrderSide(side) {
    const normalized = (side === 'sell') ? 'sell' : 'buy';
    state.orderSide = normalized;
    const buyBtn = $('#orderBuy');
    const sellBtn = $('#orderSell');
    if (buyBtn) buyBtn.classList.toggle('active', normalized === 'buy');
    if (sellBtn) sellBtn.classList.toggle('active', normalized === 'sell');
  }

  function setLadderSide(side) {
    const normalized = (side === 'sell') ? 'sell' : 'buy';
    state.ladderSide = normalized;
    const buyBtn = $('#ladderBuy');
    const sellBtn = $('#ladderSell');
    if (buyBtn) buyBtn.classList.toggle('active', normalized === 'buy');
    if (sellBtn) sellBtn.classList.toggle('active', normalized === 'sell');
  }

  async function previewOrder() {
    if (!state.exchange || !state.account) {
      alert('Pick an exchange and account first.');
      return;
    }
    const price = $('#orderPrice')?.value || '';
    const size = $('#orderSize')?.value || '';
    const reduceOnly = !!$('#reduceOnly')?.checked;
    const side = (state.orderSide === 'sell') ? 'sell' : 'buy';
    const orderType = 'limit';
    let body;
    try {
      body = await api('/api/trade/preview_order', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          exchange: state.exchange, account: state.account, market_type: state.marketType,
          symbol: state.market?.symbol || $('#instrument')?.value || '',
          side, order_type: orderType, size, price, reduce_only: reduceOnly,
        }),
      });
    } catch (err) {
      alert('Preview failed: ' + err.message);
      return;
    }
    if (!body.success) {
      alert('Preview rejected: ' + (body.error?.message || body.error?.code || 'unknown'));
      return;
    }
    activeOrderPreview = body;
    const summary = `${body.side.toUpperCase()} ${body.native_symbol} LIMIT @ ${body.final_price} × ${body.final_size} (notional ${body.notional})`;
    $('#orderPreview .preview-summary').innerHTML = `
      <div><span class="ctx-lbl">Side</span><span class="ctx-val">${body.side.toUpperCase()}</span></div>
      <div><span class="ctx-lbl">Order Type</span><span class="ctx-val">LIMIT</span></div>
      <div><span class="ctx-lbl">Final Price</span><span class="ctx-val">${fmt(body.final_price)}</span></div>
      <div><span class="ctx-lbl">Final Size</span><span class="ctx-val">${fmt(body.final_size)}</span></div>
      <div><span class="ctx-lbl">Notional</span><span class="ctx-val">${fmt(body.notional)}</span></div>
      <div><span class="ctx-lbl">Reduce Only</span><span class="ctx-val">${body.reduce_only ? 'YES' : 'no'}</span></div>
      <div class="muted">${summary}</div>`;
    $('#orderPreview').hidden = false;
    $('#orderPreview .confirm-action').disabled = false;
    startPreviewCountdown(Date.now() + (body.expires_in_s || 300) * 1000, () => {
      activeOrderPreview = null;
      $('#orderPreview').hidden = true;
    });
    showModal(
      'CONFIRM ORDER',
      contextRows(),
      `<div class="ctx-row"><span class="ctx-lbl">Side</span><span class="ctx-val">${body.side.toUpperCase()}</span></div>
       <div><span class="ctx-lbl">Order Type</span><span class="ctx-val">LIMIT</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Final Price</span><span class="ctx-val">${fmt(body.final_price)}</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Final Size</span><span class="ctx-val">${fmt(body.final_size)}</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Notional</span><span class="ctx-val">${fmt(body.notional)}</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Reduce Only</span><span class="ctx-val">${body.reduce_only ? 'YES' : 'no'}</span></div>
       <div class="muted">${summary}</div>`,
      async () => {
        try {
          const r = await api('/api/trade/execute', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preview_id: body.preview_id }),
          });
          activeOrderPreview = null;
          $('#orderPreview').hidden = true;
          clearPreviewCountdown();
          await loadAccountState();
          await loadMarkets();
          return r;
        } catch (err) {
          alert('Execute failed: ' + err.message);
          return false;
        }
      },
      state.dryRun ? 'CONFIRM DRY RUN' : 'CONFIRM & SUBMIT'
    );
  }

  async function previewLadderThenConfirm() {
    if (!state.exchange || !state.account) {
      alert('Pick an exchange and account first.');
      return;
    }
    const symbol = state.market?.symbol || $('#instrument')?.value || '';
    const startPrice = $('#ladderStart')?.value || '';
    const endPrice = $('#ladderEnd')?.value || '';
    const totalSize = $('#ladderSize')?.value || '';
    const orderCount = parseInt($('#ladderCount')?.value || '0', 10);
    const distribution = $('#ladderDistribution')?.value || 'uniform';
    const side = state.ladderSide || 'buy';
    let body;
    try {
      body = await api('/api/trade/preview_ladder', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          exchange: state.exchange, account: state.account, market_type: state.marketType,
          symbol, side, distribution, order_count: orderCount, total_size: totalSize,
          start_price: startPrice, end_price: endPrice,
        }),
      });
    } catch (err) {
      alert('Preview failed: ' + err.message);
      return;
    }
    if (!body.success) {
      alert('Preview rejected: ' + (body.error?.message || body.error?.code || 'unknown'));
      return;
    }
    activeLadderPreview = body;
    const childrenRows = (body.display_children || []).map(c => {
      if (c.ellipsis) return `<tr><td colspan="2" class="muted">…</td></tr>`;
      return `<tr><td>${fmt(c.price)}</td><td>${fmt(c.size)}</td></tr>`;
    }).join('');
    // Step 7 (controlled activation): LIVE ladder test is NOT YET enabled.
    // Preview is allowed; Confirm is gated with an explicit safety banner.
    if (!state.dryRun) {
      showModal(
        'CONFIRM LADDER (BLOCKED)',
        contextRows().concat([
          { label: 'Side', value: body.side.toUpperCase() },
          { label: 'Distribution', value: body.distribution },
          { label: 'Start', value: fmt(body.start_price) },
          { label: 'End', value: fmt(body.end_price) },
        ]),
        `<div class="ladder-blocked-banner">
           <div class="ladder-blocked-title">LIVE LADDER TEST NOT YET ENABLED</div>
           <div class="ladder-blocked-detail">
             Ladder preview is informational only. The CONFIRM action is currently
             blocked until the controlled LIVE ladder activation is approved.
           </div>
         </div>
         <div class="ctx-row"><span class="ctx-lbl">Requested Orders</span><span class="ctx-val">${fmt(body.order_count)}</span></div>
         <div class="ctx-row"><span class="ctx-lbl">Final Normalized Count</span><span class="ctx-val">${fmt(body.order_count)}</span></div>
         <div class="ctx-row"><span class="ctx-lbl">Total Size</span><span class="ctx-val">${fmt(body.total_size)}</span></div>
         <div class="ctx-row"><span class="ctx-lbl">Ladder VWAP</span><span class="ctx-val">${fmt(body.vwap)}</span></div>
         <table class="modal-children"><thead><tr><th>PRICE</th><th>SIZE</th></tr></thead><tbody>${childrenRows}</tbody></table>`,
        async () => { return { success: false, error: { code: 'LADDER_NOT_ENABLED', message: 'LIVE ladder test is not yet enabled.' } }; },
        'BLOCKED - NOT ENABLED'
      );
      return;
    }
    showModal(
      'CONFIRM LADDER',
      contextRows().concat([
        { label: 'Side', value: body.side.toUpperCase() },
        { label: 'Distribution', value: body.distribution },
        { label: 'Start', value: fmt(body.start_price) },
        { label: 'End', value: fmt(body.end_price) },
      ]),
      `<div class="ctx-row"><span class="ctx-lbl">Requested Orders</span><span class="ctx-val">${fmt(body.order_count)}</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Final Normalized Count</span><span class="ctx-val">${fmt(body.order_count)}</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Total Size</span><span class="ctx-val">${fmt(body.total_size)}</span></div>
       <div class="ctx-row"><span class="ctx-lbl">Ladder VWAP</span><span class="ctx-val">${fmt(body.vwap)}</span></div>
       <table class="modal-children"><thead><tr><th>PRICE</th><th>SIZE</th></tr></thead><tbody>${childrenRows}</tbody></table>`,
      async () => {
        try {
          const r = await api('/api/trade/execute', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preview_id: body.preview_id }),
          });
          activeLadderPreview = null;
          clearPreviewCountdown();
          await loadAccountState();
          await loadMarkets();
          return r;
        } catch (err) {
          alert('Execute failed: ' + err.message);
          return false;
        }
      },
      state.dryRun ? 'CONFIRM DRY RUN' : 'CONFIRM & SUBMIT'
    );
  }

  async function confirmTP(position, newPrice) {
    showModal('SET TAKE PROFIT', contextRows().concat([
      { label: 'Position Side', value: fmt(position.side) },
      { label: 'Position Size', value: fmt(position.size) },
      { label: 'Entry', value: fmt(position.entry_price) },
      { label: 'Mark', value: fmt(position.mark) },
      { label: 'New TP', value: fmt(newPrice) },
    ]), '', async () => {
      const r = await api('/api/position/set_tp', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exchange: state.exchange, account: state.account, symbol: position.symbol, price: newPrice }),
      });
      await loadAccountState();
      return r;
    }, state.dryRun ? 'CONFIRM TP (DRY RUN)' : 'Confirm TP');
  }

  async function confirmSL(position, newPrice) {
    showModal('SET STOP LOSS', contextRows().concat([
      { label: 'Position Side', value: fmt(position.side) },
      { label: 'Position Size', value: fmt(position.size) },
      { label: 'Entry', value: fmt(position.entry_price) },
      { label: 'Mark', value: fmt(position.mark) },
      { label: 'New SL', value: fmt(newPrice) },
    ]), '', async () => {
      const r = await api('/api/position/set_sl', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exchange: state.exchange, account: state.account, symbol: position.symbol, price: newPrice }),
      });
      await loadAccountState();
      return r;
    }, state.dryRun ? 'CONFIRM SL (DRY RUN)' : 'Confirm SL');
  }

  async function confirmClose(position) {
    showModal('CLOSE POSITION', contextRows().concat([
      { label: 'Side', value: fmt(position.side) },
      { label: 'Current Size', value: fmt(position.size) },
      { label: 'Mark', value: fmt(position.mark) },
      { label: 'Requested Close', value: 'Full Close' },
    ]), '', async () => {
      const r = await api('/api/position/close', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exchange: state.exchange, account: state.account, symbol: position.symbol }),
      });
      await loadAccountState();
      return r;
    }, state.dryRun ? 'CONFIRM CLOSE (DRY RUN)' : 'CONFIRM CLOSE');
  }

  async function confirmCancel(order) {
    showModal('CANCEL ORDER', contextRows().concat([
      { label: 'Side', value: fmt(order.side) },
      { label: 'Price', value: fmt(order.price) },
      { label: 'Remaining Size', value: fmt(order.size || order.remaining) },
      { label: 'Order ID', value: fmt(order.order_id || order.id || '') },
    ]), '', async () => {
      const r = await api('/api/orders/cancel_group', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          exchange: state.exchange, account: state.account, symbol: order.symbol,
          side: order.side, order_type: order.order_type || order.classification || 'limit',
          order_ids: order.order_id ? [order.order_id] : undefined,
        }),
      });
      await loadAccountState();
      return r;
    }, state.dryRun ? 'CONFIRM CANCEL (DRY RUN)' : 'CONFIRM CANCEL');
  }

  // ---- Capabilities / dry-run ----------------------------------------

  async function loadPhase2Status() {
    try {
      const s = await api('/api/phase2');
      state.dryRun = !!s.dry_run;
      state.writeEnabled = !!s.write_enabled;
      const banner = $('#dryRunBanner');
      if (banner) banner.hidden = !state.dryRun;
      const badge = $('#phaseBadge');
      if (badge) {
        if (!state.writeEnabled) {
          badge.textContent = 'Phase 2 · Writes disabled';
        } else if (state.dryRun) {
          badge.textContent = 'Phase 2 · Dry-run';
        } else {
          badge.textContent = 'Phase 2 · LIVE';
        }
      }
    } catch (_) { /* phase2 endpoint may not exist in older deployments */ }
  }

  function wire() {
    document.querySelectorAll('[data-trade-tab]').forEach(btn => btn.addEventListener('click', () => setTradeTab(btn.dataset.tradeTab)));
    document.querySelectorAll('[data-mobile-target]').forEach(btn => btn.addEventListener('click', () => setMobileSection(btn.dataset.mobileTarget)));
    document.querySelectorAll('[data-bottom-tab]').forEach(btn => btn.addEventListener('click', () => { state.bottomTab = btn.dataset.bottomTab; document.querySelectorAll('[data-bottom-tab]').forEach(b => b.classList.toggle('active', b === btn)); renderBottom(); }));
    document.querySelectorAll('[data-timeframe]').forEach(btn => btn.addEventListener('click', () => setTimeframe(btn.dataset.timeframe)));
    $('#exchange')?.addEventListener('change', async (e) => { state.exchange = e.target.value; invalidateActivePreviews('exchange changed'); updateAccountsAndMarketTypes(); await refreshAll(); });
    $('#account')?.addEventListener('change', async (e) => { state.account = e.target.value; invalidateActivePreviews('account changed'); persistAccount(state.exchange, state.account); await refreshAll(); });
    $('#marketType')?.addEventListener('change', async (e) => { state.marketType = e.target.value; localStorage.setItem("webtrade2.marketType", state.marketType); await refreshAll(); });
    $('#marketSearch')?.addEventListener('input', () => loadMarkets().catch(() => {}));
    // (No explicit sort selector — the server defaults to volume-desc
    // ranking for all markets including search/favorites results.)
    $('#previewLadder')?.addEventListener('click', () => previewLadder().catch(err => { const meta = $('#ladderPreview .preview-meta'); if (meta) meta.textContent = err.message; }));
    $('#previewOrderBtn')?.addEventListener('click', () => previewOrder().catch(err => alert('Preview failed: ' + err.message)));
    $('#orderBuy')?.addEventListener('click', () => { setOrderSide('buy'); invalidateActivePreviews('order side changed to buy'); });
    $('#orderSell')?.addEventListener('click', () => { setOrderSide('sell'); invalidateActivePreviews('order side changed to sell'); });
    $('#ladderBuy')?.addEventListener('click', () => { setLadderSide('buy'); invalidateActivePreviews('ladder side changed to buy'); });
    $('#ladderSell')?.addEventListener('click', () => { setLadderSide('sell'); invalidateActivePreviews('ladder side changed to sell'); });
    $('#loginForm')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const password = $('#loginPassword')?.value || '';
      const res = await fetch('/login', { method: 'POST', body: new URLSearchParams({ password }), credentials: 'same-origin', redirect: 'manual' });
      if (res.status === 303 || res.status === 0 || res.ok) { showLogin(false); await boot(); }
      else showLogin(true, 'Invalid password');
    });
    window.addEventListener('resize', () => { if (state.chart) state.chart.applyOptions({}); });
  }

  async function boot() {
    initChart();
    setTimeframe(state.defaultTimeframe);
    setTradeTab(state.tradeTab);
    setMobileSection(state.mobile);
    // Sync side-button DOM with authoritative state (defensive: in case
    // the HTML default diverges from state.orderSide on load).
    setOrderSide(state.orderSide);
    setLadderSide(state.ladderSide);
    try {
      const session = await api('/api/session');
      state.csrf = session.csrf;
      showLogin(false);
      const exchanges = await api('/api/exchanges');
      state.exchanges = exchanges.exchanges || [];
      renderSelectors();
      await loadPhase2Status();
      await refreshAll();
    } catch (err) {
      showLogin(true, 'Sign in to load read-only data');
      renderMarkets([{symbol:'Login required', price:'—'}]);
    }
  }

  wire();
  boot();
})();