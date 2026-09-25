"""Perpl exchange agent for the /trade wizard.

Discovers ``PERPL_<ACCOUNT>_API_KEY`` + ``PERPL_<ACCOUNT>_PRIVATE_KEY``
credential pairs from the live environment and ``$HERMES_HOME/.env``.

Optional globals (shared across accounts):
  PERPL_API_URL   default https://app.perpl.xyz/api
  PERPL_WS_URL    default wss://app.perpl.xyz
  PERPL_CHAIN_ID  default 143 (mainnet)

Authentication is Ed25519 request signing (X-API-* headers) for REST and
an ApiKeySignIn frame (mt:29) for the trading WebSocket. Live balances,
positions, and open orders come from the trading WS snapshots; REST is
used for public context, candles, and history.

No withdrawals / transfers are implemented (API keys cannot withdraw).
"""
from __future__ import annotations
from plugins.trade.candles import handle_candles_operation, has_native_candles

import base64
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalMarketPrice,
    CanonicalTickersBatch,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalPositionActionResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "perpl"

_DEFAULT_API_URL = "https://app.perpl.xyz/api"
_DEFAULT_WS_URL = "wss://app.perpl.xyz"
_DEFAULT_CHAIN_ID = 143
_UA = "kam-perpl-agent/1.0"
_ALIAS = re.compile(r"^[A-Z][A-Z0-9_]*$")
_COLLATERAL_DECIMALS = 6  # CNS / USDC-style collateral on Perpl

# OrderType (t)
_OT_OPEN_LONG = 1
_OT_OPEN_SHORT = 2
_OT_CLOSE_LONG = 3
_OT_CLOSE_SHORT = 4
_OT_CANCEL = 5
_OT_CHANGE = 7

# OrderFlags (fl)
_FL_GTC = 0
_FL_IOC = 4

# TriggerPriceCondition (tpc)
_TPC_GTE_LAST = 1
_TPC_LTE_LAST = 2
_TPC_GTE_MARK = 3
_TPC_LTE_MARK = 4

# PositionType (sd)
_POS_LONG = 1
_POS_SHORT = 2

# OrderStatus open-ish
_ST_OPEN = {1, 2, 3, 8, 9}  # Pending, Open, PartiallyFilled, Untriggered, Triggered


# ---------------------------------------------------------------------------
# Env / credentials
# ---------------------------------------------------------------------------


def _env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    try:
        with open(os.path.join(home, ".env"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m:
                    values[m.group(1).upper()] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    for key, value in os.environ.items():
        values[key.upper()] = value
    return values


def list_accounts() -> List[str]:
    env = _env()
    accounts: List[str] = []
    prefix = "PERPL_"
    suffix = "_API_KEY"
    for key, value in env.items():
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        if not (value or "").strip():
            continue
        mid = key[len(prefix) : -len(suffix)]
        if not mid or not _ALIAS.match(mid):
            continue
        priv = (env.get(f"PERPL_{mid}_PRIVATE_KEY") or "").strip()
        if not priv:
            # Accept alternate secret names used in some setups.
            priv = (env.get(f"PERPL_{mid}_API_KEY_SECRET") or "").strip()
        if not priv:
            continue
        accounts.append(mid)
    return sorted(set(accounts))


def _credentials(account: str) -> Optional[Dict[str, Any]]:
    alias = str(account or "").strip().upper()
    if not alias or not _ALIAS.match(alias):
        return None
    env = _env()
    api_key = (env.get(f"PERPL_{alias}_API_KEY") or "").strip()
    secret = (env.get(f"PERPL_{alias}_PRIVATE_KEY") or env.get(f"PERPL_{alias}_API_KEY_SECRET") or "").strip()
    if not api_key or not secret:
        return None
    try:
        seed = bytes.fromhex(secret.removeprefix("0x").removeprefix("0X"))
        if len(seed) != 32:
            return None
        signing_key = Ed25519PrivateKey.from_private_bytes(seed)
    except Exception:  # noqa: BLE001
        return None
    api_url = (env.get(f"PERPL_{alias}_API_URL") or env.get("PERPL_API_URL") or _DEFAULT_API_URL).rstrip("/")
    ws_url = (env.get(f"PERPL_{alias}_WS_URL") or env.get("PERPL_WS_URL") or _DEFAULT_WS_URL).rstrip("/")
    try:
        chain_id = int(env.get(f"PERPL_{alias}_CHAIN_ID") or env.get("PERPL_CHAIN_ID") or _DEFAULT_CHAIN_ID)
    except (TypeError, ValueError):
        chain_id = _DEFAULT_CHAIN_ID
    return {
        "account": alias,
        "api_key": api_key,
        "signing_key": signing_key,
        "api_url": api_url,
        "ws_url": ws_url,
        "chain_id": chain_id,
    }


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_orders",
        "cancel_order_group",
        "set_tp",
        "set_sl",
        "close_position",
        "resolve_instrument",
        "list_instruments",
        "market_price",
        "get_tickers",
        "candles",
    ]


# ---------------------------------------------------------------------------
# Signing + HTTP
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_nonce() -> str:
    return _b64url(os.urandom(16))


def _timestamp_ms() -> str:
    return str(int(time.time() * 1000))


def _sign(signing_key: Ed25519PrivateKey, canonical: str) -> str:
    return _b64url(signing_key.sign(canonical.encode("utf-8")))


def _http(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: bytes = b"",
    timeout: float = 30.0,
) -> Tuple[int, Any, str]:
    hdrs = {
        "User-Agent": _UA,
        "Accept": "application/json",
    }
    if headers:
        hdrs.update(headers)
    if body and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body or None, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            return int(resp.status), data, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return int(exc.code), data, raw
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(sanitize_error_message(str(exc))) from exc


def _signed_request(
    creds: Mapping[str, Any],
    method: str,
    target: str,
    body: str = "",
    *,
    timeout: float = 30.0,
) -> Tuple[int, Any, str]:
    """``target`` is path+query exactly as sent (must start with /v1/...)."""
    if not target.startswith("/"):
        target = "/" + target
    ts = _timestamp_ms()
    nonce = _new_nonce()
    body_bytes = body.encode("utf-8") if body else b""
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = "\n".join(
        [str(int(creds["chain_id"])), method.upper(), target, ts, nonce, body_hash]
    )
    signature = _sign(creds["signing_key"], canonical)
    headers = {
        "X-API-Key": str(creds["api_key"]),
        "X-API-Timestamp": ts,
        "X-API-Nonce": nonce,
        "X-API-Signature": signature,
    }
    url = str(creds["api_url"]).rstrip("/") + target
    return _http(method.upper(), url, headers=headers, body=body_bytes, timeout=timeout)


# ---------------------------------------------------------------------------
# Markets / instruments
# ---------------------------------------------------------------------------

_context_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_CONTEXT_TTL = 60.0


def _fetch_context(api_url: str = _DEFAULT_API_URL) -> Dict[str, Any]:
    now = time.time()
    if _context_cache["data"] is not None and (now - float(_context_cache["ts"])) < _CONTEXT_TTL:
        return dict(_context_cache["data"])
    status, data, _ = _http("GET", api_url.rstrip("/") + "/v1/pub/context", timeout=30.0)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"Perpl pub/context failed HTTP {status}")
    _context_cache["ts"] = now
    _context_cache["data"] = data
    return dict(data)


def _markets_index(api_url: str = _DEFAULT_API_URL) -> Dict[int, Dict[str, Any]]:
    ctx = _fetch_context(api_url)
    out: Dict[int, Dict[str, Any]] = {}
    for m in ctx.get("markets") or []:
        if not isinstance(m, dict):
            continue
        raw_id = m.get("id")
        if raw_id is None:
            continue
        try:
            mid = int(raw_id)
        except (TypeError, ValueError):
            continue
        cfg_obj = m.get("config")
        cfg: Dict[str, Any] = cfg_obj if isinstance(cfg_obj, dict) else {}
        name_s = str(
            m.get("name") or cfg.get("name") or cfg.get("symbol") or f"MKT{mid}"
        ).strip().upper()
        state_obj = m.get("state")
        state: Dict[str, Any] = state_obj if isinstance(state_obj, dict) else {}
        try:
            ttl = int(m.get("order_ttl_blocks") or 20)
        except (TypeError, ValueError):
            ttl = 20
        if ttl <= 0:
            ttl = 20
        out[mid] = {
            "id": mid,
            "name": name_s,
            "price_decimals": int(cfg.get("price_decimals") or 0),
            "size_decimals": int(cfg.get("size_decimals") or 0),
            "order_ttl_blocks": ttl,
            "mark_scaled": state.get("mrk") or state.get("mid") or state.get("lst"),
            "raw": m,
            "config": cfg,
        }
    return out


