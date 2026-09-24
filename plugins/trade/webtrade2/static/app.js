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
    chart: null,
    candleSeries: null,
    volumeSeries: null,
    chartReady: false,
    chartPollTimer: null,
    selectedTimeframe: '1m',
    defaultTimeframe: '1m',
    priceLines: [],
  };
  const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1D'];
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
  function formatCompactVolume(raw) {
    if (raw === null || raw === undefined || raw === '') return null;
    const n = num(raw);
    if (n === null || n === 0) return null;
    const abs = Math.abs(n);
    const sign = n < 0 ? '-' : '';
    if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2).replace(/\.?0+$/, '')}B`;
    if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
    if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1).replace(/\.0$/, '')}K`;
    return `${sign}$${abs.toFixed(2).replace(/\.?0+$/, '') || '0'}`;
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
  function formatPctChange(raw) {
    if (raw === null || raw === undefined || raw === '') return '—';
    const s = String(raw).replace('%', '');
    const n = num(s);
    if (n === null) return '—';
    const sign = n > 0 ? '+' : '';
    return `${sign}${n.toFixed(2)}%`;
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
  }

  function setTimeframe(tf) {
    if (!TIMEFRAMES.includes(tf)) return;
    state.selectedTimeframe = tf;
    document.querySelectorAll('[data-timeframe]').forEach(btn => btn.classList.toggle('active', btn.dataset.timeframe === tf));
    if (state.exchange && state.account && state.market?.symbol) {
      loadChartHistory().catch(() => {});
      startChartPolling();
    }
  }

  // ---------------------------- Chart ---------------------------------
  function initChart() {
    const container = $('#chart');
    if (!container) return false;
    if (!window.LightweightCharts || typeof window.LightweightCharts.createChart !== 'function') return false;
    const chart = window.LightweightCharts.createChart(container, {
      layout: { background: { color: '#0b0e14' }, textColor: '#e6ecf5', fontFamily: 'Inter, system-ui, sans-serif' },
      grid: { vertLines: { color: '#161d2f' }, horzLines: { color: '#161d2f' } },
      rightPriceScale: { borderColor: '#1c2438', textColor: '#8a9bb5' },
      timeScale: { borderColor: '#1c2438', timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      autoSize: true,
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
    chart.timeScale().fitContent();
    return true;
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

  async function loadChartHistory() {
    if (!state.chartReady || !state.market?.symbol || !state.exchange) return;
    const params = new URLSearchParams({
      exchange: state.exchange,
      account: state.account,
      symbol: state.market.symbol,
      interval: state.selectedTimeframe,
      limit: String(HISTORY_LIMIT),
      market_type: state.marketType,
    });
    let data;
    try {
      data = await api(`/api/candles?${params}`);
    } catch (err) {
      return;
    }
    const candles = (data && data.data && data.data.candles) || [];
    const series = candles.map(c => ({
      time: Math.floor(Number(c.time) / 1000),
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
    })).filter(c => Number.isFinite(c.open) && Number.isFinite(c.high) && Number.isFinite(c.low) && Number.isFinite(c.close) && c.time > 0);
    series.sort((a, b) => a.time - b.time);
    state.candleSeries.setData(series);
    if (series.length) applyChartPrecision(series[series.length - 1].close);
    state.chart.timeScale().fitContent();
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
    const params = new URLSearchParams({
      exchange: state.exchange,
      account: state.account,
      symbol: state.market.symbol,
      interval: state.selectedTimeframe,
      limit: '2',
      market_type: state.marketType,
    });
    let data;
    try { data = await api(`/api/candles?${params}`); } catch (_) { return; }
    const candles = (data && data.data && data.data.candles) || [];
    if (!candles.length) return;
    const sorted = candles.slice().sort((a, b) => Number(a.time) - Number(b.time));
    for (const c of sorted) {
      const t = Math.floor(Number(c.time) / 1000);
      if (!t || t <= 0) continue;
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
  function renderMarkets(rows) {
    const box = $('#markets');
    if (!box) return;
    const visible = (rows || []).slice(0, 100);
    const selectedSymbol = state.market?.symbol;
    box.innerHTML = visible.map(r => {
      const priceTxt = formatDynamicPrice(r.price);
      const chgTxt = formatPctChange(r.change_24h);
      const volTxt = formatCompactVolume(r.volume_24h);
      const chgCls = pnlClass(r.change_24h);
      const selected = r.symbol === selectedSymbol ? ' selected' : '';
      return `<button class="market-row${selected}" data-symbol="${fmt(r.symbol)}" title="${fmt(r.display_name || r.symbol)}">
        <span class="sym">${fmt(r.symbol)}</span><span class="px">${priceTxt}</span><span class="chg ${chgCls}">${chgTxt}</span><span class="vol">${volTxt ?? '—'}</span>
      </button>`;
    }).join('') || `<div class="market-row"><span class="sym">No markets loaded</span><span class="px">—</span><span class="chg">—</span><span class="vol">—</span></div>`;
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
    const chgEl = $('#change24h');
    if (chgEl) {
      chgEl.textContent = formatPctChange(state.market?.change_24h);
      chgEl.className = 'val ' + pnlClass(state.market?.change_24h);
    }
    const volEl = $('#volume24h');
    if (volEl) {
      const txt = formatCompactVolume(state.market?.volume_24h);
      volEl.textContent = txt || '—';
      volEl.className = 'val';
    }
    const fEl = $('#funding');
    if (state.marketType === 'spot') {
      if (fEl) { fEl.textContent = '—'; fEl.className = 'val muted'; }
    } else {
      const fVal = priceData?.funding || state.market?.funding;
      if (fEl) {
        fEl.textContent = formatFunding(fVal);
        fEl.className = 'val';
      }
    }
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
      box.innerHTML = rows.length ? `<table><thead><tr><th>Instrument</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>PnL</th><th>Liq</th><th>TP</th><th>SL</th><th>Close</th></tr></thead><tbody>${rows.map(p => { const side = fmt(p.side).toLowerCase(); const sideCls = side === 'buy' || side === 'long' ? 'side-buy' : (side === 'sell' || side === 'short' ? 'side-sell' : ''); const pnl = p.pnl ?? p.unrealized_pnl; const fmtSize = (() => { const n = num(p.size || p.position_size); if (n === null) return '—'; return n.toLocaleString(undefined, { maximumFractionDigits: 6 }); })(); const fmtPx = (v) => formatDynamicPrice(v); return `<tr><td>${fmt(p.symbol || p.instrument || p.market)}</td><td class="${sideCls}">${fmt(p.side)}</td><td>${fmtSize}</td><td>${fmtPx(p.entry_price || p.entry)}</td><td>${fmtPx(p.mark || p.mark_price)}</td><td class="${pnlClass(pnl)}">${formatSignedMoney(pnl)}</td><td>${fmtPx(p.liquidation_price)}</td><td>${fmtPx(p.tp)}</td><td>${fmtPx(p.sl)}</td><td class="muted">Read-only</td></tr>`; }).join('')}</tbody></table>` : `<div class="empty">No positions</div>`;
    } else if (state.bottomTab === 'orders') {
      const rows = data.order_groups || [];
      box.innerHTML = rows.length ? `<table><thead><tr><th>Instrument</th><th>Side</th><th>Orders</th><th>Status</th></tr></thead><tbody>${rows.map(o => { const side = fmt(o.side).toLowerCase(); const sideCls = side === 'buy' || side === 'long' ? 'side-buy' : (side === 'sell' || side === 'short' ? 'side-sell' : ''); return `<tr><td>${fmt(o.symbol || o.instrument || o.market)}</td><td class="${sideCls}">${fmt(o.side)}</td><td>${fmt(o.order_count || o.count || '')}</td><td>${fmt(o.status || 'open')}</td></tr>`; }).join('')}</tbody></table>` : `<div class="empty">No open orders</div>`;
    } else {
      box.innerHTML = `<div class="empty">Fills are shown when the selected exchange exposes them.</div>`;
    }
  }

  async function loadMarkets() {
    if (!state.exchange || !state.account) return;
    const search = $('#marketSearch')?.value || '';
    const data = await api(`/api/markets?${new URLSearchParams({ exchange: state.exchange, account: state.account, market_type: state.marketType, search })}`);
    state.markets = data.markets || [];
    renderMarkets(state.markets);
    if (!state.market && state.markets.length) selectMarket(state.markets[0].symbol);
  }

  async function selectMarket(symbol) {
    if (!symbol) return;
    const input = $('#instrument');
    if (input) input.value = symbol;
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

  function wire() {
    document.querySelectorAll('[data-trade-tab]').forEach(btn => btn.addEventListener('click', () => setTradeTab(btn.dataset.tradeTab)));
    document.querySelectorAll('[data-mobile-target]').forEach(btn => btn.addEventListener('click', () => setMobileSection(btn.dataset.mobileTarget)));
    document.querySelectorAll('[data-bottom-tab]').forEach(btn => btn.addEventListener('click', () => { state.bottomTab = btn.dataset.bottomTab; document.querySelectorAll('[data-bottom-tab]').forEach(b => b.classList.toggle('active', b === btn)); renderBottom(); }));
    document.querySelectorAll('[data-timeframe]').forEach(btn => btn.addEventListener('click', () => setTimeframe(btn.dataset.timeframe)));
    $('#exchange')?.addEventListener('change', async (e) => { state.exchange = e.target.value; updateAccountsAndMarketTypes(); await refreshAll(); });
    $('#account')?.addEventListener('change', async (e) => { state.account = e.target.value; await refreshAll(); });
    $('#marketType')?.addEventListener('change', async (e) => { state.marketType = e.target.value; localStorage.setItem("webtrade2.marketType", state.marketType); await refreshAll(); });
    $('#marketSearch')?.addEventListener('input', () => loadMarkets().catch(() => {}));
    $('#previewLadder')?.addEventListener('click', () => previewLadder().catch(err => { const meta = $('#ladderPreview .preview-meta'); if (meta) meta.textContent = err.message; }));
    $('#ladderBuy')?.addEventListener('click', () => { state.ladderSide = 'buy'; $('#ladderBuy')?.classList.add('active-side'); $('#ladderSell')?.classList.remove('active-side'); });
    $('#ladderSell')?.addEventListener('click', () => { state.ladderSide = 'sell'; $('#ladderSell')?.classList.add('active-side'); $('#ladderBuy')?.classList.remove('active-side'); });
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
    try {
      const session = await api('/api/session');
      state.csrf = session.csrf;
      showLogin(false);
      const exchanges = await api('/api/exchanges');
      state.exchanges = exchanges.exchanges || [];
      renderSelectors();
      await refreshAll();
    } catch (err) {
      showLogin(true, 'Sign in to load read-only data');
      renderMarkets([{symbol:'Login required', price:'—', change_24h:'—', volume_24h:'—'}]);
    }
  }

  wire();
  boot();
})();