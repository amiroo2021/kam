/**
 * WebTrade chart price-scale helpers (pure).
 * Loaded before app.js; also unit-tested under Node without a browser.
 *
 * Lightweight Charts v4.2 locks autoScale=false after the user drags the
 * Y-axis. On instrument change we must discard that lock and recompute the
 * visible range from the NEW candles (e.g. SP500 ~7600 → ETH ~2400).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.WebTradeChartScale = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function shouldResetPriceScale(prevNative, nextNative) {
    const a = String(prevNative || "").trim().toUpperCase();
    const b = String(nextNative || "").trim().toUpperCase();
    if (!b) return false;
    if (!a) return true;
    return a !== b;
  }

  function computeAutoscaleRange(candles, padRatio) {
    const pad = padRatio == null ? 0.08 : Number(padRatio);
    if (!Array.isArray(candles) || !candles.length) return null;
    let lo = Infinity;
    let hi = -Infinity;
    for (const c of candles) {
      const low = Number(c && c.low);
      const high = Number(c && c.high);
      if (Number.isFinite(low)) lo = Math.min(lo, low);
      if (Number.isFinite(high)) hi = Math.max(hi, high);
    }
    if (!(Number.isFinite(lo) && Number.isFinite(hi))) return null;
    if (hi < lo) return null;
    if (hi === lo) {
      const eps = Math.max(Math.abs(hi) * 0.001, 1e-6);
      return { from: lo - eps, to: hi + eps };
    }
    const span = hi - lo;
    const p = Math.max(0, pad) * span;
    return { from: lo - p, to: hi + p };
  }

  /**
   * Instrument-change render sequence checklist.
   * Returns ordered step names the UI must perform.
   */
  function instrumentChangeSteps() {
    return [
      "clear_overlays",
      "replace_candle_series",
      "setData",
      "autoScale_true",
      "fitContent",
      "apply_new_overlays",
    ];
  }

  /**
   * Simulate scale decision + range range for SP500 → ETH style switch.
   * Used by regression tests — no chart instance required.
   */
  function planInstrumentRender(prevNative, nextNative, candles) {
    const instrumentChanged = shouldResetPriceScale(prevNative, nextNative);
    const range = candles && candles.length ? computeAutoscaleRange(candles, 0.1) : null;
    return {
      instrumentChanged,
      replaceSeries: instrumentChanged,
      forceAutoscale: instrumentChanged,
      preserveUserZoom: !instrumentChanged,
      visibleRange: instrumentChanged ? range : null,
      steps: instrumentChanged
        ? instrumentChangeSteps()
        : ["setData", "optional_fitContent", "apply_overlays"],
    };
  }

  return {
    shouldResetPriceScale,
    computeAutoscaleRange,
    instrumentChangeSteps,
    planInstrumentRender,
  };
});