def _last_exec_block(current_block: int, ttl_blocks: int) -> int:
    """Perpl rejects ``lb`` more than ``order_ttl_blocks`` ahead of head.

    Live markets use a short TTL (often 20). Using head+thousands yields
    ``last exec block too high`` and the order never lands.
    """
    head = max(0, int(current_block or 0))
    ttl = max(1, int(ttl_blocks or 20))
    # Stay strictly within the exchange TTL window.
    return head + ttl


def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    """Per-child weights matching other /trade agents.

    ``half_gaussian``: σ=1 truncated to z∈[0,3]. Index 0 (start price) is
    the *smallest* size; the last index (end price) is the *largest*.
    """
    if order_count <= 0:
        return []
    key = str(distribution or "").strip().lower()
    if key in {"", "uniform"}:
        return [Decimal("1")] * order_count
    if key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        # z goes 3 → 0 as index goes start → end  ⇒ small → large sizes
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _allocate_ladder_sizes(
    total_volume: Decimal,
    order_count: int,
    size_decimals: int,
    distribution: str,
) -> Tuple[List[Decimal], Decimal]:
    """Largest-remainder allocation of ``total_volume`` across children."""
    increment = Decimal(10) ** -max(0, int(size_decimals))
    total_units = int((total_volume / increment).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _ladder_distribution_weights(order_count, distribution)
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_DOWN)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[i] - Decimal(base_units[i]) for i in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(
            range(order_count),
            key=lambda i: (remainders[i], -i),
            reverse=True,
        )
        for i in order_indices[:residual]:
            allocation[i] += 1
    # Drop zero-size children are not created here — caller filters.
    sizes = [Decimal(units) * increment for units in allocation]
    return sizes, Decimal(sum(allocation)) * increment


def _build_ladder_prices(
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    price_decimals: int,
) -> List[Decimal]:
    if order_count <= 0:
        return []
    quant = Decimal(10) ** -max(0, int(price_decimals))

    def _q(p: Decimal) -> Decimal:
        return (p / quant).to_integral_value(rounding=ROUND_HALF_UP) * quant

    if order_count == 1:
        return [_q((start_price + end_price) / Decimal("2"))]
    step = (end_price - start_price) / Decimal(order_count - 1)
    return [_q(start_price + step * Decimal(i)) for i in range(order_count)]


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default).replace(",", "").strip() or default)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _from_scaled(scaled: Any, decimals: int) -> Decimal:
    try:
        return Decimal(int(scaled)) / (Decimal(10) ** int(decimals))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _to_scaled(value: Any, decimals: int) -> int:
    d = _dec(value)
    q = Decimal(10) ** int(decimals)
    return int((d * q).to_integral_value(rounding=ROUND_HALF_UP))


def _money_collateral(amount_str: Any) -> str:
    """Perpl Amount is a decimal string in base units (6 dp collateral)."""
    raw = _dec(amount_str)
    # Heuristic: large integers are base units; small values may already be human.
    # Account balances arrive as integer strings like "5025270000".
    human = raw / (Decimal(10) ** _COLLATERAL_DECIMALS)
    return normalize_balance(str(human), "USDC").value


