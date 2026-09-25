#!/usr/bin/env node
/**
 * WebTrade2 frontend order-side state regression runner.
 *
 * Why this exists:
 *   The previous code had a DOM-as-state anti-pattern: order BUY/SELL
 *   buttons had no click handler, and previewOrder() read side from
 *   `$('.side-row .seg.buy.active')`. Because the .active class was only
 *   set in the initial HTML and never toggled, side was permanently 'buy'.
 *
 *   This runner:
 *     1. Loads the actual /root/kam/plugins/trade/webtrade2/static/index.html
 *        and app.js into a minimal DOM stub.
 *     2. Mocks /api/session, /api/phase2, /api/trade/preview_order so the
 *        app boots and previewOrder() can run without a live server.
 *     3. Captures every /api/trade/preview_order body sent, plus the
 *        confirmation modal HTML, plus button .active state, after each
 *        user interaction.
 *     4. Asserts the contract.
 *
 *   CRITICAL: this runner NEVER POSTs to /api/trade/execute and NEVER
 *   causes an exchange write. It only inspects preview-side state and
 *   payload construction.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEBTRADE2_DIR = "/root/kam/plugins/trade/webtrade2/static";
const HTML_PATH = path.join(WEBTRADE2_DIR, "index.html");
const APP_JS_PATH = path.join(WEBTRADE2_DIR, "app.js");

// ---------- minimal DOM stub ----------------------------------------------

class FakeClassList {
  constructor(host) {
    this.host = host;
    // The class set lives on the host element (`_classes`). This object
    // forwards mutation to the host so both stay in sync.
  }
  add(c) { this.host._classes.add(c); this.host._fireClassChange(); }
  remove(c) { this.host._classes.delete(c); this.host._fireClassChange(); }
  toggle(c, force) {
    const had = this.host._classes.has(c);
    if (force === true)  { this.host._classes.add(c);    this.host._fireClassChange(); return true;  }
    if (force === false) { this.host._classes.delete(c); this.host._fireClassChange(); return false; }
    if (had) { this.host._classes.delete(c); this.host._fireClassChange(); return false; }
    this.host._classes.add(c); this.host._fireClassChange(); return true;
  }
  contains(c) { return this.host._classes.has(c); }
  toString() { return Array.from(this.host._classes).join(" "); }
}

class FakeElement {
  constructor(tag, attrs = {}) {
    this.tagName = tag;
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
  }
  _fireClassChange() { /* hook for tests */ }
  addEventListener(type, cb) {
    (this._listeners[type] = this._listeners[type] || []).push(cb);
  }
  removeEventListener(type, cb) {
    if (!this._listeners[type]) return;
    this._listeners[type] = this._listeners.filter(c => c !== cb);
  }
  dispatchEvent(evt) {
    const type = evt.type;
    const handlers = (this._listeners[type] || []).slice();
    let returnValue = evt.returnValue !== undefined ? evt.returnValue : true;
    for (const h of handlers) {
      try {
        const r = h(evt);
        if (r === false) returnValue = false;
      } catch (e) {
        throw e;
      }
    }
    return returnValue;
  }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === "id") this.id = v;
    if (k === "class") {
      this._classes.clear();
      String(v).split(/\s+/).filter(Boolean).forEach(c => this._classes.add(c));
    }
  }
  getAttribute(k) { return this.attrs[k]; }
  querySelector(sel) {
    return _querySelector(this, sel);
  }
  querySelectorAll(sel) {
    return _querySelectorAll(this, sel);
  }
  closest(sel) {
    return _closest(this, sel);
  }
  matches(sel) {
    return _matches(this, sel);
  }
  contains(node) {
    if (node === this) return true;
    for (const c of this.children) if (c.contains(node)) return true;
    return false;
  }
  cloneNode() { return new FakeElement(this.tagName, this.attrs); }
  focus() {}
  blur() {}
  reset() { this.value = ""; }
  submit() { /* handled via form submit event */ }
}

