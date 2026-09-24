"""WebTrade2 Phase 2 service: write-side surface over TradeDesk.

Application-level independent from WebTrade (:9001): does NOT share an
in-memory WebTradeService instance, does NOT call WebTrade over HTTP,
does NOT duplicate exchange-specific trading logic. Reuses shared
primitives (TradeDesk, canonical types, ladder_math, PreviewPlanStore)
where safe.

Hard server-side gates:
  - WEBTRADE2_WRITE_ENABLED: when 0, every write endpoint returns 423
    PHASE2_DISABLED. The frontend must also disable write controls, but
    the server gate is authoritative.
  - WEBTRADE2_DRY_RUN: when 1, the execute path returns DRY_RUN status
    WITHOUT calling desk.execute on a write op. Never fabricates
    VERIFIED/SUBMITTED status; never fabricates exchange_order_ids.

Preview plan is bound to:
  - exchange, account, market_type, instrument (symbol), side, operation,
    final normalized price/size, reduce_only where relevant.
HMAC integrity, short TTL, one-shot consume, replay protection are
inherited from PreviewPlanStore.

Status normalization:
  - DRY_RUN             (dry-run path; never reached exchange)
  - VERIFIED            (post-submit evidence verifies exchange state)
  - SUBMITTED           (accepted, but verification not established)
  - PARTIALLY_SUBMITTED (some children accepted, some not)
  - REJECTED            (exchange refused)
  - FAILED              (transport / non-exchange error)
  - UNKNOWN             (we don't know)
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from ..tradedesk import TradeDesk, get_tradedesk
from ..ladder_math import build_ladder_children, quantize_to_increment
from ..webtrade.preview_plans import PreviewPlanStore

logger = logging.getLogger("webtrade2.phase2")


# --- minimal safe helpers (replicate the few shared utilities WebTrade
# service exposes, but without depending on that module's internals so
# WebTrade2's process stays self-contained).

def _to_plain(obj: Any) -> Any:
    if obj is None:
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


def _fmt_dec(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _safe_error(resp: Any) -> Dict[str, str]:
    """Mirror of WebTradeService._safe_error: scrub secret-bearing strings."""
    err = _to_plain(getattr(resp, "error", None)) or {}
    if not isinstance(err, dict):
        return {"code": "ERROR", "message": str(err)}
    code = str(err.get("code") or "ERROR")
    msg = str(err.get("message") or err.get("code") or "Operation failed.")
    low = msg.lower()
    for bad in ("api key", "private key", "secret", "password", "signature", "authorization"):
        if bad in low:
            msg = code
            break
    if "http" in low and ("error" in low or "://" in low or "status" in low):
        reason = str(err.get("exchange_reason") or "").strip()
        if reason and "http" not in reason.lower() and "://" not in reason:
            msg = reason
        else:
            msg = "Order rejected by exchange." if "order" in low else "Exchange request failed."
    for needle in (" for url:", "Client Error:", "Server Error:", "HTTPSConnectionPool"):
        if needle.lower() in low:
            msg = "Order rejected by exchange." if "order" in low else "Exchange request failed."
            break
    return {"code": code, "message": msg}


# --- capability gate ------------------------------------------------------

_WRITE_OPS = {
    "new_order", "ladder",
    "set_tp", "set_sl", "close_position",
    "cancel_order", "cancel_order_group",
}


class WebTrade2Phase2Service:
    """Phase 2 write surface for WebTrade2.

    Stateless wrapper around TradeDesk + PreviewPlanStore. Holds the
    configuration so endpoints can read the kill-switch / dry-run
    flags without re-importing globals.
    """

    def __init__(
        self,
        desk: Optional[TradeDesk] = None,
        session_secret: str = "webtrade2-dev-secret",
        write_enabled: bool = False,
        dry_run: bool = True,
        preview_ttl_seconds: int = 300,
    ) -> None:
        self.desk = desk or get_tradedesk()
        self.write_enabled = bool(write_enabled)
        self.dry_run = bool(dry_run)
        self.previews = PreviewPlanStore(session_secret, ttl_seconds=int(preview_ttl_seconds))

    # ---- status helpers -------------------------------------------------

    def phase2_status(self) -> Dict[str, Any]:
        return {
            "phase": 2,
            "write_enabled": self.write_enabled,
            "dry_run": self.dry_run,
            "preview_ttl_seconds": self.previews.ttl_seconds,
        }

    def phase2_capabilities(self, exchange: str) -> Dict[str, Any]:
        caps = set(self.desk.capabilities(exchange))
        return {
            "exchange": exchange,
            "capabilities": sorted(caps),
            "order_type_limit": "new_order" in caps,
            "order_type_market": False,  # agents do not advertise market orders via TradeDesk today
            "ladder": "ladder" in caps,
            "cancel_order": "cancel_order" in caps,
            "cancel_order_group": "cancel_order_group" in caps,
            "set_tp": "set_tp" in caps,
            "set_sl": "set_sl" in caps,
            "close_position": "close_position" in caps,
            "reduce_only": "reduce_only" in caps,
        }

    # ---- gate ------------------------------------------------------------

    def _gate(self) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Return (status_code, body) if writes are disabled, else None."""
        if not self.write_enabled:
            return 423, {
                "success": False,
                "error": {"code": "PHASE2_DISABLED", "message": "WebTrade2 writes are disabled (WEBTRADE2_WRITE_ENABLED=0)."},
            }
        return None

    def _require_capability(self, exchange: str, operation: str) -> Optional[Dict[str, Any]]:
        if operation not in _WRITE_OPS:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED", "message": f"Unknown write op {operation!r}."},
                "operation": operation,
                "exchange": exchange,
            }
        caps = set(self.desk.capabilities(exchange))
        if operation not in caps:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED", "message": f"{exchange} does not expose {operation}."},
                "operation": operation,
                "exchange": exchange,
            }
        return None

    def _validate_inputs(
        self,
        *,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        extra_required: Tuple[Tuple[str, Any], ...] = (),
    ) -> Optional[Dict[str, Any]]:
        if not exchange:
            return {"success": False, "error": {"code": "MISSING_EXCHANGE", "message": "exchange is required."}}
        if not account:
            return {"success": False, "error": {"code": "MISSING_ACCOUNT", "message": "account is required."}}
        if not symbol:
            return {"success": False, "error": {"code": "MISSING_INSTRUMENT", "message": "symbol is required."}}
        s = str(side or "").strip().lower()
        if s not in {"buy", "sell"}:
            return {"success": False, "error": {"code": "INVALID_SIDE", "message": "side must be buy or sell."}}
        for name, value in extra_required:
            if value is None or value == "" or (isinstance(value, str) and value.strip() == ""):
                return {
                    "success": False,
                    "error": {"code": f"INVALID_{name.upper()}", "message": f"{name} is required."},
                }
            if name in {"size", "price", "start_price", "end_price", "total_size"}:
                d = _dec(value)
                if d is None or d <= 0:
                    return {
                        "success": False,
                        "error": {"code": f"INVALID_{name.upper()}", "message": f"{name} must be a positive number."},
                    }
        return None

    # ---- audit ----------------------------------------------------------

    def _audit(
        self,
        *,
        operation: str,
        exchange: str,
        account: str,
        symbol: str,
        side: str = "",
        preview_id: str = "",
        mode: str,
        status: str,
        accepted: Optional[int] = None,
        requested: Optional[int] = None,
        exchange_order_ids: Optional[List[str]] = None,
        error_code: str = "",
    ) -> None:
        # NEVER log: api_key, private_key, secret, password, signature,
        # session_secret, raw credentials. Only metadata.
        bits = [
            f"op={operation}",
            f"exchange={exchange}",
            f"account={account}",
            f"symbol={symbol}",
            f"mode={mode}",
            f"status={status}",
        ]
        if side:
            bits.append(f"side={side}")
        if preview_id:
            # preview_id is HMAC-signed but does NOT carry secrets; safe.
            bits.append(f"preview_id={preview_id[:24]}…")
        if accepted is not None:
            bits.append(f"accepted={accepted}")
        if requested is not None:
            bits.append(f"requested={requested}")
        if exchange_order_ids:
            bits.append(f"exchange_order_ids={','.join(exchange_order_ids)}")
        if error_code:
            bits.append(f"error_code={error_code}")
        logger.info("webtrade2.phase2 audit " + " ".join(bits))

    # ---- preview_order --------------------------------------------------

    def preview_order(
        self,
        *,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        order_type: str = "limit",
        size: str = "",
        price: str = "",
        market_type: str = "futures",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return {"_http_status": gate[0], **gate[1]}

        cap_err = self._require_capability(exchange, "new_order")
        if cap_err:
            return cap_err
        verr = self._validate_inputs(
            exchange=exchange,
            account=account,
            symbol=symbol,
            side=side,
            extra_required=(("size", size), ("price", price)),
        )
        if verr:
            return verr
        if str(order_type or "limit").lower() != "limit":
            return {
                "success": False,
                "error": {"code": "INVALID_ORDER_TYPE", "message": "Only LIMIT orders are supported."},
            }
        if reduce_only:
            caps = set(self.desk.capabilities(exchange))
            if "reduce_only" not in caps:
                return {
                    "success": False,
                    "error": {"code": "UNSUPPORTED", "message": f"{exchange} does not expose reduce_only."},
                }

        # Round to instrument increments — best-effort: if resolve_instrument
        # is not exposed we still emit a final_price equal to the requested
        # price so the preview is never wider than the user input.
        req_size = _dec(size)
        req_price = _dec(price)
        final_size = req_size
        final_price = req_price
        native_symbol = str(symbol).strip()
        display = native_symbol
        try:
            resp = self.desk.execute({
                "operation": "resolve_instrument",
                "exchange": exchange,
                "account": account,
                "symbol": native_symbol,
            })
            if isinstance(resp, object) and getattr(resp, "success", False):
                # Different agents shape resolved metadata differently; we
                # only surface the canonical native_symbol and display.
                payload = _to_plain(resp) or {}
                if isinstance(payload, dict):
                    inst = payload.get("instrument") or {}
                    native = payload.get("native_symbol")
                    if isinstance(native, str) and native.strip():
                        native_symbol = native.strip()
                    if isinstance(inst, dict):
                        meta = inst.get("format_meta") or inst
                        px_inc = _dec(meta.get("price_increment")) or final_price
                        sz_inc = _dec(meta.get("size_increment")) or final_size
                        if px_inc is not None and final_price is not None and px_inc > 0:
                            final_price = quantize_to_increment(final_price, px_inc)
                        if sz_inc is not None and final_size is not None and sz_inc > 0:
                            final_size = quantize_to_increment(final_size, sz_inc)
                        dpy = inst.get("display") or inst.get("name")
                        if dpy:
                            display = str(dpy)
        except Exception as exc:  # noqa: BLE001
            # resolve_instrument best-effort; fall through.
            logger.debug("resolve_instrument(%s) failed: %s", exchange, exc)
        if final_size is None or final_size <= 0:
            return {"success": False, "error": {"code": "INVALID_SIZE", "message": "Size must be positive."}}
        if final_price is None or final_price <= 0:
            return {"success": False, "error": {"code": "INVALID_PRICE", "message": "Price must be positive."}}

        notional = final_size * final_price
        plan = {
            "kind": "order",
            "exchange": exchange,
            "account": account,
            "market_type": str(market_type or "futures"),
            "requested_symbol": str(symbol).strip(),
            "native_symbol": native_symbol,
            "display": display,
            "side": str(side).strip().lower(),
            "order_type": "limit",
            "requested_price": _fmt_dec(req_price),
            "requested_size": _fmt_dec(req_size),
            "final_price": _fmt_dec(final_price),
            "final_size": _fmt_dec(final_size),
            "notional": _fmt_dec(notional),
            "reduce_only": bool(reduce_only),
        }
        preview_id = self.previews.issue(plan)
        return {
            "success": True,
            "preview_id": preview_id,
            "kind": "order",
            "exchange": exchange,
            "account": account,
            "market_type": plan["market_type"],
            "requested_symbol": plan["requested_symbol"],
            "native_symbol": native_symbol,
            "display": display,
            "side": plan["side"],
            "order_type": "limit",
            "requested_price": plan["requested_price"],
            "final_price": plan["final_price"],
            "requested_size": plan["requested_size"],
            "final_size": plan["final_size"],
            "notional": plan["notional"],
            "reduce_only": bool(reduce_only),
            "expires_in_s": self.previews.ttl_seconds,
            "summary": f"{plan['side'].upper()} {native_symbol} LIMIT @ {plan['final_price']} x {plan['final_size']}",
        }

    # ---- preview_ladder -------------------------------------------------

    def preview_ladder(
        self,
        *,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        distribution: str,
        order_count: Any,
        total_size: str,
        start_price: str,
        end_price: str,
        market_type: str = "futures",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return {"_http_status": gate[0], **gate[1]}
        cap_err = self._require_capability(exchange, "ladder")
        if cap_err:
            return cap_err
        verr = self._validate_inputs(
            exchange=exchange,
            account=account,
            symbol=symbol,
            side=side,
            extra_required=(
                ("start_price", start_price),
                ("end_price", end_price),
                ("total_size", total_size),
            ),
        )
        if verr:
            return verr
        dist = str(distribution or "").strip().lower()
        if dist not in {"uniform", "half_gaussian"}:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED_DISTRIBUTION", "message": "Distribution must be uniform or half_gaussian."},
            }
        try:
            n = int(order_count)
        except (TypeError, ValueError):
            return {"success": False, "error": {"code": "INVALID_ORDER_COUNT", "message": "order_count must be an integer."}}
        if n <= 0 or n > 500:
            return {"success": False, "error": {"code": "INVALID_ORDER_COUNT", "message": "order_count must be 1..500."}}
        sp = _dec(start_price)
        ep = _dec(end_price)
        ts = _dec(total_size)
        if sp is None or ep is None or ts is None or sp <= 0 or ep <= 0 or ts <= 0:
            return {"success": False, "error": {"code": "INVALID_PRICE", "message": "start/end/total must be positive."}}

        # Round prices/sizes to instrument increments (best effort).
        # Without resolve metadata we use safe defaults.
        try:
            resp = self.desk.execute({
                "operation": "resolve_instrument",
                "exchange": exchange,
                "account": account,
                "symbol": str(symbol).strip(),
            })
            payload = _to_plain(resp) or {}
            inst = payload.get("instrument") if isinstance(payload, dict) else None
            meta = (inst or {}).get("format_meta") or inst or {}
            px_inc = _dec(meta.get("price_increment")) or Decimal("0.01")
            sz_inc = _dec(meta.get("size_increment")) or Decimal("0.0001")
        except Exception:
            px_inc = Decimal("0.01")
            sz_inc = Decimal("0.0001")

        children_raw, submitted_total, vwap = build_ladder_children(
            side=str(side).strip().lower(),
            distribution=dist,
            order_count=n,
            total_volume=ts,
            start_price=sp,
            end_price=ep,
            price_increment=px_inc,
            size_increment=sz_inc,
        )
        # Children from ladder_math are dicts {price, size} already.
        children_serialized: List[Dict[str, str]] = []
        for ch in children_raw:
            if isinstance(ch, dict):
                children_serialized.append({
                    "price": str(ch.get("price") or ""),
                    "size": str(ch.get("size") or ""),
                })
        vwap_str = str(vwap) if vwap is not None else ""
        # Compute first-5 / last-5 / ellipsis for the wizard (matches WebTrade's display).
        if len(children_serialized) > 10:
            display_children = children_serialized[:5] + [{"ellipsis": True}] + children_serialized[-5:]
        else:
            display_children = children_serialized

        plan = {
            "kind": "ladder",
            "exchange": exchange,
            "account": account,
            "market_type": str(market_type or "futures"),
            "requested_symbol": str(symbol).strip(),
            "native_symbol": str(symbol).strip(),
            "side": str(side).strip().lower(),
            "distribution": dist,
            "order_count": n,
            "requested_total_size": _fmt_dec(ts),
            "start_price": _fmt_dec(sp),
            "end_price": _fmt_dec(ep),
            "vwap": vwap_str,
            "children": children_serialized,
            "exec": {
                "symbol": str(symbol).strip(),
                "side": str(side).strip().lower(),
                "distribution": dist,
                "order_count": n,
                "total_volume": _fmt_dec(ts),
                "start_price": _fmt_dec(sp),
                "end_price": _fmt_dec(ep),
            },
            "reduce_only": bool(reduce_only),
        }
        preview_id = self.previews.issue(plan)
        return {
            "success": True,
            "preview_id": preview_id,
            "kind": "ladder",
            "exchange": exchange,
            "account": account,
            "market_type": plan["market_type"],
            "requested_symbol": plan["requested_symbol"],
            "native_symbol": plan["native_symbol"],
            "side": plan["side"],
            "distribution": dist,
            "requested_order_count": n,
            "order_count": n,
            "total_size": _fmt_dec(ts),
            "requested_total_size": _fmt_dec(ts),
            "start_price": _fmt_dec(sp),
            "end_price": _fmt_dec(ep),
            "vwap": plan["vwap"],
            "children": children_serialized,
            "display_children": display_children,
            "reduce_only": bool(reduce_only),
            "expires_in_s": self.previews.ttl_seconds,
            "summary": (
                f"{plan['side'].upper()} {plan['native_symbol']} LADDER · {dist} · "
                f"{n} orders · VWAP {plan['vwap']}"
            ),
        }

    # ---- execute_preview ------------------------------------------------

    def execute_preview(self, preview_id: str) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return {"_http_status": gate[0], **gate[1]}
        plan, err = self.previews.consume(str(preview_id or ""))
        if err or not plan:
            return {
                "success": False,
                "error": {
                    "code": err or "PREVIEW_INVALID",
                    "message": {
                        "PREVIEW_EXPIRED": "Preview expired. Generate a new preview.",
                        "PREVIEW_CONSUMED": "Preview already used. Generate a new preview.",
                        "PREVIEW_INVALID": "Invalid preview. Generate a new preview.",
                    }.get(err or "", "Invalid preview."),
                },
            }
        exchange = str(plan.get("exchange") or "")
        account = str(plan.get("account") or "")
        kind = str(plan.get("kind") or "")
        side = str(plan.get("side") or "")
        symbol = str(plan.get("native_symbol") or plan.get("requested_symbol") or "")

        # Dry-run branch — stop BEFORE desk.execute for any write op.
        if self.dry_run:
            status = "DRY_RUN"
            out: Dict[str, Any] = {
                "success": True,
                "status": status,
                "kind": kind,
                "exchange": exchange,
                "account": account,
                "market_type": plan.get("market_type") or "futures",
                "symbol": symbol,
                "side": side,
                "mode": "DRY_RUN",
                "message": "DRY_RUN: validated successfully; no exchange write was performed.",
                "accepted": int(plan.get("order_count") or 1) if kind == "ladder" else 1,
                "requested": int(plan.get("order_count") or 1) if kind == "ladder" else 1,
                "partial": False,
                "exchange_order_ids": [],
            }
            if kind == "order":
                out["final_price"] = plan.get("final_price")
                out["final_size"] = plan.get("final_size")
                out["notional"] = plan.get("notional")
            if kind == "ladder":
                out["distribution"] = plan.get("distribution")
                out["preview_vwap"] = plan.get("vwap")
            self._audit(
                operation=kind,
                exchange=exchange,
                account=account,
                symbol=symbol,
                side=side,
                preview_id=preview_id,
                mode="DRY_RUN",
                status=status,
                accepted=out["accepted"],
                requested=out["requested"],
            )
            return out

        # Live branch: dispatch through TradeDesk using the plan's
        # final normalized values (never the browser's raw inputs).
        if kind == "order":
            req = {
                "operation": "new_order",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
                "side": side,
                "order_type": "limit",
                "volume": plan.get("final_size"),
                "price": plan.get("final_price"),
            }
            t0 = time.perf_counter()
            resp = self.desk.execute(req)
            desk_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            ok = bool(getattr(resp, "success", False))
            order = _to_plain(getattr(resp, "order", None))
            eids: List[str] = []
            if isinstance(order, dict):
                oid = order.get("order_id") or order.get("id")
                if oid:
                    eids.append(str(oid))
            status = "SUBMITTED" if ok else "REJECTED"
            if not ok:
                status = "REJECTED"
            out = {
                "success": ok,
                "status": status,
                "kind": "order",
                "operation": "new_order",
                "exchange": exchange,
                "account": account,
                "market_type": plan.get("market_type") or "futures",
                "symbol": symbol,
                "side": side,
                "final_price": plan.get("final_price"),
                "final_size": plan.get("final_size"),
                "notional": plan.get("notional"),
                "exchange_order_ids": eids,
                "requested": 1,
                "accepted": 1 if ok else 0,
                "partial": False,
                "mode": "LIVE",
                "timing_ms": {"tradedesk_ms": desk_ms},
            }
            if ok:
                out["message"] = f"{side.upper()} {symbol} LIMIT submitted."
                self._audit(
                    operation="new_order",
                    exchange=exchange,
                    account=account,
                    symbol=symbol,
                    side=side,
                    preview_id=preview_id,
                    mode="LIVE",
                    status=status,
                    accepted=1,
                    requested=1,
                    exchange_order_ids=eids,
                )
            else:
                err_obj = _safe_error(resp)
                out["error"] = err_obj
                status = "REJECTED"
                out["status"] = status
                self._audit(
                    operation="new_order",
                    exchange=exchange,
                    account=account,
                    symbol=symbol,
                    side=side,
                    preview_id=preview_id,
                    mode="LIVE",
                    status=status,
                    accepted=0,
                    requested=1,
                    error_code=err_obj.get("code", ""),
                )
            if order:
                out["order"] = order
            return out

        if kind == "ladder":
            exec_body = plan.get("exec") or {}
            req = {
                "operation": "ladder",
                "exchange": exchange,
                "account": account,
                "symbol": exec_body.get("symbol") or symbol,
                "side": exec_body.get("side") or side,
                "distribution": exec_body.get("distribution"),
                "order_count": exec_body.get("order_count"),
                "total_volume": exec_body.get("total_volume"),
                "start_price": exec_body.get("start_price"),
                "end_price": exec_body.get("end_price"),
                "preview_children": plan.get("children"),
            }
            t0 = time.perf_counter()
            resp = self.desk.execute(req)
            desk_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            ok = bool(getattr(resp, "success", False))
            ladder = _to_plain(getattr(resp, "ladder", None))
            req_n = int(plan.get("order_count") or 0)
            accepted = 0
            if isinstance(ladder, dict):
                accepted = int(ladder.get("accepted_child_count") or ladder.get("submitted_order_count") or 0)
            partial = bool(isinstance(ladder, dict) and ladder.get("partial")) or (accepted < req_n)
            if not ok and accepted == 0:
                status = "REJECTED"
            elif partial:
                status = "PARTIALLY_SUBMITTED"
            else:
                status = "SUBMITTED"
            out = {
                "success": ok or accepted > 0,
                "status": status,
                "kind": "ladder",
                "operation": "ladder",
                "exchange": exchange,
                "account": account,
                "market_type": plan.get("market_type") or "futures",
                "symbol": symbol,
                "side": side,
                "distribution": plan.get("distribution"),
                "requested": req_n,
                "accepted": accepted,
                "partial": partial,
                "preview_vwap": plan.get("vwap"),
                "preview_order_count": req_n,
                "exchange_order_ids": [],
                "mode": "LIVE",
                "timing_ms": {"tradedesk_ms": desk_ms},
                "message": (
                    f"{accepted} of {req_n} orders accepted. No automatic retry performed."
                    if partial
                    else (f"Ladder submitted ({accepted} orders)." if ok else "Ladder rejected.")
                ),
            }
            if ladder:
                out["ladder"] = ladder
            if ok:
                self._audit(
                    operation="ladder",
                    exchange=exchange,
                    account=account,
                    symbol=symbol,
                    side=side,
                    preview_id=preview_id,
                    mode="LIVE",
                    status=status,
                    accepted=accepted,
                    requested=req_n,
                )
            else:
                err_obj = _safe_error(resp)
                out["error"] = err_obj
                # If the agent signalled a partial even with success=False,
                # we surface PARTIALLY_SUBMITTED with no retry.
                self._audit(
                    operation="ladder",
                    exchange=exchange,
                    account=account,
                    symbol=symbol,
                    side=side,
                    preview_id=preview_id,
                    mode="LIVE",
                    status=status,
                    accepted=accepted,
                    requested=req_n,
                    error_code=err_obj.get("code", ""),
                )
            return out

        return {"success": False, "error": {"code": "PREVIEW_INVALID", "message": "Unknown preview kind."}}

    # ---- one-shot write operations (TP / SL / Close / Cancel group) ----

    def _one_shot(
        self,
        *,
        operation: str,
        exchange: str,
        account: str,
        symbol: str,
        side: str = "",
        extra: Optional[Dict[str, Any]] = None,
        preview_kind: str,
    ) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return {"_http_status": gate[0], **gate[1]}
        verr = self._validate_inputs(exchange=exchange, account=account, symbol=symbol, side=side or "buy")
        if verr:
            return verr
        cap_err = self._require_capability(exchange, operation)
        if cap_err:
            return cap_err

        if self.dry_run:
            status = "DRY_RUN"
            out: Dict[str, Any] = {
                "success": True,
                "status": status,
                "operation": operation,
                "kind": preview_kind,
                "exchange": exchange,
                "account": account,
                "market_type": "futures",
                "symbol": symbol,
                "side": side,
                "mode": "DRY_RUN",
                "exchange_order_ids": [],
                "message": "DRY_RUN: validated successfully; no exchange write was performed.",
            }
            if operation == "cancel_order_group":
                out["requested"] = len((extra or {}).get("order_ids") or []) or 1
                out["accepted"] = out["requested"]
            else:
                out["requested"] = 1
                out["accepted"] = 1
            self._audit(
                operation=operation,
                exchange=exchange,
                account=account,
                symbol=symbol,
                side=side,
                mode="DRY_RUN",
                status=status,
                accepted=out["accepted"],
                requested=out["requested"],
            )
            return out

        req: Dict[str, Any] = {
            "operation": operation,
            "exchange": exchange,
            "account": account,
            "symbol": symbol,
        }
        if side:
            req["side"] = side
        if extra:
            req.update(extra)
        t0 = time.perf_counter()
        resp = self.desk.execute(req)
        desk_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        ok = bool(getattr(resp, "success", False))
        status = "SUBMITTED" if ok else "REJECTED"
        out = {
            "success": ok,
            "status": status,
            "operation": operation,
            "kind": preview_kind,
            "exchange": exchange,
            "account": account,
            "market_type": "futures",
            "symbol": symbol,
            "side": side,
            "mode": "LIVE",
            "timing_ms": {"tradedesk_ms": desk_ms},
            "requested": 1,
            "accepted": 1 if ok else 0,
        }
        if not ok:
            err_obj = _safe_error(resp)
            out["error"] = err_obj
            out["status"] = "REJECTED"
            self._audit(
                operation=operation,
                exchange=exchange,
                account=account,
                symbol=symbol,
                side=side,
                mode="LIVE",
                status="REJECTED",
                accepted=0,
                requested=1,
                error_code=err_obj.get("code", ""),
            )
        else:
            self._audit(
                operation=operation,
                exchange=exchange,
                account=account,
                symbol=symbol,
                side=side,
                mode="LIVE",
                status=status,
                accepted=1,
                requested=1,
            )
        # For cancel group, surface counts.
        cg = _to_plain(getattr(resp, "cancel_group", None))
        if cg and isinstance(cg, dict):
            out["cancel_group"] = cg
            cancelled = int(cg.get("cancelled_order_count") or 0)
            targeted = int(cg.get("targeted_order_count") or cancelled)
            out["requested"] = targeted or 1
            out["accepted"] = cancelled
            if targeted and cancelled < targeted:
                out["partial"] = True
                out["status"] = "PARTIALLY_SUBMITTED"
                out["message"] = f"Cancelled {cancelled}/{targeted} orders; no automatic retry performed."
            else:
                out["message"] = f"Cancelled {cancelled} orders."
        return out

    # public wrappers -----------------------------------------------------

    def set_tp(self, exchange: str, account: str, symbol: str, price: str) -> Dict[str, Any]:
        verr = self._validate_inputs(exchange=exchange, account=account, symbol=symbol, side="buy",
                                     extra_required=(("price", price),))
        if verr:
            return verr
        return self._one_shot(operation="set_tp", exchange=exchange, account=account,
                              symbol=symbol, side="", extra={"price": str(price).strip()},
                              preview_kind="set_tp")

    def set_sl(self, exchange: str, account: str, symbol: str, price: str) -> Dict[str, Any]:
        verr = self._validate_inputs(exchange=exchange, account=account, symbol=symbol, side="buy",
                                     extra_required=(("price", price),))
        if verr:
            return verr
        return self._one_shot(operation="set_sl", exchange=exchange, account=account,
                              symbol=symbol, side="", extra={"price": str(price).strip()},
                              preview_kind="set_sl")

    def close_position(self, exchange: str, account: str, symbol: str) -> Dict[str, Any]:
        return self._one_shot(operation="close_position", exchange=exchange, account=account,
                              symbol=symbol, side="", extra={}, preview_kind="close_position")

    def cancel_order_group(
        self,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        order_type: str = "limit",
        order_ids: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        verr = self._validate_inputs(exchange=exchange, account=account, symbol=symbol, side=side)
        if verr:
            return verr
        extra: Dict[str, Any] = {"order_type": str(order_type or "limit")}
        if order_ids:
            extra["order_ids"] = list(order_ids)
        return self._one_shot(operation="cancel_order_group", exchange=exchange, account=account,
                              symbol=symbol, side=str(side).strip().lower(),
                              extra=extra, preview_kind="cancel_group")


__all__ = ["WebTrade2Phase2Service"]