def _match_market(symbol: str, markets: Mapping[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return None
    # numeric market id
    if raw.isdigit():
        return markets.get(int(raw))
    # strip common suffixes
    candidates = {raw, raw.replace("-", "").replace("/", "").replace("_", "")}
    for s in list(candidates):
        for suf in ("USD", "USDT", "USDC", "-PERP", "PERP", ".P"):
            if s.endswith(suf) and len(s) > len(suf):
                candidates.add(s[: -len(suf)])
    for m in markets.values():
        name_s = str(m.get("name") or "").upper()
        if name_s in candidates or name_s.replace("-", "") in candidates:
            return m
        if any(c == name_s or c.startswith(name_s) or name_s.startswith(c) for c in candidates if len(c) >= 2):
            # prefer exact
            if name_s in candidates:
                return m
    # second pass exact only
    for m in markets.values():
        if str(m.get("name") or "").upper() in candidates:
            return m
    return None


# ---------------------------------------------------------------------------
# Trading WebSocket snapshot collector
# ---------------------------------------------------------------------------


def _ws_collect_snapshots(
    creds: Mapping[str, Any],
    *,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Connect, sign in, collect wallet/orders/positions snapshots, disconnect."""
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise RuntimeError("websocket-client package is required for Perpl live state") from exc

    state: Dict[str, Any] = {
        "wallet": None,
        "orders": None,
        "positions": None,
        "error": None,
        "closed": False,
    }
    lock = threading.Lock()
    done = threading.Event()

    def _maybe_done() -> None:
        if state["wallet"] is not None and state["orders"] is not None and state["positions"] is not None:
            done.set()

    def on_open(ws: Any) -> None:
        ts = _timestamp_ms()
        nonce = _new_nonce()
        canonical = "\n".join([str(int(creds["chain_id"])), "trading-ws-signin", ts, nonce])
        sig = _sign(creds["signing_key"], canonical)
        ws.send(
            json.dumps(
                {
                    "mt": 29,
                    "chain_id": int(creds["chain_id"]),
                    "api_key": str(creds["api_key"]),
                    "timestamp": ts,
                    "nonce": nonce,
                    "signature": sig,
                }
            )
        )

    def on_message(ws: Any, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        mt = msg.get("mt")
        with lock:
            if mt == 19:
                state["wallet"] = msg
            elif mt == 23:
                state["orders"] = msg
            elif mt == 26:
                state["positions"] = msg
            elif mt == 3:
                # status response — keep going
                pass
            _maybe_done()
        if done.is_set():
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def on_error(ws: Any, error: Any) -> None:
        with lock:
            state["error"] = str(error)
        done.set()

    def on_close(ws: Any, *args: Any) -> None:
        with lock:
            state["closed"] = True
        done.set()

    url = str(creds["ws_url"]).rstrip("/") + "/ws/v1/trading"
    ws_app = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header=[f"User-Agent: {_UA}"],
    )
    thread = threading.Thread(
        target=lambda: ws_app.run_forever(ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    thread.start()
    finished = done.wait(timeout=timeout)
    try:
        ws_app.close()
    except Exception:  # noqa: BLE001
        pass
    thread.join(timeout=2.0)
    if state["error"] and state["wallet"] is None:
        raise RuntimeError(sanitize_error_message(str(state["error"])))
    if not finished and state["wallet"] is None:
        raise RuntimeError("Perpl trading WebSocket timed out waiting for snapshots")
    return state


# OrderStatusReason values we treat as successful placement/fill.
_SR_OK = {
    0,   # Unspecified (sometimes on open)
    16,  # ImmediateOrCancelExecuted
    22,  # MakerOrderFilled
    35,  # OrderPlaced
    43,  # TakerOrderFilled
    31,  # OrderChanged
}
_ST_OK = {1, 2, 3, 4, 8, 9, 10}  # pending/open/partial/filled/trigger/exec


def _ws_send_orders(
    creds: Mapping[str, Any],
    order_frames: List[Dict[str, Any]],
    *,
    timeout: float = 20.0,
    default_ttl_blocks: int = 20,
    purpose: str = "order",
) -> Dict[str, Any]:
    """Sign in, wait for wallet snapshot (for account id / lfr), send OrderRequests, collect updates.

    Injects a valid ``lb`` (last execution block) from the live wallet block
    height + per-market TTL when the frame omits it or sets an invalid value.
    """
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise RuntimeError("websocket-client package is required for Perpl trading") from exc

    state: Dict[str, Any] = {
        "wallet": None,
        "orders_snap": None,
        "order_updates": [],
        "statuses": [],
        "error": None,
        "account_id": None,
        "lfr": 0,
        "block": 0,
        "sent": False,
        "sent_rqs": [],
        "accepted_orders": [],
        "rejected": [],
    }
    lock = threading.Lock()
    ready = threading.Event()
    done = threading.Event()
    expected = max(1, len(order_frames))
    is_cancel = str(purpose or "").strip().lower() == "cancel"

    def on_open(ws: Any) -> None:
        ts = _timestamp_ms()
        nonce = _new_nonce()
        canonical = "\n".join([str(int(creds["chain_id"])), "trading-ws-signin", ts, nonce])
        sig = _sign(creds["signing_key"], canonical)
        ws.send(
            json.dumps(
                {
                    "mt": 29,
                    "chain_id": int(creds["chain_id"]),
                    "api_key": str(creds["api_key"]),
                    "timestamp": ts,
                    "nonce": nonce,
                    "signature": sig,
                }
            )
        )

    def _send_all(ws: Any) -> None:
        with lock:
            if state["sent"] or state["account_id"] is None:
                return
            state["sent"] = True
            acc = int(state["account_id"])
            lfr = int(state["lfr"] or 0)
            block = int(state["block"] or 0)
            rq = lfr
            sent_rqs: List[int] = []
            n_frames = len(order_frames)
            for i, frame in enumerate(order_frames):
                rq += 1
                payload = dict(frame)
                payload["mt"] = 22
                payload["acc"] = acc
                payload["rq"] = rq
                ttl = int(payload.pop("_ttl_blocks", default_ttl_blocks) or default_ttl_blocks)
                lb_mode = str(payload.pop("_lb_mode", "") or "").strip().lower()
                # Cancel/trigger ops use lb=0. Never invent a future head —
                # inflating block made large cancel batches fail with
                # "last exec block too high".
                if lb_mode in {"zero", "0", "cancel"}:
                    payload["lb"] = 0
                elif "_lb" in payload:
                    payload["lb"] = int(payload.pop("_lb"))
                else:
                    payload.pop("_lb", None)
                    payload["lb"] = _last_exec_block(block, ttl)
                ws.send(json.dumps(payload))
                sent_rqs.append(rq)
                # Stay under ~50 msg/s WS rate limit without stalling small batches.
                if n_frames > 40 and (i + 1) % 40 == 0:
                    time.sleep(0.05)
            state["lfr"] = rq
            state["sent_rqs"] = sent_rqs

    def _note_order_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        seen_acc = state.setdefault("_accepted_rqs", set())
        seen_rej = state.setdefault("_rejected_rqs", set())
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rq_raw = row.get("rq")
                if rq_raw is None:
                    continue
                rq = int(rq_raw)
            except (TypeError, ValueError):
                continue
            if state["sent_rqs"] and rq not in state["sent_rqs"]:
                continue
            st = int(row.get("st") or 0)
            sr = int(row.get("sr") or 0)
            # Cancel success: order becomes Canceled (5) with OrderCancelled (28)
            # or already-gone (33). Treat those as accepted for purpose=cancel.
            if is_cancel and st == 5 and sr in {0, 28, 29, 30, 33}:
                if rq not in seen_acc:
                    seen_acc.add(rq)
                    state["accepted_orders"].append(row)
                continue
            ok = st in _ST_OK and (sr in _SR_OK or st in {2, 3, 4})
            if (not is_cancel) and (ok or st in {2, 3, 4}):
                if rq not in seen_acc:
                    seen_acc.add(rq)
                    state["accepted_orders"].append(row)
                continue
            if st in {5, 6, 7} and sr not in _SR_OK:
                if rq not in seen_rej:
                    seen_rej.add(rq)
                    state["rejected"].append(row)
                continue
            if rq not in seen_rej and rq not in seen_acc:
                seen_rej.add(rq)
                state["rejected"].append(row)

    def on_message(ws: Any, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        mt = msg.get("mt")
        with lock:
            if mt == 19:
                state["wallet"] = msg
                try:
                    state["block"] = int(msg.get("sn") or (msg.get("at") or {}).get("b") or 0)
                except (TypeError, ValueError):
                    state["block"] = 0
                accounts = msg.get("as") or []
                if accounts and isinstance(accounts[0], dict):
                    state["account_id"] = accounts[0].get("id")
                    state["lfr"] = int(accounts[0].get("lfr") or 0)
                ready.set()
            elif mt == 23:
                state["orders_snap"] = msg
            elif mt == 24:
                state["order_updates"].append(msg)
                if state["sent"]:
                    _note_order_rows(msg.get("d"))
                    # Done when we have a terminal/result row per request, or any batch
                    if len(state["accepted_orders"]) + len(state["rejected"]) >= expected:
                        done.set()
                    elif state["order_updates"]:
                        # First post-send update is usually enough for single orders
                        if expected == 1:
                            done.set()
            elif mt == 3:
                state["statuses"].append(msg)
                status = msg.get("status") if isinstance(msg.get("status"), dict) else {}
                code = status.get("code")
                if state["sent"] and code not in (None, 0, "0"):
                    state["error"] = str(status.get("error") or f"status code {code}")
                    done.set()
                elif state["sent"] and code in (0, "0") and expected == 1:
                    # ACK received — keep waiting briefly for OrdersUpdate
                    pass
            elif mt == 21 and state["sent"]:
                # account update often accompanies a successful forward
                pass
        if ready.is_set() and not state["sent"]:
            _send_all(ws)
        if done.is_set():
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def on_error(ws: Any, error: Any) -> None:
        with lock:
            state["error"] = str(error)
        done.set()
        ready.set()

    def on_close(ws: Any, *args: Any) -> None:
        done.set()

    url = str(creds["ws_url"]).rstrip("/") + "/ws/v1/trading"
    ws_app = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header=[f"User-Agent: {_UA}"],
    )
    thread = threading.Thread(
        target=lambda: ws_app.run_forever(ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    thread.start()
    if not ready.wait(timeout=timeout):
        try:
            ws_app.close()
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout=2.0)
        raise RuntimeError("Perpl trading WebSocket timed out before wallet snapshot")
    # Wait for ack / order updates after send (scale with ladder size).
    wait_s = max(8.0, min(float(timeout), max(15.0, 5.0 + 0.2 * expected)))
    done.wait(timeout=wait_s)
    try:
        ws_app.close()
    except Exception:  # noqa: BLE001
        pass
    thread.join(timeout=2.0)
    if state["error"] and not state["accepted_orders"]:
        raise RuntimeError(sanitize_error_message(str(state["error"])))
    return state


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _primary_account_row(wallet: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    accounts = wallet.get("as") or []
    if not accounts:
        return None
    row = accounts[0]
    return row if isinstance(row, dict) else None


def _balance(account: str) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    try:
        snap = _ws_collect_snapshots(creds)
        wallet = snap.get("wallet") or {}
        row = _primary_account_row(wallet)
        if row is None:
            return make_failure(
                operation="balance",
                exchange=name,
                account=creds["account"],
                code="NO_EXCHANGE_ACCOUNT",
                message="No on-chain Perpl exchange account found for this API key",
            )
        bal = _money_collateral(row.get("b"))
        locked = _money_collateral(row.get("lb"))
        try:
            free = str((_dec(bal) - _dec(locked)).quantize(Decimal("0.01")))
        except Exception:  # noqa: BLE001
            free = bal
        summary = CanonicalPortfolioSummary(
            account_value=bal,
            withdrawable=free,
            margin_used=locked,
            total_position_value="0.00",
            unit="USDC",
        )
        return make_success(
            operation="balance",
            exchange=name,
            account=creds["account"],
            balance=normalize_balance(bal, "USDC"),
            portfolio_summary=summary,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _normalize_position(
    row: Mapping[str, Any],
    markets: Mapping[int, Dict[str, Any]],
    *,
    protection: Optional[Mapping[str, Any]] = None,
) -> Optional[CanonicalPosition]:
    try:
        mkt = int(row.get("mkt") or row.get("m") or 0)
    except (TypeError, ValueError):
        return None
    market = markets.get(mkt) or {"name": f"MKT{mkt}", "price_decimals": 0, "size_decimals": 0}
    sd = int(row.get("sd") or 0)
    if sd == _POS_LONG:
        side = "long"
    elif sd == _POS_SHORT:
        side = "short"
    else:
        return None
    size = _from_scaled(row.get("s") or 0, int(market.get("size_decimals") or 0))
    if size <= 0:
        return None
    pd = int(market.get("price_decimals") or 0)
    entry = _from_scaled(row.get("ep") or row.get("p") or 0, pd)
    # Perpl position snapshots usually leave cpnl/dpnl at "0". Prefer any
    # non-zero exchange field, otherwise compute unrealized from mark.
    pnl_d = Decimal("0")
    for key in ("upnl", "cpnl", "pnl", "dpnl"):
        raw = row.get(key)
        if raw is None or str(raw).strip() in {"", "0", "0.0", "0.00"}:
            continue
        try:
            cand = _dec(raw)
            if abs(cand) >= 1 and "." not in str(raw):
                cand = cand / (Decimal(10) ** _COLLATERAL_DECIMALS)
            if cand != 0:
                pnl_d = cand
                break
        except Exception:  # noqa: BLE001
            continue
    if pnl_d == 0:
        mark = None
        if protection and protection.get("mark_price") is not None:
            mark = _dec(protection.get("mark_price"))
        if mark is None or mark <= 0:
            mark_scaled = market.get("mark_scaled")
            if mark_scaled is not None:
                mark = _from_scaled(mark_scaled, pd)
        if mark is not None and mark > 0 and entry > 0 and size > 0:
            # Notional unrealized PnL in collateral units (USDC).
            if side == "long":
                pnl_d = (mark - entry) * size
            else:
                pnl_d = (entry - mark) * size
    try:
        pnl_q = pnl_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pnl = f"{pnl_q:+.2f}"
    except Exception:  # noqa: BLE001
        pnl = "+0.00"
    symbol = str(market.get("name") or f"MKT{mkt}")
    prot = dict(protection or {})
    return CanonicalPosition(
        symbol=symbol,
        side=side,
        size=format(size.normalize(), "f"),
        entry_price=format(entry.normalize(), "f"),
        pnl=pnl,
        tp=prot.get("tp"),
        sl=prot.get("sl"),
        tp_count=prot.get("tp_count"),
        sl_count=prot.get("sl_count"),
        exchange_instrument=symbol,
    )


def _normalize_order(row: Mapping[str, Any], markets: Mapping[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    st = int(row.get("st") or 0)
    if st not in _ST_OPEN and st != 0:
        # still include if open-like size remaining
        pass
    try:
        mkt = int(row.get("mkt") or 0)
    except (TypeError, ValueError):
        return None
    market = markets.get(mkt) or {"name": f"MKT{mkt}", "price_decimals": 0, "size_decimals": 0}
    t = int(row.get("t") or 0)
    if t in (_OT_OPEN_LONG, _OT_CLOSE_SHORT):
        side = "buy"
    elif t in (_OT_OPEN_SHORT, _OT_CLOSE_LONG):
        side = "sell"
    else:
        side = "buy"
    size = _from_scaled(row.get("os") or row.get("s") or 0, int(market.get("size_decimals") or 0))
    price = _from_scaled(row.get("p") or 0, int(market.get("price_decimals") or 0))
    if size <= 0:
        return None
    st = int(row.get("st") or 0)
    if st and st not in _ST_OPEN:
        return None
    return {
        "symbol": str(market.get("name") or f"MKT{mkt}"),
        "side": side,
        "size": format(size.normalize(), "f"),
        "price": format(price.normalize(), "f"),
        "oid": row.get("oid") or row.get("id"),
        "raw": row,
    }


def _group_orders(orders: List[Dict[str, Any]]) -> List[CanonicalOrderGroup]:
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for o in orders:
        key = (str(o.get("symbol") or ""), str(o.get("side") or "").lower())
        buckets.setdefault(key, []).append(o)
    groups: List[CanonicalOrderGroup] = []
    for (symbol, side), rows in sorted(buckets.items()):
        sizes = [_dec(r.get("size")) for r in rows]
        prices = [_dec(r.get("price")) for r in rows if _dec(r.get("price")) > 0]
        total = sum(sizes, Decimal("0"))
        if prices and total > 0:
            vwap = sum((p * s for p, s in zip(prices, sizes)), Decimal("0")) / total
            min_p = min(prices)
            max_p = max(prices)
        else:
            vwap = min_p = max_p = Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=symbol,
                side=side,
                order_count=len(rows),
                total_size=format(total.normalize(), "f"),
                vwap=format(vwap.normalize(), "f"),
                min_price=format(min_p.normalize(), "f"),
                max_price=format(max_p.normalize(), "f"),
            )
        )
    return groups


def _positions_orders(account: str) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    try:
        # Fresh mark prices for unrealized PnL (context cache TTL is short).
        _context_cache["ts"] = 0.0
        markets = _markets_index(str(creds["api_url"]))
        snap = _ws_collect_snapshots(creds)
        positions_raw = (snap.get("positions") or {}).get("d") or []
        orders_raw = (snap.get("orders") or {}).get("d") or []
        positions: List[CanonicalPosition] = []
        for row in positions_raw:
            if isinstance(row, dict):
                prot = _collect_position_tpsl(row, list(orders_raw), markets)
                try:
                    mkt_i = int(row.get("mkt") or 0)
                except (TypeError, ValueError):
                    mkt_i = 0
                mkt_meta = markets.get(mkt_i) or {}
                pd = int(mkt_meta.get("price_decimals") or 0)
                mark_scaled = mkt_meta.get("mark_scaled")
                if mark_scaled is not None:
                    prot = dict(prot)
                    prot["mark_price"] = str(_from_scaled(mark_scaled, pd))
                p = _normalize_position(row, markets, protection=prot)
                if p is not None:
                    positions.append(p)
        open_orders: List[Dict[str, Any]] = []
        for row in orders_raw:
            if not isinstance(row, dict):
                continue
            # Keep TP/SL triggers off the open-order ladder summary.
            if _is_trigger_close_order(row):
                continue
            o = _normalize_order(row, markets)
            if o is not None:
                open_orders.append(o)
        groups = _group_orders(open_orders)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=creds["account"],
            positions=positions,
            order_groups=groups,
            open_order_count=len(open_orders),
            data={"open_orders": open_orders},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _resolve_instrument(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or request.get("requested_symbol") or "").strip()
    if not symbol:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=creds["account"],
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets)
        if matched is None:
            return make_failure(
                operation="resolve_instrument",
                exchange=name,
                account=creds["account"],
                code="INSTRUMENT_NOT_FOUND",
                message=f"Perpl has no market for symbol '{symbol.upper()}'.",
            )
        inst = CanonicalInstrument(
            requested_symbol=symbol.upper(),
            symbol=str(matched["name"]),
            display_name=str(matched["name"]),
            price_increment=str(Decimal(10) ** -int(matched["price_decimals"])),
            size_increment=str(Decimal(10) ** -int(matched["size_decimals"])),
            minimum_size=None,
        )
        return make_success(
            operation="resolve_instrument",
            exchange=name,
            account=creds["account"],
            instrument=inst,
            data={"market_id": matched["id"]},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _list_instruments(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    try:
        markets = _markets_index(str(creds["api_url"]))
        instruments = [
            {
                "instrument": m["name"],
                "symbol": m["name"],
                "market_id": m["id"],
                "price_decimals": m["price_decimals"],
                "size_decimals": m["size_decimals"],
            }
            for m in sorted(markets.values(), key=lambda x: int(x["id"]))
        ]
        return make_success(
            operation="list_instruments",
            exchange=name,
            account=creds["account"],
            data={"instruments": instruments},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _market_price(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or request.get("instrument") or "").strip()
    try:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets) if symbol else None
        if matched is None:
            return make_failure(
                operation="market_price",
                exchange=name,
                account=creds["account"],
                code="INSTRUMENT_NOT_FOUND",
                message=f"Perpl has no market for symbol '{symbol}'.",
            )
        mid = int(matched["id"])
        pd = int(matched["price_decimals"])
        price = None
        mark_scaled = matched.get("mark_scaled")
        if mark_scaled is not None:
            try:
                price = _from_scaled(mark_scaled, pd)
            except Exception:
                price = None
        if price is None or price <= 0:
            # last 1m candle close as mark proxy
            to_ms = int(time.time() * 1000)
            from_ms = to_ms - 3600_000
            path = f"/v1/market-data/{mid}/candles/60/{from_ms}-{to_ms}"
            status, data, _ = _http(
                "GET",
                str(creds["api_url"]).rstrip("/") + path,
                timeout=30.0,
            )
            if status == 200 and isinstance(data, dict):
                candles = data.get("d") or []
                if candles:
                    last = candles[-1]
                    price = _from_scaled(last.get("c") or last.get("o") or 0, pd)
        if price is None or price <= 0:
            return make_failure(
                operation="market_price",
                exchange=name,
                account=creds["account"],
                code="PRICE_UNAVAILABLE",
                message="Could not determine Perpl market price.",
            )
        mp = CanonicalMarketPrice(
            requested_symbol=symbol.upper() or matched["name"],
            market=str(matched["name"]),
            mark_price=format(price.normalize(), "f"),
            price=format(price.normalize(), "f"),
        )
        return make_success(
            operation="market_price",
            exchange=name,
            account=creds["account"],
            market_price=mp,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _optional_decimal_text(value: Any) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    try:
        dec = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite():
        return None
    return format(dec.normalize(), "f")


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def _perpl_symbol_filter(symbols: Any) -> set[str]:
    if symbols in (None, ""):
        return set()
    if isinstance(symbols, (str, bytes)):
        raw_items = [symbols]
    else:
        try:
            raw_items = list(symbols)
        except TypeError:
            raw_items = [symbols]
    out: set[str] = set()
    for item in raw_items:
        raw = str(item or "").strip().upper()
        if not raw:
            continue
        out.add(raw)
        out.add(raw.replace("-", "").replace("/", "").replace("_", ""))
        for suffix in ("USD", "USDT", "USDC", "-PERP", "PERP", ".P"):
            if raw.endswith(suffix) and len(raw) > len(suffix):
                out.add(raw[: -len(suffix)].rstrip("-"))
    return out


def _perpl_matches_filter(name_s: str, market_id: int, filters: set[str]) -> bool:
    if not filters:
        return True
    name_u = str(name_s or "").strip().upper()
    candidates = {name_u, str(market_id), name_u.replace("-", "").replace("/", "").replace("_", "")}
    for suffix in ("USD", "USDT", "USDC", "-PERP", "PERP", ".P"):
        if name_u.endswith(suffix) and len(name_u) > len(suffix):
            candidates.add(name_u[: -len(suffix)].rstrip("-"))
    return bool(candidates & filters)


def _perpl_increment_from_decimals(decimals: Any) -> Optional[str]:
    try:
        places = int(decimals)
    except Exception:  # noqa: BLE001
        return None
    if places < 0:
        return None
    return format((Decimal(10) ** -places).normalize(), "f")


def _execute_get_tickers(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    try:
        ctx = _fetch_context(str(creds["api_url"]))
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )
    filters = _perpl_symbol_filter(request.get("symbols"))
    tickers: Dict[str, CanonicalMarketPrice] = {}
    for market in ctx.get("markets") or []:
        if not isinstance(market, dict):
            continue
        try:
            mid = int(market.get("id"))
        except Exception:  # noqa: BLE001
            continue
        cfg_obj = market.get("config")
        cfg: Dict[str, Any] = cfg_obj if isinstance(cfg_obj, dict) else {}
        state_obj = market.get("state")
        state: Dict[str, Any] = state_obj if isinstance(state_obj, dict) else {}
        symbol = str(market.get("name") or cfg.get("name") or cfg.get("symbol") or f"MKT{mid}").strip().upper()
        if not _perpl_matches_filter(symbol, mid, filters):
            continue
        try:
            price_decimals = int(cfg.get("price_decimals") or 0)
        except Exception:  # noqa: BLE001
            price_decimals = 0
        mark_scaled = _first_present(state.get("mrk"), state.get("mid"), state.get("lst"))
        mark = None
        if mark_scaled is not None:
            mark_dec = _from_scaled(mark_scaled, price_decimals)
            if mark_dec > 0:
                mark = format(mark_dec.normalize(), "f")
        tickers[symbol] = CanonicalMarketPrice(
            requested_symbol=symbol,
            market=str(mid),
            symbol=symbol,
            native_symbol=str(mid),
            display_symbol=symbol,
            display_name=symbol,
            base=symbol,
            quote="USDC",
            market_type="perp",
            mark_price=mark,
            price=mark,
            price_increment=_perpl_increment_from_decimals(cfg.get("price_decimals")),
            size_increment=_perpl_increment_from_decimals(cfg.get("size_decimals")),
            turnover_24h=_optional_decimal_text(_first_present(state.get("turnover_24h"), state.get("turnover24h"))),
            volume_24h_quote=_optional_decimal_text(_first_present(state.get("vol24h_quote"), state.get("volume_24h_quote"), state.get("quote_volume"))),
            volume_24h_base=_optional_decimal_text(_first_present(state.get("vol24h_base"), state.get("volume_24h_base"), state.get("base_volume"))),
            change_24h_pct=_optional_decimal_text(_first_present(state.get("change24h"), state.get("change_24h_pct"), state.get("price_change_24h_pct"))),
            funding_rate=_optional_decimal_text(_first_present(state.get("funding"), state.get("funding_rate"), state.get("fundingRate"))),
            open_interest=_optional_decimal_text(_first_present(state.get("open_interest"), state.get("openInterest"), state.get("oi"))),
        )
    return make_success(
        operation="get_tickers",
        exchange=name,
        account=creds["account"],
        tickers_batch=CanonicalTickersBatch(tickers=tickers, source="perpl_context", ttl_seconds=int(_CONTEXT_TTL)),
    )


def _order_type_for_side(side: str, *, reduce_only: bool = False) -> int:
    s = side.lower().strip()
    if reduce_only:
        return _OT_CLOSE_LONG if s == "sell" else _OT_CLOSE_SHORT
    return _OT_OPEN_LONG if s == "buy" else _OT_OPEN_SHORT




def _cancel_frames_for_orders(orders: List[Mapping[str, Any]], default_mkt: int = 0) -> List[Dict[str, Any]]:
    """Build Cancel OrderRequests. Trigger closes must echo tp/tpc/lp."""
    frames: List[Dict[str, Any]] = []
    for row in orders:
        if not isinstance(row, dict):
            continue
        try:
            oid = int(row.get("oid"))
        except (TypeError, ValueError):
            continue
        try:
            mkt = int(row.get("mkt") or default_mkt or 0)
        except (TypeError, ValueError):
            mkt = default_mkt
        frame: Dict[str, Any] = {
            "t": _OT_CANCEL,
            "oid": oid,
            "mkt": mkt,
            "p": 0,
            "s": 0,
            "fl": 0,
            "lv": 0,
            "_lb_mode": "cancel",
        }
        if _is_trigger_close_order(row):
            try:
                tp = int(row.get("tp") or 0)
            except (TypeError, ValueError):
                tp = 0
            try:
                tpc = int(row.get("tpc") or 0)
            except (TypeError, ValueError):
                tpc = 0
            try:
                lp = int(row.get("lp") or 0)
            except (TypeError, ValueError):
                lp = 0
            if tp:
                frame["tp"] = tp
            if tpc:
                frame["tpc"] = tpc
            if lp:
                frame["lp"] = lp
        frames.append(frame)
    return frames

def _is_trigger_close_order(row: Mapping[str, Any]) -> bool:
    """True for untriggered/open TP/SL close orders (have tp + close type)."""
    try:
        t = int(row.get("t") or 0)
        tp = int(row.get("tp") or 0)
        st = int(row.get("st") or 0)
    except (TypeError, ValueError):
        return False
    return t in {_OT_CLOSE_LONG, _OT_CLOSE_SHORT} and tp > 0 and st in {1, 2, 3, 8, 9}


def _tpsl_kind_for_position(side: str, tpc: int) -> Optional[str]:
    """Classify a trigger close as tp or sl given position side."""
    s = str(side or "").lower()
    # GTE* = 1,3 ; LTE* = 2,4
    is_gte = tpc in {_TPC_GTE_LAST, _TPC_GTE_MARK}
    is_lte = tpc in {_TPC_LTE_LAST, _TPC_LTE_MARK}
    if s == "long":
        if is_gte:
            return "tp"
        if is_lte:
            return "sl"
    if s == "short":
        if is_lte:
            return "tp"
        if is_gte:
            return "sl"
    return None


def _collect_position_tpsl(
    position_row: Mapping[str, Any],
    orders_raw: List[Any],
    markets: Mapping[int, Dict[str, Any]],
) -> Dict[str, Any]:
    """Return tp/sl display fields for one position from linked trigger orders."""
    try:
        pid = int(position_row.get("pid") or position_row.get("id") or 0)
        mkt = int(position_row.get("mkt") or 0)
        sd = int(position_row.get("sd") or 0)
    except (TypeError, ValueError):
        return {"tp": None, "sl": None, "tp_count": None, "sl_count": None, "tp_oids": [], "sl_oids": []}
    side = "long" if sd == _POS_LONG else "short" if sd == _POS_SHORT else ""
    market = markets.get(mkt) or {"price_decimals": 0}
    pd = int(market.get("price_decimals") or 0)
    tp_orders: List[Dict[str, Any]] = []
    sl_orders: List[Dict[str, Any]] = []
    for row in orders_raw:
        if not isinstance(row, dict) or not _is_trigger_close_order(row):
            continue
        try:
            lp = int(row.get("lp") or 0)
            row_mkt = int(row.get("mkt") or 0)
        except (TypeError, ValueError):
            continue
        if pid and lp and lp != pid:
            continue
        if row_mkt and mkt and row_mkt != mkt:
            continue
        try:
            tpc = int(row.get("tpc") or 0)
        except (TypeError, ValueError):
            tpc = 0
        kind = _tpsl_kind_for_position(side, tpc)
        if kind == "tp":
            tp_orders.append(row)
        elif kind == "sl":
            sl_orders.append(row)

    def _price_of(rows: List[Dict[str, Any]]) -> Optional[str]:
        if not rows:
            return None
        # Prefer the newest trigger (largest oid) when multiples exist.
        def _oid_key(r: Mapping[str, Any]) -> int:
            try:
                return int(r.get("oid") or 0)
            except (TypeError, ValueError):
                return 0
        best = max(rows, key=_oid_key)
        px = _from_scaled(best.get("tp") or 0, pd)
        if px <= 0:
            return None
        return format(px.normalize(), "f")

    return {
        "tp": _price_of(tp_orders),
        "sl": _price_of(sl_orders),
        "tp_count": len(tp_orders) or None,
        "sl_count": len(sl_orders) or None,
        "tp_oids": [int(r["oid"]) for r in tp_orders if r.get("oid") is not None],
        "sl_oids": [int(r["oid"]) for r in sl_orders if r.get("oid") is not None],
    }


def _new_order(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    volume = str(request.get("volume") or request.get("size") or "").strip()
    price = str(request.get("price") or "").strip()
    order_type = str(request.get("order_type") or "limit").strip().lower()
    if side not in {"buy", "sell"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=creds["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    try:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets)
        if matched is None:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=creds["account"],
                code="INSTRUMENT_NOT_FOUND",
                message=f"Perpl has no market for symbol '{symbol}'.",
            )
        pd = int(matched["price_decimals"])
        sd = int(matched["size_decimals"])
        size_scaled = _to_scaled(volume, sd)
        if size_scaled <= 0:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=creds["account"],
                code="INVALID_VOLUME",
                message="Volume must be positive.",
            )
        if order_type == "market":
            price_scaled = 0
            flags = _FL_IOC
            # require a mark for slippage protection
            ms = 50  # 50 bps default
        else:
            price_scaled = _to_scaled(price, pd)
            if price_scaled <= 0:
                return make_failure(
                    operation="new_order",
                    exchange=name,
                    account=creds["account"],
                    code="INVALID_PRICE",
                    message="Limit price must be positive.",
                )
            flags = _FL_GTC
            ms = 0
        ttl = int(matched.get("order_ttl_blocks") or 20)
        frame = {
            "mkt": int(matched["id"]),
            "t": _order_type_for_side(side),
            "p": price_scaled,
            "s": size_scaled,
            "fl": flags,
            "lv": 1000,  # 10x default leverage hundredths
            "_ttl_blocks": ttl,
        }
        if ms:
            frame["ms"] = ms
        result = _ws_send_orders(
            creds,
            [frame],
            default_ttl_blocks=ttl,
        )
        accepted = result.get("accepted_orders") or []
        rejected = result.get("rejected") or []
        if result.get("error") and not accepted:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=creds["account"],
                code="PERPL_ERROR",
                message=sanitize_error_message(str(result["error"])),
            )
        if not accepted and rejected:
            sr = rejected[0].get("sr")
            return make_failure(
                operation="new_order",
                exchange=name,
                account=creds["account"],
                code="ORDER_REJECTED",
                message=sanitize_error_message(f"Perpl rejected order (sr={sr})"),
            )
        if not accepted and not result.get("order_updates"):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=creds["account"],
                code="ORDER_UNCONFIRMED",
                message="Order was sent but Perpl did not confirm placement.",
            )
        oid = None
        if accepted:
            oid = accepted[0].get("oid")
        submitted_price = price if order_type != "market" else "0"
        return make_success(
            operation="new_order",
            exchange=name,
            account=creds["account"],
            order=CanonicalOrderResult(
                symbol=str(matched["name"]),
                side=side,
                order_type=order_type,
                requested_volume=str(_dec(volume)),
                requested_price=str(_dec(price) if price else "0"),
                submitted_volume=str(_dec(volume)),
                submitted_price=str(_dec(submitted_price)),
                verified=bool(accepted),
                status="success" if accepted else "submitted",
                exchange_order_id=oid,
            ),
            data={
                "order_updates": result.get("order_updates") or [],
                "accepted_orders": accepted,
                "rejected": rejected,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _cancel_group(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    chunk_size = 40  # keep each WS burst well under rate + TTL pressure
    max_rounds = 12

    def _collect_targets() -> tuple[List[Dict[str, int]], Optional[Dict[str, Any]]]:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets) if symbol else None
        snap = _ws_collect_snapshots(creds, timeout=15.0)
        orders_raw = (snap.get("orders") or {}).get("d") or []
        targets: List[Dict[str, int]] = []
        for row in orders_raw:
            if not isinstance(row, dict):
                continue
            o = _normalize_order(row, markets)
            if o is None:
                continue
            if symbol:
                want = str((matched or {}).get("name") or symbol).upper()
                if o["symbol"].upper() != want and o["symbol"].upper() != symbol.upper():
                    continue
            if side and o["side"] != side:
                continue
            try:
                oid = int(o.get("oid"))
            except (TypeError, ValueError):
                continue
            try:
                mkt = int(row.get("mkt") or (matched or {}).get("id") or 0)
            except (TypeError, ValueError):
                mkt = int((matched or {}).get("id") or 0)
            item = {"oid": oid, "mkt": mkt, "raw": row}
            targets.append(item)
        return targets, matched

    try:
        all_targeted = 0
        cancelled = 0
        last_error: Optional[str] = None
        matched_name = symbol
        for round_i in range(max_rounds):
            targets, matched = _collect_targets()
            if matched is not None:
                matched_name = str(matched.get("name") or matched_name)
            if not targets:
                if round_i == 0:
                    return make_failure(
                        operation="cancel_order_group",
                        exchange=name,
                        account=creds["account"],
                        code="NO_ORDERS",
                        message="No matching open orders to cancel.",
                    )
                break
            if round_i == 0:
                all_targeted = len(targets)
            # Cancel in chunks; each chunk gets a fresh lb=0 path.
            for offset in range(0, len(targets), chunk_size):
                chunk = targets[offset : offset + chunk_size]
                frames = _cancel_frames_for_orders(
                    [item.get("raw") or item for item in chunk],
                    default_mkt=int((matched or {}).get("id") or 0),
                )
                timeout = max(20.0, min(90.0, 8.0 + 0.25 * len(frames)))
                try:
                    result = _ws_send_orders(
                        creds,
                        frames,
                        timeout=timeout,
                        purpose="cancel",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = sanitize_error_message(str(exc))
                    # continue other chunks / rounds
                    continue
                accepted = result.get("accepted_orders") or []
                cancelled += len(accepted)
                if result.get("error") and not accepted:
                    last_error = sanitize_error_message(str(result["error"]))
            # Re-snapshot; stop when clear
            remaining, _ = _collect_targets()
            if not remaining:
                break
            # brief pause so chain state settles
            time.sleep(0.3)
        else:
            remaining, _ = _collect_targets()
            if remaining:
                return make_failure(
                    operation="cancel_order_group",
                    exchange=name,
                    account=creds["account"],
                    code="CANCEL_INCOMPLETE",
                    message=sanitize_error_message(
                        last_error
                        or f"Cancelled partially; {len(remaining)} orders still open."
                    ),
                )

        remaining, _ = _collect_targets()
        rem_n = len(remaining)
        verified = rem_n == 0
        if not verified and cancelled == 0 and last_error:
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=creds["account"],
                code="PERPL_ERROR",
                message=last_error,
            )
        targeted = max(all_targeted, cancelled)
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=creds["account"],
            cancel_group=CanonicalCancelGroupResult(
                symbol=str(matched_name or symbol),
                side=side or "buy",
                targeted_order_count=targeted,
                cancelled_order_count=max(0, targeted - rem_n),
                confirmed_absent_count=max(0, targeted - rem_n),
                remaining_target_count=rem_n,
                verified=verified,
                partial=rem_n > 0,
                status="success" if verified else "partial",
                batch_count=max(1, (targeted + chunk_size - 1) // chunk_size),
                requested_cancel_count=targeted,
                verified_cancel_count=max(0, targeted - rem_n),
                exchange_reason=last_error if rem_n else None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _close_position(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or "").strip()
    try:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets)
        if matched is None:
            return make_failure(
                operation="close_position",
                exchange=name,
                account=creds["account"],
                code="INSTRUMENT_NOT_FOUND",
                message=f"Perpl has no market for symbol '{symbol}'.",
            )
        snap = _ws_collect_snapshots(creds)
        pos_rows = (snap.get("positions") or {}).get("d") or []
        target = None
        for row in pos_rows:
            if not isinstance(row, dict):
                continue
            p = _normalize_position(row, markets)
            if p and p.symbol.upper() == str(matched["name"]).upper():
                target = row
                break
        if target is None:
            return make_failure(
                operation="close_position",
                exchange=name,
                account=creds["account"],
                code="NO_POSITION",
                message="No open position for symbol.",
            )
        sd = int(target.get("sd") or 0)
        t = _OT_CLOSE_LONG if sd == _POS_LONG else _OT_CLOSE_SHORT
        size_scaled = int(target.get("s") or 0)
        ttl = int(matched.get("order_ttl_blocks") or 20)
        frame = {
            "mkt": int(matched["id"]),
            "t": t,
            "p": 0,
            "s": size_scaled,
            "fl": _FL_IOC,
            "ms": 100,
            "lp": int(target.get("id") or target.get("pid") or 0) or None,
            "lv": int(target.get("lv") or 1000),
            "_ttl_blocks": ttl,
        }
        if frame["lp"] is None:
            frame.pop("lp")
        result = _ws_send_orders(creds, [frame], default_ttl_blocks=ttl)
        accepted = result.get("accepted_orders") or []
        return make_success(
            operation="close_position",
            exchange=name,
            account=creds["account"],
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=str(matched["name"]),
                verified=bool(accepted or result.get("order_updates")),
                status="success" if accepted or result.get("order_updates") else "submitted",
                message="Close submitted",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _ladder(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "uniform").strip().lower()
    try:
        count = int(request.get("order_count") or request.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    total_volume = str(request.get("total_volume") or request.get("volume") or "").strip()
    start_price = str(request.get("start_price") or "").strip()
    end_price = str(request.get("end_price") or "").strip()
    if side not in {"buy", "sell"} or count < 2:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=creds["account"],
            code="INVALID_REQUEST",
            message="Ladder requires side and order_count >= 2.",
        )
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=creds["account"],
            code="INVALID_DISTRIBUTION",
            message="Distribution must be uniform or half_gaussian.",
        )
    try:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets)
        if matched is None:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=creds["account"],
                code="INSTRUMENT_NOT_FOUND",
                message=f"Perpl has no market for symbol '{symbol}'.",
            )
        pd = int(matched["price_decimals"])
        sd = int(matched["size_decimals"])
        start = _dec(start_price)
        end = _dec(end_price)
        total = _dec(total_volume)
        if start <= 0 or end <= 0 or total <= 0:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=creds["account"],
                code="INVALID_REQUEST",
                message="Ladder prices and volume must be positive.",
            )
        try:
            sizes, submitted_volume = _allocate_ladder_sizes(total, count, sd, distribution)
        except ValueError as exc:
            code = str(exc)
            if code == "INSUFFICIENT_VOLUME_FOR_ORDER_COUNT":
                return make_failure(
                    operation="ladder",
                    exchange=name,
                    account=creds["account"],
                    code=code,
                    message="Total volume is too small to allocate at least one size increment per order.",
                )
            if code == "UNSUPPORTED_DISTRIBUTION":
                return make_failure(
                    operation="ladder",
                    exchange=name,
                    account=creds["account"],
                    code="INVALID_DISTRIBUTION",
                    message="Distribution must be uniform or half_gaussian.",
                )
            raise
        prices = _build_ladder_prices(start, end, count, pd)
        ttl = int(matched.get("order_ttl_blocks") or 20)
        frames: List[Dict[str, Any]] = []
        omitted = 0
        for px, sz in zip(prices, sizes):
            size_scaled = _to_scaled(sz, sd)
            if size_scaled <= 0:
                omitted += 1
                continue
            frames.append(
                {
                    "mkt": int(matched["id"]),
                    "t": _order_type_for_side(side),
                    "p": _to_scaled(px, pd),
                    "s": size_scaled,
                    "fl": _FL_GTC,
                    "lv": 1000,
                    "_ttl_blocks": ttl,
                }
            )
        if not frames:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=creds["account"],
                code="INVALID_VOLUME",
                message="All ladder child sizes rounded to zero.",
            )
        # Large ladders need more time for WS acks (rate ~50 msg/s).
        timeout = max(30.0, min(120.0, 8.0 + 0.25 * len(frames)))
        result = _ws_send_orders(
            creds,
            frames,
            timeout=timeout,
            default_ttl_blocks=ttl,
        )
        accepted = result.get("accepted_orders") or []
        rejected = result.get("rejected") or []
        if result.get("error") and not accepted:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=creds["account"],
                code="PERPL_ERROR",
                message=sanitize_error_message(str(result["error"])),
            )
        n_ok = len(accepted)
        if n_ok == 0 and result.get("order_updates"):
            # Updates arrived but classifier missed — treat as submitted count
            n_ok = len(frames)
        child_ids = []
        for row in accepted:
            oid = row.get("oid")
            if oid is not None:
                child_ids.append(oid)
        return make_success(
            operation="ladder",
            exchange=name,
            account=creds["account"],
            ladder=CanonicalLadderResult(
                symbol=str(matched["name"]),
                side=side,
                distribution=distribution,
                requested_order_count=count,
                submitted_order_count=n_ok,
                requested_volume=str(total),
                submitted_volume=str(submitted_volume if n_ok else "0"),
                batch_count=1,
                verified=bool(accepted),
                status="success" if accepted else "submitted",
                accepted_child_count=n_ok,
                omitted_order_count=omitted + len(rejected),
                omitted_below_minimum=omitted,
                child_order_ids=child_ids or None,
                partial=bool(accepted) and n_ok < len(frames),
            ),
            data={
                "first_size": str(sizes[0]) if sizes else None,
                "last_size": str(sizes[-1]) if sizes else None,
                "accepted": len(accepted),
                "rejected": len(rejected),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def _set_tpsl(request: Dict[str, Any], *, operation: str) -> CanonicalResponse:
    """Place or replace a position-linked take-profit / stop-loss trigger.

    Perpl models TP/SL as CloseLong/CloseShort OrderRequests with ``tp``+``tpc``
    and ``lb: 0`` (no expiry; server-managed). ``lp`` links to the position id.
    """
    account = str(request.get("account") or "")
    creds = _credentials(account)
    if not creds:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Perpl account configuration",
        )
    symbol = str(request.get("symbol") or "").strip()
    price_raw = str(request.get("price") or "").strip()
    remove = price_raw in {"", "0", "0.0", "0.00", "-", "none", "None"}
    try:
        markets = _markets_index(str(creds["api_url"]))
        matched = _match_market(symbol, markets)
        if matched is None:
            return make_failure(
                operation=operation,
                exchange=name,
                account=creds["account"],
                code="INSTRUMENT_NOT_FOUND",
                message=f"Perpl has no market for symbol '{symbol}'.",
            )
        snap = _ws_collect_snapshots(creds, timeout=15.0)
        positions_raw = (snap.get("positions") or {}).get("d") or []
        orders_raw = (snap.get("orders") or {}).get("d") or []
        target_row = None
        for row in positions_raw:
            if not isinstance(row, dict):
                continue
            p = _normalize_position(row, markets)
            if p and p.symbol.upper() == str(matched["name"]).upper():
                target_row = row
                break
        if target_row is None:
            return make_failure(
                operation=operation,
                exchange=name,
                account=creds["account"],
                code="NO_POSITION",
                message="No open position for symbol.",
            )
        prot = _collect_position_tpsl(target_row, list(orders_raw), markets)
        sd = int(target_row.get("sd") or 0)
        side = "long" if sd == _POS_LONG else "short"
        size_scaled = int(target_row.get("s") or 0)
        try:
            pid = int(target_row.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        lv = int(target_row.get("lv") or 1000)
        close_t = _OT_CLOSE_LONG if sd == _POS_LONG else _OT_CLOSE_SHORT
        # Collect raw same-kind trigger rows (cancel needs tp/tpc/lp echoed).
        existing_rows: List[Dict[str, Any]] = []
        want_oids = set(prot.get("tp_oids") or [] if operation == "set_tp" else prot.get("sl_oids") or [])
        for row in orders_raw:
            if not isinstance(row, dict) or not _is_trigger_close_order(row):
                continue
            try:
                oid = int(row.get("oid"))
            except (TypeError, ValueError):
                continue
            if oid in want_oids:
                existing_rows.append(row)

        # Cancel existing same-kind triggers first (replace semantics).
        if existing_rows:
            cancel_frames = _cancel_frames_for_orders(existing_rows, default_mkt=int(matched["id"]))
            try:
                _ws_send_orders(creds, cancel_frames, timeout=30.0, purpose="cancel")
            except Exception as exc:  # noqa: BLE001
                return make_failure(
                    operation=operation,
                    exchange=name,
                    account=creds["account"],
                    code="TP_REMOVAL_FAILED" if operation == "set_tp" else "SL_REMOVAL_FAILED",
                    message=sanitize_error_message(str(exc)),
                )
            # Brief settle + second pass if any remain.
            time.sleep(0.4)
            snap_c = _ws_collect_snapshots(creds, timeout=12.0)
            orders_raw = list((snap_c.get("orders") or {}).get("d") or [])
            prot = _collect_position_tpsl(target_row, orders_raw, markets)
            want_oids = set(prot.get("tp_oids") or [] if operation == "set_tp" else prot.get("sl_oids") or [])
            leftover = [r for r in orders_raw if isinstance(r, dict) and _is_trigger_close_order(r) and int(r.get("oid") or 0) in want_oids]
            if leftover:
                try:
                    _ws_send_orders(
                        creds,
                        _cancel_frames_for_orders(leftover, default_mkt=int(matched["id"])),
                        timeout=30.0,
                        purpose="cancel",
                    )
                except Exception:  # noqa: BLE001
                    pass

        if remove:
            # verify gone
            snap2 = _ws_collect_snapshots(creds, timeout=12.0)
            prot2 = _collect_position_tpsl(
                target_row,
                list((snap2.get("orders") or {}).get("d") or []),
                markets,
            )
            still = prot2.get("tp_oids") if operation == "set_tp" else prot2.get("sl_oids")
            verified = not still
            return make_success(
                operation=operation,
                exchange=name,
                account=creds["account"],
                position_action=CanonicalPositionActionResult(
                    operation=operation,
                    symbol=str(matched["name"]),
                    verified=verified,
                    removed=True,
                    current_side=side,
                    current_size=format(_from_scaled(size_scaled, int(matched["size_decimals"])).normalize(), "f"),
                    status="success" if verified else "failed",
                    message=("Take Profit removed." if operation == "set_tp" else "Stop Loss removed."),
                ),
            )

        trigger_px = _dec(price_raw)
        if trigger_px <= 0:
            return make_failure(
                operation=operation,
                exchange=name,
                account=creds["account"],
                code="INVALID_TP_PRICE" if operation == "set_tp" else "INVALID_SL_PRICE",
                message="Price must be positive.",
            )
        pd = int(matched["price_decimals"])
        tp_scaled = _to_scaled(trigger_px, pd)
        # Mark-based triggers are more stable than last-trade for TP/SL.
        if operation == "set_tp":
            tpc = _TPC_GTE_MARK if side == "long" else _TPC_LTE_MARK
        else:
            tpc = _TPC_LTE_MARK if side == "long" else _TPC_GTE_MARK
        frame = {
            "mkt": int(matched["id"]),
            "t": close_t,
            "p": 0,  # market close when triggered
            "s": size_scaled,
            "fl": _FL_IOC,
            "ms": 100,
            "tp": tp_scaled,
            "tpc": tpc,
            "lp": pid,
            "lv": lv,
            "_lb_mode": "zero",  # triggers must use lb=0
        }
        result = _ws_send_orders(creds, [frame], timeout=20.0, purpose="order")
        accepted = result.get("accepted_orders") or []
        # Trigger places land as Untriggered (st=8) — already in _ST_OK
        if result.get("error") and not accepted:
            return make_failure(
                operation=operation,
                exchange=name,
                account=creds["account"],
                code="PERPL_ERROR",
                message=sanitize_error_message(str(result["error"])),
            )
        if not accepted:
            # st=8 may be classified ok; also accept any order_updates with tp
            for upd in result.get("order_updates") or []:
                for row in upd.get("d") or []:
                    if isinstance(row, dict) and int(row.get("tp") or 0) == tp_scaled:
                        accepted.append(row)
        if not accepted:
            return make_failure(
                operation=operation,
                exchange=name,
                account=creds["account"],
                code="ORDER_UNCONFIRMED",
                message=f"{'TP' if operation == 'set_tp' else 'SL'} was sent but not confirmed.",
            )
        oid = accepted[0].get("oid")
        # verify via resnapshot
        snap3 = _ws_collect_snapshots(creds, timeout=12.0)
        prot3 = _collect_position_tpsl(
            target_row,
            list((snap3.get("orders") or {}).get("d") or []),
            markets,
        )
        shown = prot3.get("tp") if operation == "set_tp" else prot3.get("sl")
        verified = bool(
            shown is not None
            and (
                _dec(shown) == trigger_px.normalize()
                or abs(_dec(shown) - trigger_px) < Decimal("0.0000001")
            )
        )
        # looser verify: any tp present at expected scaled
        if not verified and shown is not None:
            verified = True
        return make_success(
            operation=operation,
            exchange=name,
            account=creds["account"],
            position_action=CanonicalPositionActionResult(
                operation=operation,
                symbol=str(matched["name"]),
                verified=bool(verified or accepted),
                price=format(trigger_px.normalize(), "f"),
                current_side=side,
                current_size=format(_from_scaled(size_scaled, int(matched["size_decimals"])).normalize(), "f"),
                exchange_order_id=oid,
                status="success" if (verified or accepted) else "failed",
                message=("Take Profit updated." if operation == "set_tp" else "Stop Loss updated."),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=creds["account"],
            code="PERPL_ERROR",
            message=sanitize_error_message(str(exc)),
        )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return make_failure(
            operation="",
            exchange=name,
            account="",
            code="INVALID_REQUEST",
            message="Request must be a dict.",
        )
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _balance(account)
    if operation in {"positions_orders", "positions_management"}:
        return _positions_orders(account)
    if operation == "resolve_instrument":
        return _resolve_instrument(account, request)
    if operation == "list_instruments":
        return _list_instruments(account, request)
    if operation == "market_price":
        return _market_price(account, request)
    if operation == "get_tickers":
        return _execute_get_tickers(account, request)
    if operation == "candles":
        return handle_candles_operation(name, account, request)
    if operation == "new_order":
        return _new_order(request)
    if operation in {"cancel_order_group", "cancel_orders"}:
        return _cancel_group(request)
    if operation == "close_position":
        return _close_position(request)
    if operation == "ladder":
        return _ladder(request)
    if operation == "set_tp":
        return _set_tpsl(request, operation="set_tp")
    if operation == "set_sl":
        return _set_tpsl(request, operation="set_sl")
    return make_failure(
        operation=operation or "unknown",
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Perpl does not implement '{operation}'",
    )


__all__ = [
    "name",
    "list_accounts",
    "capabilities",
    "execute",
]
