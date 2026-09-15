"""JSON schemas / serializers for HTTP + WebSocket (protocol v1)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..engine.engine import GoldenFiboEngine
from ..engine.events import DomainEvent
from ..engine.levels import ladder_step
from ..engine.state import EngineState, StateSnapshot
from ..metrics import OhlcvBar, fmt_metric, fmt_price, metrics_for_legs


PROTOCOL_VERSION = 1


def _s(x: Optional[Decimal]) -> Optional[str]:
    """Round engine prices to 2 decimal places for display/parity."""
    return fmt_price(x)


def _leg_ts_by_step(state: EngineState) -> Dict[int, int]:
    return {int(leg.step): int(leg.ts_ms) for leg in state.legs}


def levels_for_render(state: EngineState, extra: int = 2) -> List[Dict[str, Any]]:
    """Current-cycle ladder only: one entry per P0..P(n+2) with activation times.

    - Activated P0..P(n) use leg activation timestamps from the engine.
    - Projected P(n+1), P(n+2) start at current P(n) activation time.
    - TP is NOT a separate level: step n-1 is labeled (TP) when n >= 1.
    - Prices always from ladder_step (backend geometry only).
    """
    if state.p0 is None or state.highest_filled < 0:
        return []
    n = state.highest_filled
    leg_ts = _leg_ts_by_step(state)
    pn_ts = leg_ts.get(n)
    out: List[Dict[str, Any]] = []

    max_i = min(state.max_step, n + extra)
    for i in range(0, max_i + 1):
        p, _tp_at = ladder_step(state.side, state.p0, i, phi=state.phi, percentage=state.percentage)
        is_tp = n >= 1 and i == n - 1
        if i < n:
            kind = "activated"
            label = f"(TP) P{i}" if is_tp else f"P{i}"
            act_ms = leg_ts.get(i)
        elif i == n:
            kind = "current"
            label = f"P{i} [P(n)]" if n > 0 else f"P{i} [P(n)]"
            if n == 0:
                label = "P0 [P(n)]"
            act_ms = leg_ts.get(i)
        elif i == n + 1:
            kind = "projected_next"
            label = f"P{i} [P(n+1)]"
            act_ms = pn_ts
        else:
            kind = "projected_further"
            label = f"P{i} [P(n+2)]"
            act_ms = pn_ts

        out.append(
            {
                "id": f"P{i}",
                "step": i,
                "price": fmt_price(p),
                "kind": kind,
                "label": label,
                "is_tp": is_tp,
                "activation_ts_ms": act_ms,
                "activation_time": (act_ms // 1000) if act_ms is not None else None,
            }
        )
    return out


def markers_from_domain(events: List[DomainEvent]) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for e in events:
        t = e.ts_ms // 1000
        if e.kind.value in ("step_filled", "progression"):
            markers.append(
                {
                    "time": t,
                    "position": "belowBar",
                    "color": "#5b8def",
                    "shape": "circle",
                    "text": f"P{e.payload.get('step', e.payload.get('highest_filled', ''))}",
                }
            )
        elif e.kind.value in ("tp_hit", "cycle_closed"):
            markers.append(
                {
                    "time": t,
                    "position": "aboveBar",
                    "color": "#26a69a",
                    "shape": "arrowDown",
                    "text": "TP",
                }
            )
        elif e.kind.value in ("cycle_started", "p0_reset"):
            markers.append(
                {
                    "time": t,
                    "position": "belowBar",
                    "color": "#f5a623",
                    "shape": "arrowUp",
                    "text": "P0",
                }
            )
    return markers


def build_state_payload(
    *,
    mode: str,
    symbol: str,
    timeframe: str,
    side: str,
    percentage: str,
    price: Optional[str],
    engine: GoldenFiboEngine,
    candles: List[Dict[str, Any]],
    bars: List[OhlcvBar],
    recent_domain: Optional[List[DomainEvent]] = None,
    connected: bool = True,
) -> Dict[str, Any]:
    st = engine.state
    snap = StateSnapshot.from_state(st)
    ladder_ts = st.legs[0].ts_ms if st.legs else None
    step_ts = st.legs[-1].ts_ms if st.legs else None
    lv, sv, lp, sp, l_val, l_vah = metrics_for_legs(
        bars, ladder_start_ts_ms=ladder_ts, step_start_ts_ms=step_ts
    )

    last_candle_time = None
    if candles:
        last_candle_time = candles[-1].get("time")

    payload = {
        "v": PROTOCOL_VERSION,
        "type": "state_snapshot",
        "mode": mode,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "percentage": percentage,
        "price": fmt_price(price) if price is not None else None,
        "connected": connected,
        "cycle_id": st.cycle_id,
        "active": st.active,
        "p0": _s(st.p0),
        "initial_p0": _s(st.initial_p0),
        "n": st.highest_filled,
        "current_p": _s(st.current_p()),
        "shared_tp": _s(st.shared_tp),
        "next_p": _s(st.next_p()),
        "further_p": _s(st.further_p()),
        "legs": [
            {
                "step": leg.step,
                "entry": fmt_price(leg.entry),
                "ts_ms": leg.ts_ms,
                "time": leg.ts_ms // 1000,
                "qty": str(leg.qty),
            }
            for leg in st.legs
        ],
        "closed_count": len(st.closed),
        # Current ladder geometry for chart (no separate TP line)
        "levels": levels_for_render(st),
        "candles": candles,
        "last_candle_time": last_candle_time,
        # Metric windows (authoritative starts from legs)
        "metric_windows": {
            "ladder_start_ts_ms": ladder_ts,
            "ladder_start_time": (ladder_ts // 1000) if ladder_ts is not None else None,
            "step_start_ts_ms": step_ts,
            "step_start_time": (step_ts // 1000) if step_ts is not None else None,
            "latest_candle_time": last_candle_time,
        },
        "ladder_vwap": fmt_metric(lv),
        "active_step_vwap": fmt_metric(sv),
        "ladder_poc": fmt_metric(lp),
        "active_step_poc": fmt_metric(sp),
        "ladder_val": fmt_metric(l_val),
        "ladder_vah": fmt_metric(l_vah),
        "markers": markers_from_domain(recent_domain or []),
        "snapshot": {
            "symbol": snap.symbol,
            "side": snap.side,
            "n": snap.n,
            "p0": snap.p0,
            "current_p": snap.current_p,
            "shared_tp": snap.shared_tp,
            "next_p": snap.next_p,
            "further_p": snap.further_p,
        },
    }
    return payload


def candle_update_msg(candle: Dict[str, Any], *, final: bool = False) -> Dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "type": "candle_update", "candle": candle, "final": final}


def price_update_msg(price: str, ts_ms: int) -> Dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "type": "price_update", "price": fmt_price(price) or price, "ts_ms": ts_ms}


def engine_event_msg(events: List[DomainEvent], state_fragment: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": "engine_event",
        "events": [{"kind": e.kind.value, "ts_ms": e.ts_ms, "payload": e.payload} for e in events],
        "state": state_fragment,
    }
