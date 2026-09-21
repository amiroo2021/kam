"""Chart drawing smoke test for /backtest aggressive-flow overlays.

Verifies that _draw_jpg renders the four new Buy/Sell VWAP lines and the
new AGGRESSIVE FLOW summary line at the bottom of the chart. Uses synthetic
levels/candles so the chart can be drawn without a real Binance backtest.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
for _p in (str(_ROOT / "GoldenFibo" / "reference" / "legacy_research"),
           str(_ROOT / "GoldenFibo"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

wizard = importlib.import_module("plugins.trade.backtest_wizard")


def test_draw_jpg_renders_with_aggressive_flow_lines():
    """Synthetic chart with explicit Buy/Sell VWAPs renders without error
    and the file is produced on disk."""
    levels = [
        {"level": "P0", "price": 500.0, "role": ""},
        {"level": "P1", "price": 510.0, "role": ""},
        {"level": "P2", "price": 520.0, "role": ""},
        {"level": "P3", "price": 530.0, "role": ""},
    ]
    out = wizard._draw_jpg(
        "ZECUSDT", "spot", __import__("golden_fibo.constants", fromlist=["Side"]).Side.SELL,
        levels=levels, n=2,
        ladder_vwap=505.0, step_vwap=515.0,
        ladder_poc=508.0, step_poc=518.0,
        current=525.0,
        ladder_value_area={"val": 502.0, "vah": 528.0},
        step_delta_ratios={0: -0.1, 1: 0.05, 2: 0.15, 3: None},
        ladder_aggressor={
            "status": "COMPLETE", "buy_vwap": 503.0, "sell_vwap": 506.0,
            "delta_ratio": -0.20, "buy_base": 100, "sell_base": 150,
        },
        step_aggressor={
            "status": "COMPLETE", "buy_vwap": 514.0, "sell_vwap": 517.0,
            "delta_ratio": 0.10, "buy_base": 20, "sell_base": 16,
        },
        ladder_buy_vwap=503.0, ladder_sell_vwap=506.0,
        step_buy_vwap=514.0, step_sell_vwap=517.0,
        step_aggressor_table={
            0: {"buy_vwap": 501.0, "sell_vwap": 504.0, "delta_ratio": -0.10,
                "ladder_buy_vwap": 501.0, "ladder_sell_vwap": 504.0, "ladder_delta_ratio": -0.10},
            1: {"buy_vwap": 506.0, "sell_vwap": 508.0, "delta_ratio": 0.05,
                "ladder_buy_vwap": 503.5, "ladder_sell_vwap": 506.5, "ladder_delta_ratio": -0.04},
            2: {"buy_vwap": 514.0, "sell_vwap": 517.0, "delta_ratio": 0.10,
                "ladder_buy_vwap": 507.0, "ladder_sell_vwap": 509.0, "ladder_delta_ratio": 0.02},
        },
    )
    assert isinstance(out, str)
    p = Path(out)
    assert p.exists(), f"chart file not produced: {out}"
    assert p.stat().st_size > 1000, f"chart file suspiciously small: {p.stat().st_size} bytes"


def test_draw_jpg_moves_historical_flow_into_panel(monkeypatch):
    """Dense historical Pn flow values are not appended to price labels.

    They are rendered in the separate flow-history panel, so vertical price-line
    labels stay compact when P0..P13 are close together.
    """
    from PIL import ImageDraw

    original_draw = ImageDraw.Draw
    drawn_text = []

    class _DrawProxy:
        def __init__(self, inner):
            self._inner = inner

        def text(self, xy, text, *args, **kwargs):
            drawn_text.append(str(text))
            return self._inner.text(xy, text, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def _collecting_draw(*args, **kwargs):
        return _DrawProxy(original_draw(*args, **kwargs))

    monkeypatch.setattr(ImageDraw, "Draw", _collecting_draw)
    levels = [{"level": f"P{i}", "price": 500.0 + i, "role": ""} for i in range(16)]
    per_step = {
        i: {"delta_ratio": 0.01 * i, "ladder_delta_ratio": -0.01 * i}
        for i in range(14)
    }
    out = wizard._draw_jpg(
        "ZECUSDT", "spot", __import__("golden_fibo.constants", fromlist=["Side"]).Side.SELL,
        levels=levels, n=13,
        ladder_vwap=506.0, step_vwap=512.0,
        ladder_poc=507.0, step_poc=513.0,
        current=514.0,
        step_delta_ratios={i: 0.01 * i for i in range(14)},
        ladder_aggressor={"buy_vwap": 503.0, "sell_vwap": 506.0, "delta_ratio": -0.20},
        step_aggressor={"buy_vwap": 514.0, "sell_vwap": 517.0, "delta_ratio": 0.10},
        ladder_buy_vwap=503.0, ladder_sell_vwap=506.0,
        step_buy_vwap=514.0, step_sell_vwap=517.0,
        step_aggressor_table=per_step,
    )

    assert Path(out).exists()
    assert "Historical frozen per-step flow snapshots" in drawn_text
    assert "L-ΔR" in drawn_text
    historical_price_labels = [
        s for s in drawn_text
        if any(s.startswith(f"P{i} ") for i in range(13))
    ]
    assert historical_price_labels
    assert all("L-ΔR" not in s and "S-ΔR" not in s for s in historical_price_labels)


def test_draw_jpg_handles_missing_aggressive_flow_gracefully():
    """No aggressive-flow data — chart still renders, no exceptions."""
    levels = [
        {"level": "P0", "price": 500.0, "role": ""},
        {"level": "P1", "price": 510.0, "role": ""},
    ]
    out = wizard._draw_jpg(
        "BTCUSDT", "spot", __import__("golden_fibo.constants", fromlist=["Side"]).Side.BUY,
        levels=levels, n=1,
        ladder_vwap=505.0, step_vwap=508.0,
        ladder_poc=506.0, step_poc=509.0,
        current=507.0,
        # No aggressor args — must not crash.
    )
    assert Path(out).exists()