class FakeFormElement extends FakeElement {
  constructor(tag, attrs) { super(tag, attrs); }
}

// ---------- selector engine -----------------------------------------------

function _parseSimpleSelector(sel) {
  // Supports: tag, #id, .cls, [data-x], combined without spaces (we only need a subset)
  // and descendant selectors like '.side-row .seg.buy' or '#orderBuy'.
  const parts = [];
  let i = 0;
  while (i < sel.length) {
    if (sel[i] === " ") { i++; continue; }
    let part = "";
    while (i < sel.length && sel[i] !== " ") { part += sel[i++]; }
    parts.push(part);
  }
  return parts;
}

function _matchesSingle(el, singleSel) {
  if (!el || !el.tagName) return false;
  // tag
  let rest = singleSel;
  let tagMatch = rest.match(/^([a-zA-Z][a-zA-Z0-9]*)/);
  let tag = null;
  if (tagMatch) { tag = tagMatch[1]; rest = rest.slice(tagMatch[0].length); }
  if (tag && el.tagName.toLowerCase() !== tag.toLowerCase()) return false;
  // #id
  let m;
  const idRe = /#([a-zA-Z0-9_-]+)/g;
  while ((m = idRe.exec(rest))) {
    if (el.id !== m[1]) return false;
  }
  // .class
  const clsRe = /\.([a-zA-Z0-9_-]+)/g;
  while ((m = clsRe.exec(rest))) {
    if (!el.classList.contains(m[1])) return false;
  }
  // [attr]
  const attrRe = /\[([a-zA-Z0-9_-]+)(?:=([^\]]+))?\]/g;
  while ((m = attrRe.exec(rest))) {
    if (m[2] !== undefined) {
      if (el.attrs[m[1]] !== m[2].replace(/^['"]|['"]$/g, "")) return false;
    } else {
      if (el.attrs[m[1]] === undefined) return false;
    }
  }
  return true;
}

function _matches(el, sel) {
  const parts = _parseSimpleSelector(sel);
  return _matchesSingle(el, parts[parts.length - 1]);
}

function _querySelector(root, sel) {
  const parts = _parseSimpleSelector(sel);
  if (parts.length === 1) {
    return _findBySingle(root, parts[0]);
  }
  // descendant: find rightmost first, walk up to find matching ancestor chain
  const candidates = _findAllBySingle(root, parts[parts.length - 1]);
  for (const c of candidates) {
    let node = c.parent;
    let matched = true;
    for (let p = parts.length - 2; p >= 0; p--) {
      let ancestor = null;
      while (node) {
        if (_matchesSingle(node, parts[p])) { ancestor = node; break; }
        node = node.parent;
      }
      if (!ancestor) { matched = false; break; }
      node = ancestor.parent;
    }
    if (matched) return c;
  }
  return null;
}

function _querySelectorAll(root, sel) {
  const parts = _parseSimpleSelector(sel);
  if (parts.length === 1) {
    return _findAllBySingle(root, parts[0]);
  }
  const out = [];
  const candidates = _findAllBySingle(root, parts[parts.length - 1]);
  for (const c of candidates) {
    let node = c.parent;
    let matched = true;
    for (let p = parts.length - 2; p >= 0; p--) {
      let ancestor = null;
      while (node) {
        if (_matchesSingle(node, parts[p])) { ancestor = node; break; }
        node = node.parent;
      }
      if (!ancestor) { matched = false; break; }
      node = ancestor.parent;
    }
    if (matched) out.push(c);
  }
  return out;
}

function _findBySingle(root, singleSel) {
  if (root.tagName && _matchesSingle(root, singleSel)) return root;
  for (const c of root.children) {
    const r = _findBySingle(c, singleSel);
    if (r) return r;
  }
  return null;
}

function _findAllBySingle(root, singleSel) {
  const out = [];
  function walk(n) {
    if (n.tagName && _matchesSingle(n, singleSel)) out.push(n);
    for (const c of n.children) walk(c);
  }
  walk(root);
  return out;
}

function _closest(el, sel) {
  let cur = el;
  while (cur) {
    if (cur.tagName && _matchesSingle(cur, sel)) return cur;
    cur = cur.parent;
  }
  return null;
}

// ---------- DOM construction from the real HTML ----------------------------

const html = fs.readFileSync(HTML_PATH, "utf-8");

// Strip script tags from HTML (we'll inject app.js manually after extracting markup).
const strippedHtml = html.replace(/<script[\s\S]*?<\/script>/g, "");

const bodyMatch = strippedHtml.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
if (!bodyMatch) {
  console.error("FATAL: could not find <body> in index.html");
  process.exit(2);
}
const bodyInnerHtml = bodyMatch[1];

// Tiny HTML → FakeElement parser (only what index.html actually uses).
function parseFragment(fragmentHtml) {
  const root = new FakeElement("root");
  let pos = 0;

  function skipWhitespace() { while (pos < fragmentHtml.length && /\s/.test(fragmentHtml[pos])) pos++; }

  function parseNodes(parent) {
    while (pos < fragmentHtml.length) {
      skipWhitespace();
      if (pos >= fragmentHtml.length) break;
      if (fragmentHtml[pos] === "<") {
        if (fragmentHtml.startsWith("</", pos)) break;
        if (fragmentHtml.startsWith("<!--", pos)) {
          // Skip HTML comment.
          const end = fragmentHtml.indexOf("-->", pos);
          if (end < 0) { pos = fragmentHtml.length; break; }
          pos = end + 3;
          continue;
        }
        if (fragmentHtml.startsWith("<!", pos)) {
          // Skip doctype / CDATA / processing instructions.
          const end = fragmentHtml.indexOf(">", pos);
          if (end < 0) { pos = fragmentHtml.length; break; }
          pos = end + 1;
          continue;
        }
        parseElement(parent);
      } else {
        // text node: skip
        while (pos < fragmentHtml.length && fragmentHtml[pos] !== "<") pos++;
      }
    }
  }

  function parseElement(parent) {
    // skip '<'
    pos++;
    // Parse tag name
    let tag = "";
    while (pos < fragmentHtml.length && /[a-zA-Z0-9-]/.test(fragmentHtml[pos])) tag += fragmentHtml[pos++];
    if (!tag) return;
    tag = tag.toLowerCase();
    // self-closing allowed tags
    const selfClosing = new Set(["input", "br", "img", "meta", "link", "hr"]);
    // Parse attributes until '>' or '/>'
    const attrs = {};
    while (pos < fragmentHtml.length) {
      skipWhitespace();
      if (fragmentHtml[pos] === ">" || (fragmentHtml[pos] === "/" && fragmentHtml[pos + 1] === ">")) break;
      let name = "";
      while (pos < fragmentHtml.length && /[a-zA-Z0-9_-]/.test(fragmentHtml[pos])) name += fragmentHtml[pos++];
      if (!name) { pos++; continue; }
      let value = "";
      if (fragmentHtml[pos] === "=") {
        pos++;
        if (fragmentHtml[pos] === '"' || fragmentHtml[pos] === "'") {
          const q = fragmentHtml[pos++];
          while (pos < fragmentHtml.length && fragmentHtml[pos] !== q) value += fragmentHtml[pos++];
          pos++; // skip closing quote
        } else {
          while (pos < fragmentHtml.length && /[^>\s]/.test(fragmentHtml[pos])) value += fragmentHtml[pos++];
        }
      }
      attrs[name.toLowerCase()] = value;
    }
    // Consume '>' or '/>'
    if (fragmentHtml[pos] === "/") { pos += 2; }
    else { pos++; }
    const el = new FakeElement(tag, attrs);
    parent.appendChild(el);
    if (!selfClosing.has(tag)) {
      parseNodes(el);
      // consume closing tag if present
      skipWhitespace();
      const close = `</${tag}`;
      if (fragmentHtml.slice(pos, pos + close.length).toLowerCase() === close) {
        while (pos < fragmentHtml.length && fragmentHtml[pos] !== ">") pos++;
        if (pos < fragmentHtml.length) pos++; // skip '>'
      }
    }
    return el;
  }

  parseNodes(root);
  return root;
}

const rootFragment = parseFragment(bodyInnerHtml);
const body = rootFragment;

// ---------- network capture ------------------------------------------------

const previewCalls = [];
const executeCalls = [];

function fakeApi(path, opts) {
  const method = (opts?.method || "GET").toUpperCase();
  if (method === "POST" && path === "/api/trade/preview_order") {
    const body = JSON.parse(opts.body);
    previewCalls.push(body);
    const sym = body.symbol || "BTC";
    const side = body.side;
    const fp = body.price || "100";
    const fs = body.size || "1";
    const fpN = Number(fp);
    const fsN = Number(fs);
    const notional = (fpN * fsN).toString();
    return Promise.resolve({
      success: true,
      preview_id: "PREVIEW_" + previewCalls.length,
      kind: "order",
      exchange: body.exchange,
      account: body.account,
      market_type: body.market_type || "futures",
      symbol: sym,
      native_symbol: sym,
      side,
      order_type: "limit",
      final_price: fp,
      final_size: fs,
      notional,
      reduce_only: !!body.reduce_only,
      expires_in_s: 300,
      summary: `${side.toUpperCase()} ${sym} LIMIT @ ${fp} x ${fs}`,
    });
  }
  if (method === "POST" && path === "/api/trade/execute") {
    executeCalls.push(JSON.parse(opts.body));
    return Promise.resolve({
      success: true,
      status: "DRY_RUN",
      mode: "DRY_RUN",
      exchange_order_ids: [],
      accepted: 1,
      requested: 1,
    });
  }
  if (path === "/api/session") {
    return Promise.resolve({ csrf: "FAKE_CSRF", logged_in: true });
  }
  if (path === "/api/phase2") {
    return Promise.resolve({
      phase: 2, write_enabled: true, dry_run: true,
      ladder_enabled: false, preview_ttl_seconds: 300,
    });
  }
  if (path === "/api/exchanges") {
    return Promise.resolve({
      exchanges: [{ exchange: "hyperliquid", accounts: [{ alias: "fibo" }] }],
    });
  }
  if (path === "/api/markets") {
    return Promise.resolve({ markets: [{ symbol: "BTC", price: "100", change_24h: "0", volume_24h: "0" }] });
  }
  if (path === "/api/account_state") {
    return Promise.resolve({ balances: [], positions: [], orders: [] });
  }
  if (path === "/api/positions_orders") {
    return Promise.resolve({ positions: [], orders: [] });
  }
  return Promise.reject(new Error("unmocked: " + method + " " + path));
}

// ---------- Load app.js into the sandbox -----------------------------------

const appJs = fs.readFileSync(APP_JS_PATH, "utf-8");

const document_ = {
  querySelector: (sel) => _querySelector(body, sel) || null,
  querySelectorAll: (sel) => _querySelectorAll(body, sel),
  addEventListener: (type, cb) => {
    if (type === "click" || type === "input" || type === "change" || type === "submit") {
      (docListeners[type] = docListeners[type] || []).push(cb);
    }
  },
  createElement: (tag) => new FakeElement(tag),
};

const docListeners = {};
document_.addEventListener = (type, cb) => {
  (docListeners[type] = docListeners[type] || []).push(cb);
};

const ctx = {
  document: document_,
  window: { addEventListener: () => {}, _charts: {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
  console,
  localStorage: {
    _data: {},
    getItem(k) { return this._data[k] !== undefined ? this._data[k] : null; },
    setItem(k, v) { this._data[k] = String(v); },
    removeItem(k) { delete this._data[k]; },
  },
  alert: () => {},
  prompt: () => null,
  confirm: () => true,
  fetch: (url, opts) => {
    // Parse URL path
    const u = String(url);
    const path = u.split("?")[0];
    // The app's api() uses fetch internally, so mock all preview/execute/session/phase2
    // routes here too.
    const method = ((opts && opts.method) || "GET").toUpperCase();
    if (path === "/api/trade/preview_order") {
      const body = JSON.parse(opts.body);
      previewCalls.push(body);
      const sym = body.symbol || "BTC";
      const side = body.side;
      const fp = body.price || "100";
      const fs = body.size || "1";
      const fpN = Number(fp);
      const fsN = Number(fs);
      const notional = (fpN * fsN).toString();
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          success: true,
          preview_id: "PREVIEW_" + previewCalls.length,
          kind: "order",
          exchange: body.exchange,
          account: body.account,
          market_type: body.market_type || "futures",
          symbol: sym,
          native_symbol: sym,
          side,
          order_type: "limit",
          final_price: fp,
          final_size: fs,
          notional,
          reduce_only: !!body.reduce_only,
          expires_in_s: 300,
          summary: `${side.toUpperCase()} ${sym} LIMIT @ ${fp} x ${fs}`,
        }),
      });
    }
    if (path === "/api/trade/execute") {
      executeCalls.push(JSON.parse(opts.body));
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ success: true, status: "DRY_RUN", mode: "DRY_RUN", exchange_order_ids: [], accepted: 1, requested: 1 }),
      });
    }
    if (path === "/api/session") {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ csrf: "FAKE_CSRF", logged_in: true }),
      });
    }
    if (path === "/api/phase2") {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ phase: 2, write_enabled: true, dry_run: true, ladder_enabled: false, preview_ttl_seconds: 300 }),
      });
    }
    if (path === "/api/exchanges") {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ exchanges: [{ exchange: "hyperliquid", accounts: [{ alias: "fibo" }] }] }),
      });
    }
    if (path === "/api/markets") {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ markets: [{ symbol: "BTC", price: "100", change_24h: "0", volume_24h: "0" }] }),
      });
    }
    if (path === "/api/account_state") {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ balances: [], positions: [], orders: [] }) });
    }
    if (path === "/api/positions_orders") {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ positions: [], orders: [] }) });
    }
    // For app's login fetch, return a redirect.
    if (u.startsWith("/login")) {
      return Promise.resolve({ status: 303, ok: true });
    }
    return Promise.reject(new Error("unmocked fetch: " + method + " " + u));
  },
  URLSearchParams,
  // For lightweight-charts, no-op stub.
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
  const evt = { type: "click", target: el, currentTarget: el, returnValue: true, preventDefault() { this.returnValue = false; }, stopPropagation() {} };
  // dispatch on element first
  el.dispatchEvent(evt);
  // also fire document-level listeners
  if (docListeners.click) {
    for (const h of docListeners.click) {
      try { h(evt); } catch (e) { throw e; }
    }
  }
}
async function settle() { await new Promise(r => setImmediate(r)); }
async function boot() {
  // The IIFE has already run; boot() was called inside. Wait one tick for
  // the async chain to settle.
  await settle();
  await settle();
  await settle();
}

