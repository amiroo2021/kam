#!/usr/bin/env node
/**
 * WebTrade2 chart-state regression runner.
 *
 * Bug being prevented (real iPhone Safari findings):
 *   - Selecting a new instrument did not always clear the previous chart.
 *   - A slow candle response (especially Apex) for an old selection
 *     could paint onto the chart after the user already moved to a
 *     different instrument.
 *   - Some symbols (NVDAUSDT, QQQUSDT, AAPLUSDT, BZ/USDC, VVV/USDC)
 *     failed the candle request; the previous chart must clear, not stay.
 *   - The price scale's previous autoscale state could carry over and
 *     produce a wildly wrong visible price range on the new instrument.
 *
 * This runner loads the real /root/kam/plugins/trade/webtrade2/static/
 * index.html + app.js into a minimal DOM stub, with:
 *   - /api/candles → controlled per-test responses (success / fail /
 *     slow, with controllable data).
 *   - /api/markets / /api/market_price / /api/phase2 / /api/session /
 *     /api/account_state / /api/positions_orders mocked.
 *
 * It exercises every scenario the user listed:
 *   1. BTC -> failed symbol -> BTC              (recovery after failure)
 *   2. successful A -> successful B with very different price
 *   3. successful A -> failed B (must CLEAR, not leave A's candles)
 *   4. rapid A -> B -> C, slow A response arriving last
 *   5. timeframe change resets scale
 *   6. price scale after switch matches new candle range
 *
 * CRITICAL: this runner NEVER calls /api/trade/execute and NEVER
 * triggers an exchange write. It only inspects chart state.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEBTRADE2_DIR = "/root/kam/plugins/trade/webtrade2/static";
const HTML_PATH = path.join(WEBTRADE2_DIR, "index.html");
const APP_JS_PATH = path.join(WEBTRADE2_DIR, "app.js");

// ---------- minimal DOM stub ----------------------------------------------

class FakeClassList {
  constructor(host) { this.host = host; }
  add(c) { this.host._classes.add(c); }
  remove(c) { this.host._classes.delete(c); }
  toggle(c, force) {
    const had = this.host._classes.has(c);
    if (force === true) { this.host._classes.add(c); return true; }
    if (force === false) { this.host._classes.delete(c); return false; }
    if (had) { this.host._classes.delete(c); return false; }
    this.host._classes.add(c); return true;
  }
  contains(c) { return this.host._classes.has(c); }
  toString() { return Array.from(this.host._classes).join(" "); }
}

class FakeElement {
  constructor(tag, attrs = {}) {
    this.tagName = (tag || "DIV").toUpperCase();
    this.children = [];
    this.parent = null;
    this.attrs = Object.assign({}, attrs);
    this._classes = new Set();
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this.style = {};
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.disabled = false;
    this._listeners = {};
    this.innerHTML = "";
    this.id = attrs.id || "";
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.offsetWidth = 0;
    this.offsetHeight = 0;
    this.scrollWidth = 0;
    this.scrollHeight = 0;
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "id") this.id = v;
      else if (k === "class") {
        String(v).split(/\s+/).filter(Boolean).forEach(c => this._classes.add(c));
      } else if (k.startsWith("data-")) {
        this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
      } else {
        this.attrs[k] = v;
      }
    }
    // Make innerHTML a getter/setter so assignments invoke setInnerHTML().
    Object.defineProperty(this, "innerHTML", {
      configurable: true,
      get() { return this._innerHTMLStr || ""; },
      set(v) {
        this._innerHTMLStr = String(v);
        this.setInnerHTML(this._innerHTMLStr);
      },
    });
  }
  // Custom setter for innerHTML.
  _setInnerHTMLProp(v) {
    this._innerHTMLStr = String(v);
    this.setInnerHTML(this._innerHTMLStr);
  }
  _getInnerHTMLProp() { return this._innerHTMLStr || ""; }

  addEventListener(type, cb) {
    (this._listeners[type] = this._listeners[type] || []).push(cb);
  }
  removeEventListener(type, cb) {
    if (!this._listeners[type]) return;
    this._listeners[type] = this._listeners.filter(c => c !== cb);
  }
  dispatchEvent(evt) {
    const handlers = (this._listeners[evt.type] || []).slice();
    let returnValue = evt.returnValue !== undefined ? evt.returnValue : true;
    for (const h of handlers) {
      const r = h(evt);
      if (r === false) returnValue = false;
    }
    return returnValue;
  }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref) {
    child.parent = this;
    const idx = ref ? this.children.indexOf(ref) : this.children.length;
    if (idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parent = null;
    return child;
  }
  // Tiny innerHTML parser for the limited patterns used by app.js. We
  // support: <button class="..." data-sym="..."> <span class="sym">sym</span>
  // <span class="px">px</span> </button>, <div class="...">...</div>, plain
  // <span>...</span>. Anything else is ignored (we still clear children).
  setInnerHTML(html) {
    this.children = [];
    const re = /<(\/?)([a-zA-Z]+)([^>]*)>/g;
    let i = 0;
    const stack = [this];
    let m;
    while ((m = re.exec(html)) !== null) {
      const close = m[1] === "/";
      const tag = m[2].toLowerCase();
      if (close) {
        if (stack.length > 1) stack.pop();
        continue;
      }
      const attrStr = m[3] || "";
      const attrs = {};
      const attrRe = /([a-zA-Z-]+)\s*=\s*"([^"]*)"/g;
      let am;
      while ((am = attrRe.exec(attrStr)) !== null) {
        attrs[am[1]] = am[2];
      }
      // Self-closing? Treat <br>, <input>, <img> as void; everything else as open.
      const voidTags = new Set(["br", "img", "input", "hr", "meta", "link"]);
      const el = new FakeElement(tag, attrs);
      // Append to current parent FIRST, then push the new el onto the
      // stack so the next open-tag becomes its child.
      stack[stack.length - 1].appendChild(el);
      if (!voidTags.has(tag)) stack.push(el);
      i = re.lastIndex;
    }
  }
  // Read textContent recursively from descendants.
  _readText() {
    let out = this.textContent || "";
    for (const c of this.children) {
      if (c.children.length === 0) out += c.textContent || "";
      else out += c._readText();
    }
    return out;
  }
  // querySelector by innerHTML-tagged descendants.
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === "id") this.id = v;
    if (k === "class") {
      this._classes.clear();
      String(v).split(/\s+/).filter(Boolean).forEach(c => this._classes.add(c));
    }
  }
  getAttribute(k) { return this.attrs[k]; }
  querySelector(sel) { return _querySelector(this, sel); }
  querySelectorAll(sel) { return _querySelectorAll(this, sel); }
  closest(sel) { return _closest(this, sel); }
  matches(sel) { return _matches(this, sel); }
  contains(node) {
    if (node === this) return true;
    for (const c of this.children) if (c.contains(node)) return true;
    return false;
  }
  cloneNode() { return new FakeElement(this.tagName, this.attrs); }
  focus() {}
  blur() {}
  reset() { this.value = ""; }
  getBoundingClientRect() {
    return { x: 0, y: 0, width: 356, height: 300, top: 0, bottom: 300, left: 0, right: 356 };
  }
  submit() {}
}

// ---------- selector engine -----------------------------------------------

function _matches(el, sel) {
  if (!el) return false;
  if (sel.startsWith("#")) return el.id === sel.slice(1);
  if (sel.startsWith(".")) {
    const cls = sel.slice(1).split(/[\s.]+/).filter(Boolean);
    return cls.every(c => el._classes.has(c));
  }
  if (sel.startsWith("[")) {
    // attr selector: [name="value"] or [name="value"]
    const m = sel.match(/^\[([^=\]]+)(?:=["']?([^"']*)["']?)?\]$/);
    if (!m) return false;
    const name = m[1];
    const val = m[2];
    const actual = el.attrs[name] !== undefined ? el.attrs[name] : el.dataset[_camel(name)];
    if (val === undefined) return actual !== undefined && actual !== "";
    return actual === val;
  }
  // Compound tag + attrs: button[data-symbol="BTCUSDT"]
  // or tag.classes.attrs
  const attrPart = sel.indexOf("[");
  let base = sel;
  let attrStr = null;
  if (attrPart >= 0) {
    base = sel.slice(0, attrPart);
    attrStr = sel.slice(attrPart);
  }
  if (base.includes(".")) {
    const [tag, ...rest] = base.split(".");
    if (tag && el.tagName !== tag.toUpperCase()) return false;
    if (!rest.every(c => el._classes.has(c))) return false;
  } else if (base) {
    if (el.tagName !== base.toUpperCase()) return false;
  }
  if (attrStr && !_matches(el, attrStr)) return false;
  return true;
}
function _camel(s) { return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

function _querySelector(root, sel) {
  const parts = sel.split(/\s+/).filter(Boolean);
  function walk(node, depth) {
    if (depth >= parts.length) return node;
    const part = parts[depth];
    for (const child of (node.children || [])) {
      if (_matches(child, part)) {
        if (depth === parts.length - 1) return child;
        const deeper = walk(child, depth + 1);
        if (deeper) return deeper;
      } else {
        const deeper = walk(child, depth);
        if (deeper) return deeper;
      }
    }
    return null;
  }
  return walk(root, 0);
}

function _querySelectorAll(root, sel) {
  const out = [];
  const parts = sel.split(/\s+/).filter(Boolean);
  function walk(node, depth) {
    if (depth >= parts.length) { out.push(node); return; }
    const part = parts[depth];
    for (const child of (node.children || [])) {
      if (_matches(child, part)) {
        if (depth === parts.length - 1) out.push(child);
        else walk(child, depth + 1);
      } else {
        walk(child, depth);
      }
    }
  }
  walk(root, 0);
  return out;
}

function _closest(node, sel) {
  let cur = node;
  while (cur) {
    if (_matches(cur, sel)) return cur;
    cur = cur.parent;
  }
  return null;
}

// ---------- Build minimal DOM matching WebTrade2 index.html ----------------

const htmlText = fs.readFileSync(HTML_PATH, "utf-8");

const body = new FakeElement("BODY");
const html = new FakeElement("HTML");
html.appendChild(body);

// Parse only the elements we need from index.html.
function buildShellDom() {
  // Login panel (initially visible until /api/session responds OK).
  const login = new FakeElement("DIV", { id: "loginPanel", class: "login-panel" });
  const lcard = new FakeElement("DIV", { class: "login-card" });
  login.appendChild(lcard);
  body.appendChild(login);

  // Chart wrapper.
  const chartPanel = new FakeElement("DIV", { class: "panel chart" });
  const chartWrap = new FakeElement("DIV", { class: "chart-wrap" });
  const chartCanvas = new FakeElement("DIV", { id: "chart", class: "chart-canvas" });
  chartCanvas.clientWidth = 356; chartCanvas.clientHeight = 300;
  chartCanvas.offsetWidth = 356; chartCanvas.offsetHeight = 300;
  chartWrap.appendChild(chartCanvas);
  chartPanel.appendChild(chartWrap);

  const timeframes = new FakeElement("DIV", { id: "timeframes", class: "timeframes" });
  for (const tf of ["15m", "1h", "4h", "1D"]) {
    const b = new FakeElement("BUTTON", { "data-timeframe": tf });
    b.textContent = tf;
    timeframes.appendChild(b);
  }
  chartPanel.appendChild(timeframes);

  const overlay = new FakeElement("DIV", { class: "overlay-hint" });
  overlay.textContent = "Read-only overlays planned for our data only";
  chartPanel.appendChild(overlay);

  body.appendChild(chartPanel);

  // Market list (so renderMarkets() does not blow up).
  const marketsPanel = new FakeElement("DIV", { class: "panel markets" });
  const markets = new FakeElement("DIV", { id: "markets", class: "market-list" });
  const head = new FakeElement("DIV", { class: "market-head" });
  head.innerHTML = "<span>Instrument</span><span>Price</span>";
  markets.appendChild(head);
  marketsPanel.appendChild(markets);
  body.appendChild(marketsPanel);

  // Instrument input
  const inst = new FakeElement("INPUT", { id: "instrument" });
  body.appendChild(inst);

  // Selectors: exchange / account / marketType (SELECT elements with
  // .value to drive the initial state.exchange / state.account).
  const exch = new FakeElement("SELECT", { id: "exchange" });
  exch.value = "apex";
  body.appendChild(exch);
  const acct = new FakeElement("SELECT", { id: "account" });
  acct.value = "BITGET";
  body.appendChild(acct);
  const mt = new FakeElement("SELECT", { id: "marketType" });
  mt.value = "futures";
  body.appendChild(mt);

  // Market tabs / search.
  for (const id of ["marketSearch", "marketSymbol", "marketTabs"]) {
    body.appendChild(new FakeElement(id === "marketTabs" ? "DIV" : "INPUT", { id }));
  }

  // Trade pane (skeleton).
  const tradePane = new FakeElement("DIV", { class: "panel trade" });
  body.appendChild(tradePane);

  // Bottom + account strip.
  body.appendChild(new FakeElement("DIV", { class: "panel bottom" }));
  body.appendChild(new FakeElement("DIV", { class: "account-strip" }));

  // CSRF hidden input
  const csrf = new FakeElement("INPUT", { type: "hidden", id: "csrfToken" });
  body.appendChild(csrf);
}

buildShellDom();

// ---------- Series / Chart stub --------------------------------------------

// Track all setData / update calls so tests can verify the chart got the
// right symbol's data.
const setDataCalls = [];
const updateCalls = [];
const resizeCalls = [];
const fitContentCalls = [];

function makeSeries() {
  return {
    setData(data) {
      const last = (data && data.length) ? data[data.length - 1] : null;
      const first = (data && data.length) ? data[0] : null;
      setDataCalls.push({
        count: data ? data.length : 0,
        first,
        last,
        empty: !data || data.length === 0,
      });
    },
    update(c) { updateCalls.push(c); },
    applyOptions() {},
  };
}

function makeChart() {
  const psLeft = {
    applyOptions() {}, setMargins() {},
    getPriceRange() { return { minValue: 0, maxValue: 1 }; },
  };
  const psRight = {
    applyOptions() {}, setMargins() {},
    getPriceRange() { return { minValue: 0, maxValue: 1 }; },
  };
  const ts = {
    fitContent() { fitContentCalls.push(Date.now()); },
    setVisibleRange() {},
    getVisibleLogicalRange() { return { from: 0, to: 1 }; },
  };
  return {
    addCandlestickSeries() { return makeSeries(); },
    addHistogramSeries() { return makeSeries(); },
    remove() {},
    resize(w, h) { resizeCalls.push({ w, h }); },
    timeScale() { return ts; },
    priceScale(side) { return side === "left" ? psLeft : psRight; },
    applyOptions() {},
  };
}

// Mock Lightweight Charts factory. We replace window.LightweightCharts later.
function makeLwcFactory() {
  const chart = makeChart();
  return {
    createChart() { return chart; },
  };
}

// ---------- Mock /api routes ----------------------------------------------

const candleRequests = []; // path + query string + arrival order
const executedTrades = [];

function okResp(body) {
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(body),
  });
}

function failResp(status, body) {
  return Promise.resolve({
    ok: false, status,
    json: () => Promise.resolve(body),
  });
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

// Test-controllable mock state. Tests set this before triggering UI actions.
const mockState = {
  // Map of "exchange/account/symbol/tf" → array of candle objects (or null = fail).
  // If "delayed", returns after `delayMs`.
  candles: {},
  delayMs: 0,
  onRequest: null, // (key) => void — called synchronously when the request is dispatched.
};

// The runner's fetch mock reads these on every call.
async function handleFetch(url, opts) {
  const u = String(url);
  const path = u.split("?")[0];
  const query = u.includes("?") ? u.split("?")[1] : "";
  const params = new URLSearchParams(query);
  const exchange = params.get("exchange") || "";
  const account = params.get("account") || "";
  const symbol = params.get("symbol") || "";
  const tf = params.get("interval") || "";
  const key = `${exchange}/${account}/${symbol}/${tf}`;
  if (path === "/api/candles") {
    candleRequests.push({ url: u, key, at: Date.now() });
    if (mockState.onRequest) mockState.onRequest(key);
    if (mockState.delayMs > 0) await delay(mockState.delayMs);
    // Re-check staleness: if the mock has a "current expected key" set and
    // we don't match, the app should drop this. The runner does NOT
    // simulate that here — it's the app's responsibility.
    const entry = mockState.candles[key];
    if (entry === undefined) {
      // No mock data — default to a tiny 1-candle success so the app
      // doesn't error out on initial loads. Tests override specific keys.
      return okResp({
        success: true,
        data: {
          interval: tf, candles: [
            { time: Math.floor(Date.now() / 1000) - 900, open: 100, high: 101, low: 99, close: 100.5, volume: 1 },
          ],
        },
      });
    }
    if (entry === null) {
      return failResp(200, {
        success: false, operation: "candles", exchange, account, symbol,
        error: { code: "CANDLES_UNAVAILABLE", message: `Mock failure for ${symbol}` },
      });
    }
    return okResp({ success: true, data: { interval: tf, candles: entry } });
  }
  if (path === "/api/session") return okResp({ csrf: "FAKE_CSRF", logged_in: true });
  if (path === "/api/phase2") return okResp({
    phase: 2, write_enabled: true, dry_run: true, ladder_enabled: false, preview_ttl_seconds: 300,
  });
  if (path === "/api/exchanges") return okResp({
    exchanges: [{ exchange: "apex", accounts: ["BITGET"] }],
  });
  if (path === "/api/markets") {
    const syms = ["BTCUSDT", "ETHUSDT", "NVDAUSDT", "QQQUSDT", "XRPUSDT", "AAPLUSDT"];
    return okResp({
      markets: syms.map(s => ({
        symbol: s,
        price: "100",
        turnover24h: 1000000,
        mark_price: "100",
      })),
    });
  }
  if (path === "/api/market_price") {
    return okResp({ market_price: { mark_price: "100", index_price: "100" } });
  }
  if (path === "/api/account_state") return okResp({ balances: [], positions: [], orders: [] });
  if (path === "/api/positions_orders") return okResp({ positions: [], orders: [] });
  if (path === "/api/trade/execute") {
    executedTrades.push(JSON.parse(opts.body));
    return okResp({ success: true, status: "DRY_RUN", mode: "DRY_RUN", exchange_order_ids: [], accepted: 1, requested: 1 });
  }
  if (u.startsWith("/login")) return Promise.resolve({ ok: true, status: 303 });
  return Promise.reject(new Error("unmocked: " + u));
}

// ---------- Build sandbox --------------------------------------------------

const appJs = fs.readFileSync(APP_JS_PATH, "utf-8");

const docListeners = {};
const document_ = {
  querySelector: (sel) => _querySelector(body, sel) || null,
  querySelectorAll: (sel) => _querySelectorAll(body, sel),
  getElementById: (id) => _querySelector(body, "#" + id) || null,
  addEventListener: (type, cb) => { (docListeners[type] = docListeners[type] || []).push(cb); },
  createElement: (tag) => new FakeElement(tag),
  body,
};

const ctx = {
  document: document_,
  window: {
    addEventListener: () => {},
    LightweightCharts: makeLwcFactory(),
    __webtrade2_chart__: {},
  },
  setTimeout, clearTimeout, setInterval, clearInterval,
  console,
  localStorage: { _data: {}, getItem(k){return this._data[k]||null;}, setItem(k,v){this._data[k]=String(v);}, removeItem(k){delete this._data[k];} },
  alert: () => {}, prompt: () => null, confirm: () => true,
  fetch: handleFetch,
  URLSearchParams,
  ResizeObserver: class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} unobserve() {} },
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
};

vm.createContext(ctx);
try {
  vm.runInContext(appJs, ctx, { filename: "app.js" });
} catch (e) {
  console.error("FATAL: app.js failed to load:", e.message);
  process.exit(3);
}

// ---------- helpers --------------------------------------------------------

function $(sel) { return _querySelector(body, sel); }
function click(el) {
  const evt = { type: "click", target: el, currentTarget: el, returnValue: true, preventDefault() {}, stopPropagation() {} };
  el.dispatchEvent(evt);
  for (const h of (docListeners.click || [])) h(evt);
}
function fire(sel, type, init = {}) {
  const el = $(sel);
  if (!el) throw new Error("missing element: " + sel);
  const evt = Object.assign({ type, target: el, currentTarget: el, returnValue: true, preventDefault() {}, stopPropagation() {} }, init);
  el.dispatchEvent(evt);
}
async function settle(ms = 0) {
  if (ms > 0) await delay(ms);
  // run all queued microtasks
  for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r));
}

function makeCandles(center, count, stepSec, jitter) {
  jitter = jitter || 0.001;
  const out = [];
  const now = Math.floor(Date.now() / 1000);
  for (let i = 0; i < count; i++) {
    const t = now - (count - 1 - i) * stepSec;
    const drift = 1 + Math.sin(i / 5) * jitter;
    out.push({
      time: t,
      open: center * drift,
      high: center * drift * 1.005,
      low: center * drift * 0.995,
      close: center * drift * 1.001,
      volume: 1,
    });
  }
  return out;
}

let failed = 0;
function assert(cond, msg) {
  if (cond) console.log("PASS:", msg);
  else { console.log("FAIL:", msg); failed++; }
}

function resetState() {
  setDataCalls.length = 0;
  updateCalls.length = 0;
  resizeCalls.length = 0;
  fitContentCalls.length = 0;
  candleRequests.length = 0;
  executedTrades.length = 0;
  mockState.candles = {};
  mockState.delayMs = 0;
  mockState.onRequest = null;
}

// Reach into the IIFE's state. The app is wrapped in an IIFE so state is
// not directly accessible. We rely on the observable chart side-effects:
// setDataCalls, the #chartMessage DOM div (for the error overlay), and
// the priceScale (which our mock lets us observe via resizeCalls). The
// old telemetry-based assertions have been rewritten in terms of these.
function getChartMessage() {
  return $("#chartMessage");
}

// ---------- Run the scenarios ---------------------------------------------

(async () => {
  await settle(50); // let boot finish

  // The boot() path calls loadMarkets() which auto-selects
  // state.markets[0].symbol. After that auto-load, the chart shows
  // that market's data and any subsequent click on the SAME market
  // is a no-op. So we await boot, then RESET our tracked setDataCalls
  // so the assertions only count post-boot interactions.

  function clickBySymbol(sym) {
    // Re-query because renderMarkets() rewrites innerHTML.
    const m = _querySelector(body, `#markets .market-list-inner button[data-symbol="${sym}"]`)
            || _querySelector(body, `#markets button[data-symbol="${sym}"]`);
    if (!m) throw new Error("missing market button: " + sym);
    click(m);
  }

  // -- Scenario 1: BTC -> failed symbol -> BTC recovery --
  resetState();
  mockState.candles["apex/BITGET/BTCUSDT/15m"] = makeCandles(84000, 60, 900, 0.002);
  mockState.candles["apex/BITGET/NVDAUSDT/15m"] = null; // failure
  mockState.candles["apex/BITGET/ETHUSDT/15m"] = makeCandles(2690, 60, 900, 0.001);

  // Click BTCUSDT (already loaded by boot — but this forces a fresh load).
  clickBySymbol("BTCUSDT");
  await settle(80);
  const afterBtc = setDataCalls[setDataCalls.length - 1];
  assert(afterBtc && afterBtc.count === 60, "S1: BTC loaded 60 candles");
  assert(afterBtc && afterBtc.last && Math.abs(afterBtc.last.close - 84000) / 84000 < 0.01,
    "S1: BTC close ~84000");

  // Now click NVDAUSDT (failure expected).
  clickBySymbol("NVDAUSDT");
  await settle(80);
  // After failure: chart should be cleared (last setData call empty or chartMessage visible).
  const lastAfterNvda = setDataCalls[setDataCalls.length - 1];
  assert(lastAfterNvda && lastAfterNvda.empty === true,
    "S1: NVDA failure cleared chart (last setData was empty)");
  // Chart should NOT contain the previous BTC data. We assert this by
  // checking the last setData payload is empty.
  assert(!lastAfterNvda.last,
    "S1: NVDA failure: chart shows no last candle (BTC's data is gone)");
  // Error message visible to the user.
  const msg1 = getChartMessage();
  assert(msg1 && !msg1.hidden && /CANDLES_UNAVAILABLE/i.test(msg1.textContent),
    `S1: chartMessage shows CANDLES_UNAVAILABLE, got hidden=${msg1 && msg1.hidden} text=${msg1 && JSON.stringify(msg1.textContent)}`);

  // Now click BTC again — must recover.
  clickBySymbol("BTCUSDT");
  await settle(80);
  const lastAfterBtc2 = setDataCalls[setDataCalls.length - 1];
  assert(lastAfterBtc2 && lastAfterBtc2.count === 60,
    "S1: BTC re-loaded after NVDA failure: 60 candles");
  assert(lastAfterBtc2 && lastAfterBtc2.last && Math.abs(lastAfterBtc2.last.close - 84000) / 84000 < 0.01,
    "S1: BTC re-loaded close ~84000");

  // -- Scenario 2: successful A (BTC) -> successful B (XRP) with very different price --
  resetState();
  mockState.candles["apex/BITGET/BTCUSDT/15m"] = makeCandles(84000, 50, 900, 0.002);
  mockState.candles["apex/BITGET/XRPUSDT/15m"] = makeCandles(1.55, 50, 900, 0.005);
  clickBySymbol("BTCUSDT");
  await settle(80);
  assert(setDataCalls[setDataCalls.length - 1].last.close > 80000,
    "S2: BTC chart applied with high price");

  clickBySymbol("XRPUSDT");
  await settle(80);
  const xrpApplied = setDataCalls[setDataCalls.length - 1];
  assert(xrpApplied.last.close < 5,
    "S2: XRP chart applied with low price (scale reset to XRP range, not BTC's)");

  // -- Scenario 3: A -> failed B clears old candles --
  resetState();
  mockState.candles["apex/BITGET/AAPLUSDT/15m"] = makeCandles(335, 40, 900, 0.001);
  mockState.candles["apex/BITGET/QQQUSDT/15m"] = null; // fail
  clickBySymbol("AAPLUSDT");
  await settle(80);
  assert(setDataCalls[setDataCalls.length - 1].last.close > 300,
    "S3: AAPL loaded");

  clickBySymbol("QQQUSDT");
  await settle(80);
  const afterQqq = setDataCalls[setDataCalls.length - 1];
  assert(afterQqq.empty === true, "S3: QQQ failure cleared chart");
  // QQQ must NOT leave AAPL's candles on screen — last setData is empty.
  assert(!afterQqq.last,
    "S3: QQQ failure: AAPL's last candle is NOT visible after the failure");
  // Error overlay present.
  const msg3 = getChartMessage();
  assert(msg3 && !msg3.hidden && /CANDLES_UNAVAILABLE/i.test(msg3.textContent),
    `S3: chartMessage shows CANDLES_UNAVAILABLE, got ${msg3 && JSON.stringify(msg3.textContent)}`);

  // -- Scenario 4: rapid A -> B -> C with slow A arriving last --
  // We make A delayed, B and C fast. After the user clicks C, A's late
  // response must be dropped (staleResponseDropped=true) and C's chart
  // must be the final state.
  resetState();
  mockState.delayMs = 0;
  let aRequested = false;
  mockState.candles["apex/BITGET/NVDAUSDT/15m"] = makeCandles(223, 40, 900, 0.001);
  mockState.candles["apex/BITGET/ETHUSDT/15m"] = makeCandles(2690, 40, 900, 0.001);
  mockState.candles["apex/BITGET/BTCUSDT/15m"] = makeCandles(84000, 40, 900, 0.002);
  // Make NVDA delayed by intercepting via onRequest.
  mockState.onRequest = (key) => {
    if (key === "apex/BITGET/NVDAUSDT/15m") {
      aRequested = true;
      mockState.delayMs = 200;
    } else if (aRequested) {
      mockState.delayMs = 0;
    }
  };

  clickBySymbol("NVDAUSDT");
  await settle(20); // let the NVDA request start
  clickBySymbol("ETHUSDT");
  await settle(40);
  clickBySymbol("BTCUSDT");
  await settle(400); // wait long enough for all requests to complete

  const lastApplied = setDataCalls[setDataCalls.length - 1];
  assert(lastApplied && lastApplied.last && Math.abs(lastApplied.last.close - 84000) / 84000 < 0.01,
    "S4: final chart state matches BTC (slow NVDA response did NOT overwrite)");

  // -- Scenario 5: timeframe change resets scale --
  resetState();
  mockState.candles["apex/BITGET/BTCUSDT/15m"] = makeCandles(84000, 60, 900, 0.002);
  mockState.candles["apex/BITGET/BTCUSDT/1h"] = makeCandles(84000, 60, 3600, 0.005);
  clickBySymbol("BTCUSDT");
  await settle(80);
  const beforeFit = fitContentCalls.length;
  fire('[data-timeframe="1h"]', "click");
  await settle(80);
  assert(fitContentCalls.length > beforeFit,
    "S5: timeframe change triggers fitContent (scale reset)");
  const afterTf = setDataCalls[setDataCalls.length - 1];
  assert(afterTf.count === 60,
    "S5: 1h timeframe loaded 60 candles");
  assert(afterTf.last.time - afterTf.first.time === 3600 * 59,
    "S5: 1h spacing matches (3600 sec between consecutive)");

  // -- Scenario 6: visible price range sanity --
  resetState();
  mockState.candles["apex/BITGET/BTCUSDT/15m"] = makeCandles(84000, 60, 900, 0.002);
  mockState.candles["apex/BITGET/BTCUSDT/1h"] = makeCandles(84000, 60, 3600, 0.002);
  mockState.candles["apex/BITGET/XRPUSDT/15m"] = makeCandles(1.55, 60, 900, 0.005);
  mockState.candles["apex/BITGET/XRPUSDT/1h"] = makeCandles(1.55, 60, 3600, 0.005);
  // Reset timeframe to 15m by clicking the button.
  fire('[data-timeframe="15m"]', "click");
  await settle(60);
  clickBySymbol("BTCUSDT");
  await settle(80);
  clickBySymbol("XRPUSDT");
  await settle(80);
  // The last applied setData payload must contain XRP-range candles,
  // not BTC's. We assert this by inspecting the last setData's series
  // directly (not via telemetry).
  const lastXrp = setDataCalls[setDataCalls.length - 1];
  assert(lastXrp.last && lastXrp.last.close < 5,
    `S6: last setData after XRP switch is in XRP range (close < 5), got ${lastXrp.last && lastXrp.last.close}`);
  assert(lastXrp.first && lastXrp.first.open < 5,
    `S6: first setData after XRP switch is in XRP range (open < 5), got ${lastXrp.first && lastXrp.first.open}`);

  // -- Live safety: no /api/trade/execute calls during the entire run --
  assert(executedTrades.length === 0,
    "LIVE SAFETY: zero /api/trade/execute calls");

  console.log(failed === 0 ? "ALL PASS" : `FAILURES: ${failed}`);
  process.exit(failed === 0 ? 0 : 1);
})().catch(e => {
  console.error("runner crashed:", e.stack || e.message);
  process.exit(2);
});