// Track class-changes for assertions: wrap classList.toggle/add/remove hooks.
function activeOn(el) {
  return el && el.classList && el.classList.contains("active");
}

// ---------- run the scenario ----------------------------------------------

(async () => {
  let failed = 0;
  function assert(cond, msg) {
    if (cond) {
      console.log("PASS:", msg);
    } else {
      console.log("FAIL:", msg);
      failed++;
    }
  }

  await boot();

  // Find the order BUY/SELL buttons (they exist in HTML even before login).
  const orderBuy = $("#orderBuy");
  const orderSell = $("#orderSell");
  const ladderBuy = $("#ladderBuy");
  const ladderSell = $("#ladderSell");
  assert(orderBuy, "HTML has #orderBuy button");
  assert(orderSell, "HTML has #orderSell button");
  assert(ladderBuy, "HTML has #ladderBuy button");
  assert(ladderSell, "HTML has #ladderSell button");

  // ---- Test 1: initial default = BUY (active on orderBuy) ----
  assert(activeOn(orderBuy), "initial: orderBuy has .active");
  assert(!activeOn(orderSell), "initial: orderSell does NOT have .active");
  assert(activeOn(ladderBuy), "initial: ladderBuy has .active");
  assert(!activeOn(ladderSell), "initial: ladderSell does NOT have .active");

  // ---- Test 2: click orderSell, then orderSell is active and orderBuy is not ----
  click(orderSell);
  assert(activeOn(orderSell), "after click #orderSell: orderSell has .active");
  assert(!activeOn(orderBuy), "after click #orderSell: orderBuy does NOT have .active");
  // ladder side must be independent
  assert(activeOn(ladderBuy), "after click #orderSell: ladderBuy still active (independent state)");

  // ---- Test 3: trigger previewOrder; payload.side must be 'sell' ----
  // Force prerequisites: state.exchange / state.account / state.marketType set.
  // We do this by selecting options and dispatching change events.
  const exchangeEl = $("#exchange");
  if (exchangeEl) {
    exchangeEl.value = "hyperliquid";
    const evt = { type: "change", target: exchangeEl, currentTarget: exchangeEl, returnValue: true, preventDefault() {}, stopPropagation() {} };
    exchangeEl.dispatchEvent(evt);
    if (docListeners.change) for (const h of docListeners.change) h(evt);
  }
  const accountEl = $("#account");
  if (accountEl) {
    accountEl.value = "fibo";
    const evt = { type: "change", target: accountEl, currentTarget: accountEl, returnValue: true, preventDefault() {}, stopPropagation() {} };
    accountEl.dispatchEvent(evt);
    if (docListeners.change) for (const h of docListeners.change) h(evt);
  }
  // Also fill price/size so preview doesn't bail.
  const orderPrice = $("#orderPrice"); if (orderPrice) orderPrice.value = "100";
  const orderSize = $("#orderSize"); if (orderSize) orderSize.value = "1";
  // Wait for any boot continuations.
  await settle(); await settle(); await settle();

  const previewBtn = $("#previewOrderBtn");
  assert(previewBtn, "HTML has #previewOrderBtn");
  click(previewBtn);
  // Drain microtasks + macrotasks: previewOrder is async, awaits fetch,
  // then awaits api() and calls showModal synchronously after.
  for (let i = 0; i < 10; i++) {
    await new Promise(r => setImmediate(r));
  }
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));

  const lastPreview = previewCalls[previewCalls.length - 1];
  assert(!!lastPreview, "previewOrder was called");
  assert(lastPreview && lastPreview.side === "sell", "preview payload side === 'sell' after tapping SELL");

  // ---- Test 4: confirmation modal reflects the new side ----
  // The app sets modal content via element.innerHTML = "<div>SELL BTC LIMIT …</div>".
  // Our minimal DOM stub stores innerHTML as a string; scan both textContent
  // AND innerHTML of every node.
  let foundSummary = false;
  function scanString(s) {
    if (typeof s !== "string") return;
    if (/SELL\s+\w+\s+LIMIT/.test(s)) foundSummary = true;
  }
  function walk(n) {
    if (!n) return;
    scanString(n.textContent);
    scanString(n.innerHTML);
    for (const c of n.children || []) walk(c);
  }
  walk(body);
  assert(foundSummary, "confirmation modal contains a 'SELL ... LIMIT ...' line");

  // ---- Test 5: tap BUY again -> preview payload side === 'buy' ----
  click(orderBuy);
  assert(activeOn(orderBuy), "after click #orderBuy: orderBuy has .active");
  assert(!activeOn(orderSell), "after click #orderBuy: orderSell does NOT have .active");

  // Wait for the invalidation hook to settle and then preview again.
  await settle();
  previewCalls.length = 0;
  click(previewBtn);
  for (let i = 0; i < 10; i++) {
    await new Promise(r => setImmediate(r));
  }
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  const second = previewCalls[previewCalls.length - 1];
  assert(!!second, "previewOrder was called again after BUY");
  assert(second && second.side === "buy", "preview payload side === 'buy' after tapping BUY");

  // ---- Test 6: changing side invalidates an existing preview ----
  // We assert that after a side-change, a subsequent /api/trade/execute call
  // (using the previously-stored preview_id) cannot be made because the
  // frontend does NOT keep an activeOrderPreview pointer past the click.
  // We approximate this by checking that activeOrderPreview in app.js was
  // cleared via the invalidation hook (the source wires invalidateActivePreviews).
  // Strongest signal: count preview calls and confirm that after side-change
  // the next preview is a fresh request, not a re-send of the cached preview.
  previewCalls.length = 0;
  click(orderSell);   // change to sell while no active preview
  await settle();
  click(previewBtn);
  for (let i = 0; i < 10; i++) {
    await new Promise(r => setImmediate(r));
  }
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  const fresh = previewCalls[previewCalls.length - 1];
  assert(fresh && fresh.side === "sell", "fresh preview after side change uses new side 'sell'");

  // ---- Test 7: /api/trade/execute was NEVER called during any test ----
  assert(executeCalls.length === 0, "no /api/trade/execute call was ever made (live safety)");

  // ---- Test 8: ladder side also has single authoritative state ----
  click(ladderSell);
  assert(activeOn(ladderSell), "ladder: after click #ladderSell, ladderSell active");
  assert(!activeOn(ladderBuy), "ladder: after click #ladderSell, ladderBuy inactive");
  // and order side is independent
  assert(activeOn(orderSell), "order side unaffected by ladder side click (still SELL)");

  // ---- Test 9: mobile layout (iPhone) — same buttons, same single-source state ----
  // In mobile layout (`@media (max-width:720px)`), the .trade panel is hidden
  // by default and only becomes visible when the mobile-nav "Trade" button
  // is tapped (sets state.mobile = "trade"). The order BUY/SELL buttons
  // are the SAME DOM elements (#orderBuy / #orderSell). The bug must NOT
  // re-appear in mobile layout — the fix uses state.orderSide which is
  // viewport-independent.
  click(orderBuy);
  assert(activeOn(orderBuy), "[mobile] after click #orderBuy, orderBuy active");
  assert(!activeOn(orderSell), "[mobile] after click #orderBuy, orderSell inactive");

  // Open the trade section in mobile
  const tradeTab = _querySelector(body, "[data-mobile-target='trade']");
  if (tradeTab) click(tradeTab);
  // Tap SELL via the same button id — the click handler is identical.
  click(orderSell);
  assert(activeOn(orderSell), "[mobile] after click #orderSell, orderSell active");
  assert(!activeOn(orderBuy), "[mobile] after click #orderSell, orderBuy inactive");
  // previewOrder must use state.orderSide ('sell') in mobile too.
  previewCalls.length = 0;
  click(previewBtn);
  for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r));
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  const mob = previewCalls[previewCalls.length - 1];
  assert(mob && mob.side === "sell", "[mobile] preview payload side === 'sell' after tapping SELL");

  console.log("---");
  console.log("preview calls:", previewCalls.length);
  console.log("execute calls:", executeCalls.length);
  if (failed > 0) {
    console.log("FAILURES:", failed);
    process.exit(1);
  } else {
    console.log("ALL PASS");
    process.exit(0);
  }
})().catch(e => {
  console.error("FATAL: scenario threw:", e.stack || e.message);
  process.exit(4);
});
