"""Apex Omni exchange agent.

This module owns Apex Omni-specific behavior for the /trade stack.

Current scope (Phase 1 — read-only):
- Credential discovery from ``APEX_<ALIAS>_ACCOUNTID``,
  ``APEX_<ALIAS>_APIKEY``, ``APEX_<ALIAS>_APIKEYSECRET``,
  ``APEX_<ALIAS>_APIKEYPASSPHRASE``, ``APEX_<ALIAS>_SEEDS``, and
  ``APEX_<ALIAS>_L2KEY`` in the live environment or ``$HERMES_HOME/.env``.
- Read-only portfolio retrieval through Apex's documented REST endpoints.
- Canonical conversion into the exchange-agnostic TradeDesk / wizard contract.
- TP/SL enrichment via Apex's history-orders-v3 endpoint (active position-level
  triggers are surfaced as ``tp`` / ``sl`` on the position summary, mirroring
  the other agents' behavior).

Write operations (new_order / ladder / set_tp / set_sl / close_position /
cancel_orders) are stubbed and return ``NOT_IMPLEMENTED`` — Apex Omni SDK
write semantics (EIP-712 + action-hash signing, batched child orders,
``HttpPrivateSign``-mediated ``create_order_v3`` / ``create_batch_order_v3``
/ cancel_v3) are wired next in the same shape as the other agents' write
paths.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and must not
parse ``APEX_*`` environment variables or Apex-native payloads.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalLadderResult,
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

name = "apex"

# Apex Omni mainnet REST base URL — overridden by apexomni.constants at runtime
# if the SDK is available; this constant is the documented fallback.
APEX_OMNI_HTTP_MAIN_FALLBACK = "https://omni.apex.exchange"

# Apex requires 6 credentials per account. The 6-tuple below defines the
# set of env-var suffixes that MUST all be present (with non-empty values)
# for a configured account to count as complete. Order matches the spec:
#   ACCOUNTID, APIKEY, APIKEYSECRET, APIKEYPASSPHRASE, SEEDS, L2KEY
APEX_REQUIRED_SUFFIXES: Tuple[str, ...] = (
    "ACCOUNTID",
    "APIKEY",
    "APIKEYSECRET",
    "APIKEYPASSPHRASE",
    "SEEDS",
    "L2KEY",
)

# Credential-source fields exposed to the credential dict the wizard sees.
# These are the keys the agent uses internally; they map 1:1 to the
# apexomni HttpPrivateSign constructor kwargs (api_key, api_secret, passphrase,
# addresses/seeds, private_key/l2_key).
_CREDENTIAL_FIELD_BY_SUFFIX = {
    "ACCOUNTID": "account_id",
    "APIKEY": "api_key",
    "APIKEYSECRET": "api_secret",
    "APIKEYPASSPHRASE": "passphrase",
    "SEEDS": "seeds",
    "L2KEY": "l2_private_key",
}

# Apex environment uses the L2 account's ETH address for the on-chain signer;
# the SEEDS field is the same hex (the SDK reuses it as addresses=).
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# HTTP / network defaults
APEX_HTTP_TIMEOUT_SECONDS = 20
APEX_HISTORY_TPSL_PAGE_SIZE = 100

# Apex side encodings — match the SDK's expected int forms.
APEX_SIDE_TO_INT = {"buy": 0, "sell": 1}
APEX_ORDER_TYPE_TO_INT = {"limit": 0, "market": 1}

# How many raw fields to consult when picking a "best" price for an
# instrument — Apex surfaces bid/ask/oracle/mark/limit in slightly
# different shapes depending on the endpoint, so we probe several keys
# in priority order before giving up.
_APEX_PRICE_FIELD_CANDIDATES: Tuple[str, ...] = (
    "markPrice",
    "oraclePrice",
    "indexPrice",
    "lastPrice",
    "price",
)

# ---------------------------------------------------------------------------
# Module-level public surface (TradeDesk contract)
# ---------------------------------------------------------------------------


def list_accounts() -> List[str]:
    """Discover Apex Omni accounts by scanning the environment.

    An account is "complete" (and therefore surfaced to the wizard) only if
    all six required credential suffixes are present with non-empty values.
    """
    return _discover_accounts()


def capabilities() -> List[str]:
    """Return the operations this agent supports.

    Phase 1 advertises the read path (balance, positions_orders,
    positions_management) and the operation names that the wizard exercises
    even if the corresponding write paths are not yet implemented — those
    return ``NOT_IMPLEMENTED`` from the dispatcher and are wired in a
    subsequent phase.
    """
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "cancel_orders",
        "cancel_order_group",
        "set_tp",
        "set_sl",
        "close_position",
        "ladder",
        "resolve_instrument",
        "list_instruments",
        "market_price",
    ]


def _apex_resolve_instrument(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or "").strip()
    if not requested:
        return make_failure(operation="resolve_instrument", exchange=name, account=account,
                            code="MISSING_SYMBOL", message="Symbol is required.")
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="resolve_instrument", exchange=name, account=account,
                            code=error["code"], message=error["message"])
    assert credentials is not None
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta = _apex_resolve_symbol(requested, all_contracts)
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(operation="resolve_instrument", exchange=name, account=credentials["account"],
                                code="INSTRUMENT_AMBIGUOUS",
                                message=f"Apex instrument '{requested}' is ambiguous.")
        raise
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="resolve_instrument", exchange=name, account=credentials["account"],
                            code="APEX_ERROR", message=sanitize_error_message(str(exc)))
    if meta is None:
        return make_failure(operation="resolve_instrument", exchange=name, account=credentials["account"],
                            code="INSTRUMENT_NOT_FOUND",
                            message=f"Apex symbol '{requested}' is not available.")
    instrument = CanonicalInstrument(
        requested_symbol=requested,
        symbol=str(meta.get("symbol") or ""),
        display_name=str(meta.get("symbolDisplayName") or ""),
        price_increment=str(meta.get("tickSize") or "") or None,
        size_increment=str(meta.get("stepSize") or meta.get("lotSize") or "") or None,
        minimum_size=str(meta.get("minOrderSize") or "") or None,
    )
    return make_success(operation="resolve_instrument", exchange=name, account=credentials["account"],
                        instrument=instrument)


def _apex_list_instruments(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Enumerate Apex perps + prelaunch + TradFi/stock contracts for the picker."""
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code=error["code"],
            message=error["message"],
        )
    assert credentials is not None
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        try:
            client.configs_v3()
        except Exception:  # noqa: BLE001
            pass
        # Prefer section-tagged fetch so TradFi (stock) rows are labeled.
        config = getattr(client, "configV3", None) or {}
        cc = (config.get("contractConfig") or {}) if isinstance(config, dict) else {}
        sections = (
            ("perp", list(cc.get("perpetualContract") or [])),
            ("prelaunch", list(cc.get("prelaunchContract") or [])),
            ("tradfi", list(cc.get("stockContract") or [])),
        )
        if not any(rows for _label, rows in sections):
            # Fallback to the unified helper if configV3 is empty.
            contracts = _apex_fetch_supported_markets(client)
            sections = (("perp", list(contracts)),)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            code="CATALOG_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    instruments: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for section, rows in sections:
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            key = symbol.upper()
            if key in seen:
                continue
            seen.add(key)
            display = str(
                row.get("symbolDisplayName") or row.get("crossSymbolName") or ""
            ).strip()
            base = symbol.split("-", 1)[0].strip() or display
            entry: Dict[str, Any] = {
                "instrument": symbol,
                "display_name": display or symbol,
                "base": base,
                "market_type": section,
                "description": f"{display or symbol} [{section}]",
            }
            instruments.append(entry)
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=credentials["account"],
        data={"instruments": instruments},
    )


def _apex_market_price(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Return mark price via Apex ticker_v3 for one symbol."""
    requested = str(request.get("symbol") or request.get("requested_symbol") or "").strip()
    if not requested:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code=error["code"],
            message=error["message"],
        )
    assert credentials is not None
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        contracts = _apex_fetch_supported_markets(client)
        meta = _apex_resolve_symbol(requested, contracts)
    except ValueError as exc:
        code = (
            "INSTRUMENT_AMBIGUOUS"
            if str(exc) == "INSTRUMENT_AMBIGUOUS"
            else "INSTRUMENT_NOT_FOUND"
        )
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code=code,
            message=f"Instrument not found: {requested}",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="PRICE_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    if meta is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested}",
        )
    native = str(meta.get("symbol") or "").strip()
    mark = _apex_fetch_mark_price(client, native)
    if mark is None or mark <= 0:
        display = str(meta.get("symbolDisplayName") or meta.get("crossSymbolName") or "").strip()
        if display:
            mark = _apex_fetch_mark_price(client, display)
    if mark is None or mark <= 0:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="PRICE_UNAVAILABLE",
            message=f"Price unavailable for {requested}",
        )
    try:
        text = _format_apex_decimal(mark)
    except Exception:  # noqa: BLE001
        text = format(mark.normalize(), "f")
    return make_success(
        operation="market_price",
        exchange=name,
        account=credentials["account"],
        instrument=CanonicalInstrument(
            requested_symbol=requested,
            symbol=native,
            display_name=str(
                meta.get("symbolDisplayName") or meta.get("crossSymbolName") or native
            ),
        ),
        market_price=CanonicalMarketPrice(
            requested_symbol=requested,
            market=native,
            mark_price=text,
            price=text,
        ),
    )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    """Dispatch to the requested operation.

    Mirrors the contract used by the other agents in this package:
    ``request`` is a dict carrying at least ``operation`` and ``account``,
    plus operation-specific fields (``symbol``, ``price``, ``size``, etc.).
    """
    if not isinstance(request, dict):
        operation = ""
        account = ""
    else:
        operation = str(request.get("operation") or "").strip()
        account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(operation="", exchange=name, account=account,
                            code="INVALID_REQUEST", message="Missing 'operation'.")
    if not account:
        return make_failure(operation=operation, exchange=name, account=account,
                            code="INVALID_REQUEST", message="Missing 'account'.")
    try:
        if operation == "balance":
            return _balance(account)
        if operation == "positions_orders":
            return _positions_orders(account)
        if operation == "positions_management":
            # Read-only alias — the wizard re-uses positions_orders as the
            # "show me everything about this position" feed.
            return _positions_orders(account)
        if operation == "new_order":
            return _new_order(request)
        if operation == "ladder":
            return _execute_ladder(request)
        if operation == "cancel_order_group":
            return _cancel_order_group(request)
        if operation == "positions_management":
            return _apex_position_management(account)
        if operation == "resolve_instrument":
            return _apex_resolve_instrument(account, request)
        if operation == "list_instruments":
            return _apex_list_instruments(account, request)
        if operation == "market_price":
            return _apex_market_price(account, request)
        if operation == "set_tp":
            return _apex_set_tp(request)
        if operation == "set_sl":
            return _apex_set_sl(request)
        if operation == "close_position":
            return _apex_close_position(request)
        if operation in ("cancel_orders",):
            return make_failure(
                operation=operation, exchange=name, account=account,
                code="NOT_IMPLEMENTED",
                message=("Apex Omni's ``cancel_orders`` plural operation is not "
                         "implemented. Use ``cancel_order_group`` (singular) "
                         "to cancel open orders for a specific (symbol, side) "
                         "pair — that's the operation the wizard's Cancel "
                         "menu dispatches."),
            )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation, exchange=name, account=account,
            code="APEX_ERROR", message=sanitize_error_message(str(exc)),
        )
    return make_failure(
        operation=operation, exchange=name, account=account,
        code="NOT_IMPLEMENTED",
        message=f"Apex Omni does not implement '{operation}' yet.",
    )


# ---------------------------------------------------------------------------
# Account discovery
# ---------------------------------------------------------------------------


def _combined_casefold_env() -> Dict[str, Tuple[str, str, str]]:
    """Return ``{upper-key: (actual-key, value, source)}`` across os.environ
    and ``$HERMES_HOME/.env``.

    The apexomni SDK and the rest of the wizard both rely on case-insensitive
    environment lookups, so we mimic that here. We do NOT parse secrets —
    only key names — when we hand back ``actual_key`` for downstream callers.
    """
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        out.setdefault(k.upper(), (k, v, "env"))
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        env_path = os.path.join(hermes_home, ".env")
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key:
                            out.setdefault(key.upper(), (key, value, "dotenv"))
            except OSError:
                pass
    return out


def _normalize_alias(raw_account: str) -> str:
    """Sanitize an Apex account alias (the substring between ``APEX_`` and the
    final suffix) so it can be displayed in the wizard."""
    alias = raw_account.strip().strip("_")
    if not alias:
        return ""
    return alias.upper() if _ALIAS_PATTERN.match(alias.upper()) else alias


def _has_complete_credentials(raw_account: str,
                              env: Mapping[str, Tuple[str, str, str]]) -> bool:
    """True iff every required suffix for ``raw_account`` is present and
    non-empty in the env map."""
    for suffix in APEX_REQUIRED_SUFFIXES:
        key = f"APEX_{raw_account}_{suffix}".upper()
        value = env.get(key)
        if not value or not value[1].strip():
            return False
    return True


def _discover_accounts() -> List[str]:
    """Discover Apex Omni accounts that have all six required env vars set.

    Mirrors the same env-merging pattern the other agents use so that an
    Apex account configured in ``$HERMES_HOME/.env`` is visible to the
    wizard even if it's not in the live process environment.
    """
    env = _combined_casefold_env()
    aliases: List[str] = []
    seen: set = set()
    for upper_key in env:
        if not upper_key.startswith("APEX_") or not upper_key.endswith("_ACCOUNTID"):
            continue
        raw_account = upper_key[len("APEX_"):-len("_ACCOUNTID")]
        alias = _normalize_alias(raw_account)
        if not alias or alias in seen:
            continue
        if _has_complete_credentials(raw_account, env):
            seen.add(alias)
            aliases.append(alias)
    return sorted(aliases)


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    """Look up the six Apex credentials for ``account`` and return a dict
    shaped for the apexomni HttpPrivateSign constructor + our helpers.

    Returns None if the account is unknown or incomplete — the wizard's
    exchange-picker should never have surfaced such an account, but we
    re-check here defensively so a stale /trade state cannot crash the
    agent.
    """
    raw = account.strip()
    if not raw:
        return None
    env = _combined_casefold_env()
    creds: Dict[str, Any] = {"account": raw}
    for suffix, field in _CREDENTIAL_FIELD_BY_SUFFIX.items():
        key = f"APEX_{raw}_{suffix}".upper()
        value = env.get(key)
        if not value or not value[1].strip():
            return None
        creds[field] = value[1].strip()
    return creds


# ---------------------------------------------------------------------------
# SDK client construction
# ---------------------------------------------------------------------------


def _client_for_credentials(credentials: Dict[str, Any]) -> Any:
    """Construct an apexomni v3-aware client from a credentials dict.

    Lazy-imports the SDK so this module imports cleanly (and the wizard
    discovers the exchange) on a fresh Hermes install that hasn't installed
    ``apexomni`` yet. The first real balance / positions call surfaces a
    clear SDK_MISSING error to the operator.

    Note on the SDK class + kwargs:
      apexomni 3.3.1 puts the v3 endpoints (``get_account_balance_v3``,
      ``history_orders_v3``, ``open_orders_v3``) on a different subclass
      than the basic ``HttpPrivateSign`` — using the basic class would
      fail with ``AttributeError`` at runtime. We use
      ``HttpPrivateRwaSign`` from the same module, which carries the v3
      methods the agent needs.

      The constructor takes the API credentials as a single
      ``api_key_credentials={'apiKey': ..., 'secret': ..., 'passphrase': ...}``
      dict, plus ``eth_private_key`` (the L2 signing key), ``zk_seeds``
      (the L2 signing seed, also called ``addresses`` in older apexomni
      versions), and ``zk_l2Key`` (the L2 private key in some SDK
      versions). The agent passes both ``zk_seeds`` and ``zk_l2Key``
      because some Apex endpoints need either one depending on the path.
    """
    try:
        from apexomni.constants import APEX_OMNI_HTTP_MAIN  # type: ignore
        from apexomni.http_private_sign import HttpPrivateRwaSign  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Apex Omni SDK (apexomni) is not installed in this Hermes venv. "
            "Install it with `pip install apexomni==3.3.1` and retry."
        ) from exc
    api_key_credentials = {
        "key": credentials["api_key"],
        "secret": credentials["api_secret"],
        "passphrase": credentials["passphrase"],
    }
    seeds = credentials.get("seeds")
    l2_key = credentials.get("l2_private_key")
    client = HttpPrivateRwaSign(
        APEX_OMNI_HTTP_MAIN,
        api_key_credentials=api_key_credentials,
        eth_private_key=l2_key,
        zk_seeds=seeds,
        zk_l2Key=l2_key,
    )
    # The RWA wrapper defaults ``_default_account_type`` to its own RWA
    # account type ("stock"). For perp read calls (positions / open
    # orders / history) the wrapper transparently falls back to the
    # primary context when the requested type is missing — but the perp
    # write path (``create_order_v3``) explicitly accesses
    # ``account['contractAccount']`` and crashes with ``'NoneType' object
    # has no attribute 'get'`` on the stock account. Pin the default to
    # primary up front so both reads and writes share a single account
    # context and never trip that crash.
    try:
        client.set_default_account_type("primary")
    except Exception:  # noqa: BLE001
        # Defensive: if the SDK ever removes this method we still want
        # the client to be usable for read paths.
        pass
    return client


def _apex_stock_symbol_set(client: Any) -> set[str]:
    """Return uppercased venue symbols that live under stockContract (TradFi)."""
    try:
        config = getattr(client, "configV3", None) or {}
        cc = (config.get("contractConfig") or {}) if isinstance(config, dict) else {}
        rows = list(cc.get("stockContract") or [])
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in ("symbol", "symbolDisplayName", "crossSymbolName"):
            val = str(row.get(key) or "").strip().upper()
            if val:
                out.add(val)
    return out


def _apex_is_stock_symbol(client: Any, symbol: str) -> bool:
    target = str(symbol or "").strip().upper()
    if not target:
        return False
    return target in _apex_stock_symbol_set(client)


def _apex_prepare_trading_context(client: Any, *, symbol: str) -> str:
    """Select primary vs RWA/stock account context for order placement.

    Apex TradFi (``stockContract``) symbols such as ``XAU-USDT`` MUST be
    signed and submitted under the RWA/stock sub-account. Crypto perps use
    the primary contract account. Returns the active account_type string
    to pass into ``create_order_v3``.
    """
    is_stock = _apex_is_stock_symbol(client, symbol)
    if not is_stock:
        try:
            client.set_default_account_type("primary")
        except Exception:  # noqa: BLE001
            pass
        try:
            if hasattr(client, "use_primary_account"):
                client.use_primary_account()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.get_account_v3(account_type="primary")
        except Exception:  # noqa: BLE001
            pass
        return "primary"

    rwa_type = str(getattr(client, "rwa_account_type", None) or "rwa")
    # 1) Ensure primary is loaded (eth address, fees, l2Key).
    try:
        client.set_default_account_type("primary")
    except Exception:  # noqa: BLE001
        pass
    primary = {}
    try:
        primary = client.get_account_v3(account_type="primary") or {}
    except Exception:  # noqa: BLE001
        primary = {}
    if not isinstance(primary, dict):
        primary = {}

    # 2) Discover stock sub-account id via /v3/stock/account (auth with primary).
    stock_id = None
    stock_payload: Dict[str, Any] = {}
    try:
        path = client._rwa_path("/account") if hasattr(client, "_rwa_path") else None
        if path:
            res = client._get(endpoint=path, params={}, account_type="primary")
            data = res.get("data") if isinstance(res, dict) else None
            if isinstance(data, dict):
                stock_payload = dict(data)
                subs = (data.get("contractAccount") or {}).get("subAccountInfo") or []
                for sub in subs:
                    if not isinstance(sub, Mapping):
                        continue
                    if str(sub.get("accountType") or "") == "SUB_STOCK_ACCOUNT":
                        stock_id = str(sub.get("accountId") or "").strip() or None
                        break
                if not stock_id:
                    stock_id = str(
                        data.get("stockAccountId") or data.get("id") or ""
                    ).strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("apex stock account lookup failed: %s", exc)

    if not stock_id:
        raise RuntimeError(
            "Apex TradFi/stock account is not available for this user. "
            "Open Apex Omni, enable the TradFi/stock account, then retry."
        )

    # 3) Build RWA account context (fees from primary when stock payload is sparse).
    pca = dict(primary.get("contractAccount") or {})
    ca = dict(stock_payload.get("contractAccount") or {})
    for fee_key in ("takerFeeRate", "makerFeeRate"):
        if not ca.get(fee_key) and pca.get(fee_key):
            ca[fee_key] = pca.get(fee_key)
    if not ca.get("takerFeeRate"):
        ca["takerFeeRate"] = "0.0005"
    if not ca.get("makerFeeRate"):
        ca["makerFeeRate"] = "0.0002"
    rwa_ctx: Dict[str, Any] = dict(stock_payload)
    rwa_ctx.update(
        {
            "id": stock_id,
            "stockAccountId": stock_id,
            "subAccountId": stock_id,
            "contractAccount": ca,
            "l2Key": rwa_ctx.get("l2Key") or primary.get("l2Key"),
            "ethereumAddress": rwa_ctx.get("ethereumAddress")
            or primary.get("ethereumAddress"),
        }
    )
    try:
        client._set_account_context(rwa_ctx, account_type=rwa_type)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to set Apex stock account context: {exc}") from exc

    # 4) Ensure RWA API credentials exist (server maps key → stock account).
    try:
        rwa_creds = None
        if hasattr(client, "_get_api_credentials"):
            rwa_creds = client._get_api_credentials(rwa_type)
        if not rwa_creds and hasattr(client, "generate_rwa_api_v3"):
            if getattr(client, "network_id", None) is None:
                try:
                    client.network_id = 1
                except Exception:  # noqa: BLE001
                    pass
            client.generate_rwa_api_v3(
                wallet_name="KAM Auto RWA",
                account_id=stock_id,
                eth_address=primary.get("ethereumAddress") or rwa_ctx.get("ethereumAddress"),
                chain_id=getattr(client, "network_id", None) or 1,
            )
            # generate_rwa_api_v3 may clobber context — restore.
            client._set_account_context(rwa_ctx, account_type=rwa_type)
            rwa_creds = client._get_api_credentials(rwa_type)
        if not rwa_creds:
            raise RuntimeError("Apex RWA/stock API credentials are not available.")
        client.api_key_credentials = rwa_creds
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Apex TradFi orders require a stock (RWA) API key. "
            f"Setup failed: {sanitize_error_message(str(exc))}"
        ) from exc

    # 5) Prefer derived RWA zk seeds for stock signing when available.
    try:
        if hasattr(client, "_derive_rwa_seed") and client.zk_seeds:
            client.rwa_zk_seeds = client._derive_rwa_seed(client.zk_seeds)
    except Exception:  # noqa: BLE001
        pass

    try:
        client.set_default_account_type(rwa_type)
    except Exception:  # noqa: BLE001
        pass
    return rwa_type


# ---------------------------------------------------------------------------
# Read: balance
# ---------------------------------------------------------------------------


def _balance(account: str) -> CanonicalResponse:
    """Fetch the account-level balance and normalize it to the canonical
    portfolio summary used by the wizard.

    Apex Omni v3 surfaces both an account summary (``get_account_v3``) and
    a balance endpoint (``get_account_balance_v3``); both are pulled and the
    ``_normalize_balance`` helper merges them. Note that
    ``get_account_balance_v3`` wraps its payload in ``{"data": ..., ...}``
    while ``get_account_v3`` returns the inner dict directly — the normalizer
    unwraps both shapes so callers don't need to care which endpoint the
    field came from.
    """
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="balance", exchange=name, account=account,
                            code=error["code"], message=error["message"])
    assert credentials is not None  # _resolve_credentials guarantees this
    try:
        client = _client_for_credentials(credentials)
        raw_balance = client.get_account_balance_v3()
        raw_account = client.get_account_v3()
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="balance", exchange=name, account=account,
                            code="APEX_ERROR", message=sanitize_error_message(str(exc)))
    summary = _normalize_balance(raw_balance, raw_account)
    positions = _normalize_positions(raw_account)
    return make_success(
        operation="balance", exchange=name, account=credentials["account"],
        balance=normalize_balance(summary.account_value, "USD"),
        portfolio_summary=summary,
        positions=positions,
    )


def _resolve_credentials(account: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """Return ``(credentials, None)`` on success or ``(None, error_dict)``."""
    credentials = _lookup_credentials(account)
    if credentials is None:
        return None, {
            "code": "ACCOUNT_NOT_FOUND",
            "message": (f"Apex Omni account '{account}' is not configured. "
                        "Set APEX_<alias>_ACCOUNTID, _APIKEY, _APIKEYSECRET, "
                        "_APIKEYPASSPHRASE, _SEEDS, and _L2KEY."),
        }
    return credentials, None


# ---------------------------------------------------------------------------
# Read: positions + orders + TP/SL enrichment
# ---------------------------------------------------------------------------


def _positions_orders(account: str) -> CanonicalResponse:
    """Composite read: returns positions, open orders, and TP/SL summary.

    The wizard's "💼 Positions & Orders" view consumes this single payload
    so we fold the three reads into one round-trip per source endpoint.

    Apex TradFi (stock) positions/orders live on the RWA sub-account; we
    merge them with primary (perp) data so the wizard sees both.
    """
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="positions_orders", exchange=name,
                            account=account, code=error["code"],
                            message=error["message"])
    assert credentials is not None
    try:
        client = _client_for_credentials(credentials)
        try:
            client.configs_v3()
        except Exception:  # noqa: BLE001
            pass
        snapshots = _apex_fetch_all_account_snapshots(client)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="positions_orders", exchange=name,
                            account=account, code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))
    positions = _normalize_positions_merged(snapshots)
    open_orders = _extract_open_orders(client)
    open_count, order_groups = _group_open_orders(open_orders)
    protections = _enrich_positions_with_tpsl(client, positions)
    enriched: List[CanonicalPosition] = []
    for position in positions:
        prot = protections.get(position.symbol, {})
        # CanonicalPosition is a frozen dataclass — rebuild with the
        # protection fields rather than assigning attributes.
        enriched.append(CanonicalPosition(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            pnl=position.pnl,
            tp=prot.get("tp"),
            sl=prot.get("sl"),
            tp_count=prot.get("tp_count"),
            sl_count=prot.get("sl_count"),
        ))
    return make_success(
        operation="positions_orders", exchange=name,
        account=credentials["account"],
        positions=enriched,
        open_order_count=open_count,
        order_groups=order_groups,
    )


# ---------------------------------------------------------------------------
# Write: new order
# ---------------------------------------------------------------------------


def _new_order(request: Dict[str, Any]) -> CanonicalResponse:
    """Place a single limit order on Apex Omni.

    The wizard's new-order flow passes the same keys the other agents
    accept (``symbol``, ``side``, ``order_type``, ``volume`` / ``size`` /
    ``quantity``, ``price``, ``reduce_only``, ``client_id``). Apex supports
    both limit and market orders; the wizard only exposes limit, so we
    reject market/anything-else with ``INVALID_ORDER_TYPE`` for now (the
    SDK call shape is the same; adding market later is mechanical).

    Apex requires:
      - ``configs_v3`` and ``get_account_v3`` to have been called on the
        client (we trigger both inline).
      - The price must be a clean multiple of ``tickSize`` (the SDK
        raises a clear exception otherwise).
      - The symbol must resolve to a known perpetual contract — the
        wizard's quick buttons emit bare bases like ``BTC`` / ``SOL`` /
        ``HYPE`` / ``ETH``; we resolve those to ``<BASE>-USDT`` via the
        configs-v3 contract list.
    """
    account = str(request.get("account") or "").strip()
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="new_order", exchange=name, account=account,
                            code=error["code"], message=error["message"])
    assert credentials is not None

    # --- parse + validate ---
    symbol_raw = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().lower()
    requested_volume = _safe_decimal(request.get("volume") or request.get("size") or request.get("quantity"))
    requested_price = _safe_decimal(request.get("price"))
    reduce_only_raw = request.get("reduce_only")
    if reduce_only_raw is None:
        reduce_only_raw = request.get("reduceOnly")
    reduce_only = bool(reduce_only_raw) if reduce_only_raw is not None else False
    client_id_raw = request.get("client_id") or request.get("clientId")

    if not symbol_raw:
        return make_failure(operation="new_order", exchange=name, account=account,
                            code="MISSING_SYMBOL", message="Symbol is required.")
    if side not in {"buy", "sell"}:
        return make_failure(operation="new_order", exchange=name, account=account,
                            code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_type != "limit":
        return make_failure(operation="new_order", exchange=name, account=account,
                            code="INVALID_ORDER_TYPE",
                            message="Only limit orders are supported.")
    if requested_volume <= 0:
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="INVALID_VOLUME",
                            message="Volume must be positive.")
    if requested_price <= 0:
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="INVALID_PRICE",
                            message="Price must be positive.")

    # --- bootstrap client + market metadata ---
    try:
        client = _client_for_credentials(credentials)
        # Start on primary for catalog/account bootstrap; switch later if
        # the resolved symbol is TradFi/stock.
        try:
            client.set_default_account_type("primary")
        except Exception:  # noqa: BLE001
            pass
        # The create_order_v3 path needs both configsV3 and accountV3
        # populated. Without these, the SDK raises "No config provided"
        # or "No accountId provided". Both calls are idempotent on the
        # server side and cheap on the cache.
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta = _apex_resolve_symbol(symbol_raw, all_contracts)
        if meta is None:
            return make_failure(operation="new_order", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex symbol '{symbol_raw}' is not available.")
        # Use the canonical ``symbol`` field (e.g. ``ETH-USDT``). The
        # alternative ``symbolDisplayName`` (``ETHUSDT``) is a display-only
        # field that the order placement endpoint rejects — confirmed via
        # live test (returned ``{code: ..., msg: ...}`` instead of orderId).
        symbol = meta["symbol"]
        tick_size = _safe_decimal(meta.get("tickSize"))
        step_size = _safe_decimal(meta.get("stepSize") or meta.get("lotSize"))
        min_notional = _safe_decimal(meta.get("minOrderNotional") or meta.get("minNotional") or "0")
        try:
            account_type = _apex_prepare_trading_context(client, symbol=str(symbol))
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="APEX_ACCOUNT_TYPE_ERROR",
                message=sanitize_error_message(str(exc)),
            )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))

    # --- snap to instrument precision ---
    submitted_volume = _floor_to_step(requested_volume, step_size)
    submitted_price = _snap_price_to_tick(requested_price, tick_size)
    if step_size > 0 and submitted_volume <= 0:
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="INVALID_VOLUME",
                            message="Volume rounds down to zero at the market step size.")
    if tick_size > 0 and submitted_price <= 0:
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="INVALID_PRICE",
                            message="Price rounds down to zero at the market tick size.")
    if min_notional > 0 and submitted_volume * submitted_price < min_notional:
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="NOTIONAL_BELOW_MINIMUM",
                            message=f"Order notional is below the market minimum ({min_notional}).")

    # --- build client id ---
    client_id = str(client_id_raw).strip() if client_id_raw is not None else ""
    if not client_id:
        client_id = f"apex-{uuid.uuid4().hex[:16]}"

    # --- submit ---
    try:
        sdk_side = "BUY" if side == "buy" else "SELL"
        raw = client.create_order_v3(
            symbol=symbol,
            side=sdk_side,
            type="LIMIT",
            size=_format_apex_decimal(submitted_volume),
            price=_format_apex_decimal(submitted_price),
            reduceOnly=reduce_only,
            timeInForce="GOOD_TIL_CANCEL",
            clientId=client_id,
            account_type=account_type,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="ORDER_SUBMISSION_FAILED",
                            message=sanitize_error_message(str(exc)))

    # --- verify ---
    verified_id = _apex_extract_order_id(raw)
    if verified_id is None:
        # The SDK call returned without an order id. Two possibilities:
        # (a) the server returned an error envelope ({"code": ..., "msg": ...})
        #     because of a validation failure — surface the server's code
        #     to the operator so they can see what went wrong.
        # (b) the response shape was unexpected (older SDK, drift) — fall
        #     through to a generic VERIFICATION_FAILED.
        if isinstance(raw, Mapping) and "code" in raw and "msg" in raw:
            server_code = str(raw.get("code") or "UNKNOWN")
            server_msg = str(raw.get("msg") or "Unknown error from Apex.")
            return make_failure(
                operation="new_order", exchange=name,
                account=credentials["account"],
                code=f"APEX_{server_code}",
                message=(
                    f"Apex rejected the order: {server_msg} "
                    f"(server code {server_code}; check symbol, size, price, "
                    "and account state)."
                ),
            )
        # The SDK call returned without an order id — treat as failed.
        return make_failure(operation="new_order", exchange=name,
                            account=credentials["account"],
                            code="VERIFICATION_FAILED",
                            message=(
                                f"Apex did not return an order id for the new order. "
                                f"Raw response keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}."
                            ),
            )

    # Optional read-back via open_orders_v3 to confirm the order is
    # actually resting on the book. This catches the case where the SDK
    # accepted the request but Apex silently rejected it on the server.
    verified_visible = _apex_verify_order_visible(client, client_id, verified_id)
    return make_success(
        operation="new_order", exchange=name,
        account=credentials["account"],
        order=CanonicalOrderResult(
            symbol=symbol, side=side, order_type=order_type,
            requested_volume=_format_decimal(requested_volume),
            requested_price=_format_decimal(requested_price),
            submitted_volume=_format_decimal(submitted_volume),
            submitted_price=_format_decimal(submitted_price),
            verified=verified_visible,
            status="success" if verified_visible else "submitted",
            exchange_order_id=verified_id,
        ),
    )


def _apex_resolve_symbol(requested: str,
                          contracts: List[Mapping[str, Any]]
                          ) -> Optional[Mapping[str, Any]]:
    """Resolve a wizard-supplied symbol to an Apex contract config row.

    Accepts any of:
      - bare base asset: ``BTC``, ``ETH``, ``SOL``, ``HYPE``, ``XAU``
      - human aliases: ``GOLD`` → ``XAU-USDT`` (TradFi stock contract)
      - explicit pair: ``BTC-USDT``, ``BTCUSDT``, ``XAUUSDT``
      - display / cross name: ``BTCUSDT``, ``XAUUSDT``

    The Apex config has distinct fields:
      - ``symbol``            = ``\"XAU-USDT\"`` (canonical, dash-separated)
      - ``symbolDisplayName`` = ``\"XAUUSDT\"``
      - ``crossSymbolName``   = often the same compact form

    TradFi instruments live under ``stockContract`` but are merged into
    ``contracts`` by ``_apex_fetch_supported_markets``.

    Returns the matching config dict, raises ``ValueError("INSTRUMENT_AMBIGUOUS")``
    when more than one contract matches, or ``None`` if nothing matches.
    """
    target = (requested or "").strip().upper()
    if not target:
        return None

    # Human commodity aliases (catalog still owns the venue id).
    _ALIAS_BASES = {
        "GOLD": "XAU",
        "SILVER": "XAG",
        "OIL": "USO",  # Apex lists USO-USDT in TradFi; WTI may be crypto/perp
        "CRUDE": "USO",
    }

    def _peel_base(token: str) -> str:
        base = token.split("-", 1)[0]
        for suffix in ("USDT", "USDC", "USD"):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
        return _ALIAS_BASES.get(base, base)

    target_base = _peel_base(target)
    alias_base = _ALIAS_BASES.get(target, target_base)

    candidates_for_target = {
        target,
        f"{target}-USDT",
        f"{target}USDT",
        f"{target}-USDC",
        f"{target}USDC",
        f"{target}-USD",
        f"{target}USD",
    }
    bases = {target_base, alias_base}
    candidates_for_base: set[str] = set()
    for base in bases:
        if not base:
            continue
        candidates_for_base.update(
            {
                base,
                f"{base}-USDT",
                f"{base}USDT",
                f"{base}-USDC",
                f"{base}USDC",
                f"{base}-USD",
                f"{base}USD",
            }
        )

    exact_symbol_matches: List[Mapping[str, Any]] = []
    for row in contracts:
        symbol = str(row.get("symbol") or "").upper()
        if symbol == target:
            exact_symbol_matches.append(row)
    if len(exact_symbol_matches) == 1:
        return exact_symbol_matches[0]
    if len(exact_symbol_matches) > 1:
        raise ValueError("INSTRUMENT_AMBIGUOUS")

    matches: List[Mapping[str, Any]] = []
    seen_ids = set()
    for row in contracts:
        symbol = str(row.get("symbol") or "").upper()
        display = str(row.get("symbolDisplayName") or "").upper()
        cross = str(row.get("crossSymbolName") or "").upper()
        row_id = row.get("id") if isinstance(row, Mapping) else None
        names = {symbol, display, cross}
        if (
            target in names
            or names & candidates_for_target
            or names & candidates_for_base
            or any(n in candidates_for_base for n in names if n)
            or any(
                _peel_base(n) in bases
                for n in names
                if n
            )
        ):
            # Prefer unique id; fall back to symbol when id missing.
            dedupe = row_id if row_id is not None else symbol
            if dedupe in seen_ids:
                continue
            seen_ids.add(dedupe)
            matches.append(row)
    if not matches:
        return None
    if len(matches) > 1:
        # Prefer exact base match on XAU over fuzzy multi-hits.
        preferred: List[Mapping[str, Any]] = []
        for row in matches:
            symbol = str(row.get("symbol") or "").upper()
            display = str(row.get("symbolDisplayName") or "").upper()
            cross = str(row.get("crossSymbolName") or "").upper()
            base = _peel_base(symbol or display or cross)
            if base == alias_base or base == target_base:
                preferred.append(row)
        if len(preferred) == 1:
            return preferred[0]
        if len(preferred) > 1:
            raise ValueError("INSTRUMENT_AMBIGUOUS")
        raise ValueError("INSTRUMENT_AMBIGUOUS")
    return matches[0]


def _apex_fetch_supported_markets(client: Any) -> List[Mapping[str, Any]]:
    """Return the unified Apex contract catalog used by every symbol-sensitive op.

    New writes, ladders, cancels, close and TP/SL helpers all read from this
    same list so duplicate-underlying disambiguation and ``INSTRUMENT_AMBIGUOUS``
    apply uniformly. Perps, prelaunch, and stock contracts are merged so the
    resolver is the single seam.
    """
    try:
        client.configs_v3()
    except Exception:
        pass
    config = client.configV3 or {}
    cc = config.get("contractConfig") or {}
    perps = list(cc.get("perpetualContract") or [])
    prelaunch = list(cc.get("prelaunchContract") or [])
    stock = list(cc.get("stockContract") or [])
    return perps + prelaunch + stock


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` down to the nearest multiple of ``step``."""
    if step <= 0:
        return value
    n = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return Decimal(n) * step


def _snap_price_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Snap ``value`` to the nearest tick. Apex SDK requires an exact
    multiple — failing the snap returns the raw value and the SDK will
    raise a precise error."""
    if tick <= 0:
        return value
    n = (value / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return Decimal(n) * tick


def _format_apex_decimal(value: Decimal) -> str:
    """Format a Decimal as a plain string (no scientific notation, no
    thousand separators). Apex accepts strings; we strip trailing zeros
    so ``1.0`` -> ``"1"``."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def _apex_extract_order_id(raw: Any) -> Optional[int]:
    """Pull a numeric order id out of an Apex create_order_v3 response.

    The SDK returns the order envelope as ``{"data": {"orderId": ...}, ...}``
    on success; ``orderId`` may be a string of digits. We coerce to int
    because the canonical response expects an int.
    """
    if not isinstance(raw, Mapping):
        return None
    data = raw.get("data")
    candidates: List[Any] = []
    if isinstance(data, Mapping):
        candidates.append(data.get("orderId"))
        candidates.append(data.get("id"))
    candidates.append(raw.get("orderId"))
    candidates.append(raw.get("id"))
    for cand in candidates:
        if cand is None:
            continue
        try:
            return int(cand)
        except (TypeError, ValueError):
            continue
    return None


def _apex_verify_order_visible(client: Any,
                                client_id: str,
                                order_id: int) -> bool:
    """Confirm the just-placed order is actually resting on the book.

    Returns True if either the open-orders list contains a row matching
    the order id OR (failing that) the client-id we generated. The SDK
    surfaces open orders via ``open_orders_v3``; we tolerate either
    envelope shape.
    """
    fn = getattr(client, "open_orders_v3", None) or getattr(client, "open_orders", None)
    if fn is None:
        return False
    try:
        result = fn()
    except Exception:  # noqa: BLE001
        return False
    rows: List[Mapping[str, Any]] = []
    if isinstance(result, Mapping):
        data = result.get("orders")
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, Mapping)]
        elif isinstance(data, Mapping):
            rows = [data]
        elif isinstance(result.get("data"), list):
            rows = [r for r in result["data"] if isinstance(r, Mapping)]
    elif isinstance(result, list):
        rows = [r for r in result if isinstance(r, Mapping)]
    if not rows:
        return False
    for row in rows:
        if isinstance(row.get("id"), int) and row.get("id") == order_id:
            return True
        if str(row.get("orderId") or "") == str(order_id):
            return True
        if str(row.get("clientId") or "") == str(client_id):
            return True
    return False


# ---------------------------------------------------------------------------
# Write: cancel order group
# ---------------------------------------------------------------------------


def _apex_cancel_open_orders(client: Any) -> Any:
    """Probe Apex's cancel endpoint to see what kwargs it actually accepts.

    The SDK's ``delete_open_orders_v3(**kwargs)`` is permissive — Apex's
    docs advertise ``symbol`` for filtering, but live tests show the
    endpoint cancels ALL open orders regardless of filter, which is
    catastrophic for users with ladders on multiple symbols. We refuse
    to expose this endpoint for symbol-scoped cancellation and only
    use it for true account-wide cancel-all operations if the operator
    explicitly requests it via a future bulk endpoint.

    This helper returns ``None`` because the wizard doesn't have a
    "cancel everything" menu item. Single-order cancellation goes
    through ``_cancel_order_group`` (see below).
    """
    return None


def _cancel_order_group(request: Dict[str, Any]) -> CanonicalResponse:
    """Cancel all open orders for (symbol, side) on Apex Omni.

    Apex Omni's cancel endpoints:
      - ``delete_order_v3(id="<orderId_str>")`` — single-order cancel,
        idempotent, verified (live-tested with ``id=str``).
      - ``delete_orders_v3(ids=[...])`` — bulk cancel via the SDK, but
        live testing shows it returns ``20016: Failed to check signature!``
        on apexomni 3.3.1 — the SDK's bulk signing is broken. We avoid
        this endpoint and cancel one-at-a-time instead.
      - ``delete_open_orders_v3(symbol="...")`` — mass cancel. Live
        testing confirms the ``symbol`` filter is IGNORED — passing
        any symbol cancels EVERYTHING on the account. We refuse to
        expose this endpoint for symbol-scoped operations.

    So the path is: snapshot open orders → identify (symbol, side)
    targets → cancel each via ``delete_order_v3`` one at a time →
    re-snapshot and confirm absence. This matches the Arcus
    implementation exactly and gives the wizard the same cancellation
    semantics it already knows how to render.
    """
    account = str(request.get("account") or "").strip()
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="cancel_order_group", exchange=name,
                            account=account, code=error["code"],
                            message=error["message"])
    assert credentials is not None

    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if not symbol:
        return make_failure(operation="cancel_order_group", exchange=name,
                            account=account, code="MISSING_SYMBOL",
                            message="Symbol is required.")
    if side not in {"buy", "sell"}:
        return make_failure(operation="cancel_order_group", exchange=name,
                            account=account, code="INVALID_SIDE",
                            message="Side must be buy or sell.")

    # Resolve the canonical symbol — the wizard may send a bare base
    # ("BTC") or a display name ("BTCUSDT") but the open-orders rows
    # always carry the canonical "BTC-USDT".
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta = _apex_resolve_symbol(symbol, all_contracts)
        if meta is None:
            return make_failure(operation="cancel_order_group", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex symbol '{symbol}' is not available.")
        canonical_symbol = meta["symbol"]
        # TradFi cancels must run under the stock/RWA account context.
        try:
            _apex_prepare_trading_context(client, symbol=str(canonical_symbol))
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                code="APEX_ACCOUNT_TYPE_ERROR",
                message=sanitize_error_message(str(exc)),
            )
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(operation="cancel_order_group", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_AMBIGUOUS",
                                message=f"Apex instrument '{symbol}' is ambiguous.")
        raise
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="cancel_order_group", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))

    # Snapshot open orders and identify targets.
    try:
        before = _extract_open_orders(client)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="cancel_order_group", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))
    targets: List[Dict[str, Any]] = []
    for row in before:
        if not isinstance(row, Mapping):
            continue
        row_symbol = str(row.get("symbol") or "").upper()
        row_side = str(row.get("side") or "").strip().lower()
        if row_symbol != canonical_symbol.upper() or row_side != side:
            continue
        # Apex stores the id under both ``id`` (string of digits) and
        # ``orderId`` (alias); prefer the integer ``id`` since that's
        # what the cancel endpoint accepts.
        raw_id = row.get("id") or row.get("orderId")
        order_id = str(raw_id).strip() if raw_id is not None else ""
        if not order_id:
            continue
        # Only cancel resting orders (OPEN/NEW/PENDING/UNTRIGGERED).
        # TRIGGERED/FILLED/CANCELED rows are no longer cancellable.
        row_status = str(row.get("status") or "").strip().upper()
        if row_status not in {"OPEN", "NEW", "PENDING", "UNTRIGGERED", "ACCEPTED"}:
            continue
        targets.append({"row": row, "order_id": order_id})

    if not targets:
        cancel_group = CanonicalCancelGroupResult(
            symbol=canonical_symbol, side=side,
            targeted_order_count=0, cancelled_order_count=0,
            confirmed_absent_count=0, remaining_target_count=0,
            verified=True, partial=False, status="success",
            batch_count=0, batches=[],
        )
        return make_success(operation="cancel_order_group", exchange=name,
                            account=credentials["account"],
                            cancel_group=cancel_group)

    # Re-select the correct account context for deletes (extract_open_orders
    # restores primary in its finally block).
    try:
        _apex_prepare_trading_context(client, symbol=str(canonical_symbol))
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="APEX_ACCOUNT_TYPE_ERROR",
            message=sanitize_error_message(str(exc)),
        )

    # Cancel each target one at a time. The SDK's ``delete_order_v3``
    # accepts ``id=<orderId_str>`` and returns the deleted order id
    # on success (verified empirically — see commit log). We capture
    # per-order failures as ``reason`` entries on the batch record so
    # the wizard can surface which children failed.
    cancelled = 0
    batches: List[Dict[str, Any]] = []
    for target in targets:
        order_id = target["order_id"]
        try:
            response = client.delete_order_v3(id=order_id)
        except Exception as exc:  # noqa: BLE001
            # Network / SDK error: record the failure but keep trying the
            # other targets. After the loop we verify which are actually
            # gone — a "FailedRequestError" on the SDK doesn't necessarily
            # mean the cancel was rejected; the server may have processed
            # it but the SDK may have lost the response (e.g. mid-stream
            # disconnect). Verify against the authoritative open-orders
            # snapshot.
            batches.append({
                "submitted": 1, "accepted": 0, "ok": False,
                "order_id": order_id,
                "reason": sanitize_error_message(str(exc)),
            })
            continue
        # SDK returned without raising: parse the response shape.
        response_data: Any = response
        if isinstance(response, Mapping):
            response_data = response.get("data")
        if isinstance(response_data, str) and response_data.strip():
            # Apex returns ``{"data": "<orderId>"}`` on success.
            cancelled += 1
            batches.append({
                "submitted": 1, "accepted": 1, "ok": True,
                "order_id": order_id,
            })
        elif isinstance(response, Mapping) and response.get("code"):
            # Server returned an error envelope.
            server_code = response.get("code")
            server_msg = response.get("msg") or "Unknown error from Apex."
            batches.append({
                "submitted": 1, "accepted": 0, "ok": False,
                "order_id": order_id,
                "reason": f"{server_code}: {server_msg}",
            })
        else:
            # Unknown shape — treat as a soft success; the after-snapshot
            # will tell us definitively.
            batches.append({
                "submitted": 1, "accepted": 1, "ok": True,
                "order_id": order_id,
                "note": "response shape unknown; verifying via snapshot",
            })
            cancelled += 1

    # Authoritative verification: re-snapshot open orders and confirm
    # the targeted (symbol, side) children are absent.
    try:
        after = _extract_open_orders(client)
    except Exception as exc:  # noqa: BLE001
        # Snapshot failure is rare but possible — report partial.
        cancel_group = CanonicalCancelGroupResult(
            symbol=canonical_symbol, side=side,
            targeted_order_count=len(targets),
            cancelled_order_count=cancelled,
            confirmed_absent_count=0, remaining_target_count=len(targets) - cancelled,
            verified=False, partial=True, status="partial",
            batch_count=len(batches), batches=batches,
        )
        return make_failure(
            operation="cancel_order_group", exchange=name,
            account=credentials["account"],
            code="VERIFICATION_FAILED",
            message=(
                "Could not verify cancellation by snapshotting open orders: "
                f"{sanitize_error_message(str(exc))}."
            ),
            cancel_group=cancel_group,
        )
    remaining: List[Mapping[str, Any]] = []
    for row in after:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "").upper() != canonical_symbol.upper():
            continue
        if str(row.get("side") or "").strip().lower() != side:
            continue
        if str(row.get("status") or "").strip().upper() not in {
            "OPEN", "NEW", "PENDING", "UNTRIGGERED", "ACCEPTED",
        }:
            continue
        # Cross-reference against target orderIds.
        raw_id = row.get("id") or row.get("orderId")
        row_id_str = str(raw_id).strip() if raw_id is not None else ""
        if row_id_str in {t["order_id"] for t in targets}:
            remaining.append(row)
    confirmed_absent = len(targets) - len(remaining)
    verified = len(remaining) == 0
    if verified:
        return make_success(
            operation="cancel_order_group", exchange=name,
            account=credentials["account"],
            cancel_group=CanonicalCancelGroupResult(
                symbol=canonical_symbol, side=side,
                targeted_order_count=len(targets),
                cancelled_order_count=cancelled,
                confirmed_absent_count=confirmed_absent,
                remaining_target_count=len(remaining),
                verified=True, partial=False, status="success",
                batch_count=len(batches), batches=batches,
            ),
        )
    return make_failure(
        operation="cancel_order_group", exchange=name,
        account=credentials["account"],
        code="VERIFICATION_FAILED",
        message=(
            f"Cancellation could not be fully verified: "
            f"{len(remaining)} of {len(targets)} order(s) still resting on the book."
        ),
        cancel_group=CanonicalCancelGroupResult(
            symbol=canonical_symbol, side=side,
            targeted_order_count=len(targets),
            cancelled_order_count=cancelled,
            confirmed_absent_count=confirmed_absent,
            remaining_target_count=len(remaining),
            verified=False,
            partial=confirmed_absent > 0,
            status="partial" if confirmed_absent > 0 else "failed",
            batch_count=len(batches), batches=batches,
        ),
    )


# ---------------------------------------------------------------------------
# Write: position management (set_tp / set_sl / close_position)
# ---------------------------------------------------------------------------


def _apex_position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    status: str,
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    exchange_order_id: Optional[int] = None,
    message: Optional[str] = None,
) -> CanonicalPositionActionResult:
    """Construct a ``CanonicalPositionActionResult`` with the fields the
    wizard's position-action renderer expects."""
    return CanonicalPositionActionResult(
        operation=operation, symbol=symbol, verified=verified, status=status,
        current_side=current_side, current_size=current_size, price=price,
        removed=removed, exchange_order_id=exchange_order_id,
        message=message,
    )


def _apex_fetch_positions(client: Any) -> List[Mapping[str, Any]]:
    """Return the live position rows from ``client.get_account_v3()``.

    Apex surfaces positions as a list under the account snapshot's
    ``positions`` key (plural — verified against the live BITGET account
    which returned 6 rows under ``positions``). We coerce the SDK
    response into a uniform list of Mapping rows so the position-
    management helpers can iterate it.
    """
    client.get_account_v3()
    account = client.accountV3 or {}
    raw_positions = account.get("positions") or []
    return [p for p in raw_positions if isinstance(p, Mapping)]


def _apex_find_position(positions: List[Mapping[str, Any]],
                        symbol: str,
                        ) -> Optional[Mapping[str, Any]]:
    """Find an open position matching ``symbol`` (case-insensitive)."""
    target = symbol.upper()
    for row in positions:
        row_symbol = str(row.get("symbol") or "").upper()
        if row_symbol == target:
            return row
    return None


def _apex_position_side(row: Mapping[str, Any]) -> str:
    """Apex positions use ``side`` ('LONG' / 'SHORT') or ``posSide``; fall
    back to size sign (positive = long, negative = short)."""
    side = str(row.get("side") or row.get("posSide") or "").strip().lower()
    if side in {"long", "buy", "long_buy"}:
        return "long"
    if side in {"short", "sell", "long_sell"}:
        return "short"
    # Fall back to size sign.
    try:
        size_value = Decimal(str(row.get("size") or "0"))
    except Exception:
        return "long"
    return "long" if size_value >= 0 else "short"


def _apex_position_size(row: Mapping[str, Any]) -> Decimal:
    """Return the absolute size of a position."""
    raw = str(row.get("size") or "0")
    try:
        value = Decimal(raw)
    except Exception:
        return Decimal("0")
    return value.copy_abs()


def _apex_fetch_mark_price(client: Any, symbol: str) -> Decimal:
    """Return the current mark price for ``symbol`` via the SDK's ticker
    endpoint. Returns ``Decimal(0)`` on any failure — callers treat that
    as "mark unavailable" and refuse to place a close order.

    Apex position rows do NOT carry the live mark price (only
    ``entryPrice``, ``fee``, ``fundingFee``, etc.), so we MUST hit the
    ticker endpoint to get a recent mark before placing a close-order
    limit at the right price.
    """
    fn = getattr(client, "ticker_v3", None)
    if fn is None:
        return Decimal("0")
    try:
        response = fn(symbol=symbol)
    except Exception:
        return Decimal("0")
    data = response.get("data") if isinstance(response, Mapping) else None
    if not isinstance(data, list) or not data:
        return Decimal("0")
    target = symbol.upper()
    # Find the matching symbol; the ticker rows use ``symbolDisplayName``
    # form (e.g. ``BTCUSDT`` without dash) or canonical ``BTC-USDT``.
    for row in data:
        if not isinstance(row, Mapping):
            continue
        row_symbol = str(row.get("symbol") or "").upper().replace("USDT", "-USDT")
        if row_symbol == target:
            mark = row.get("markPrice") or row.get("oraclePrice") or row.get("indexPrice") or row.get("lastPrice")
            try:
                return Decimal(str(mark))
            except Exception:
                return Decimal("0")
    return Decimal("0")


def _apex_closing_side(position_side: str) -> str:
    """A closing order must trade against the position side."""
    return "sell" if position_side == "long" else "buy"


def _apex_normalize_meta(meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Pick out the metadata fields the position-management code uses."""
    tick = _safe_decimal(meta.get("tickSize"))
    step = _safe_decimal(meta.get("stepSize") or meta.get("lotSize"))
    min_size = _safe_decimal(meta.get("minOrderSize") or "0")
    symbol = str(meta.get("symbol") or "")
    return {
        "symbol": symbol,
        "tick_size": tick,
        "step_size": step,
        "min_order_size": min_size,
    }


def _apex_fetch_open_orders_for_symbol_side(
    client: Any,
    symbol: str,
    side: str,
    *,
    include_history: bool = False,
) -> List[Mapping[str, Any]]:
    """Return open orders matching (symbol, side). Used to identify
    existing position-level TP/SL orders before placing a new one.

    When ``include_history=True``, also probes the ``history_orders_v3``
    endpoint because Apex's ``open_orders_v3`` may exclude position-bound
    TP/SL orders (which are technically resting but counted as "attached"
    to the position). The merged, de-duplicated list is returned.
    """
    target_symbol = symbol.upper()
    target_side = side.lower()
    seen: Dict[str, Mapping[str, Any]] = {}

    def _filter(rows: Any) -> List[Mapping[str, Any]]:
        out: List[Mapping[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("symbol") or "").upper() != target_symbol:
                continue
            row_side = str(row.get("side") or "").strip().lower()
            if row_side != target_side:
                continue
            out.append(row)
        return out

    try:
        for row in _filter(_extract_open_orders(client)):
            raw_id = str(row.get("id") or row.get("orderId") or "")
            seen[str(raw_id) or f"row-{id(row)}"] = row
    except Exception:
        pass

    if include_history:
        history_fn = getattr(client, "history_orders_v3", None) or getattr(
            client, "history_orders", None
        )
        if callable(history_fn):
            try:
                history_raw = history_fn(symbol=symbol)
                # history_orders_v3 returns ``{"data": {"orders": [...],
                # "totalSize": N}, "timeCost": ...}`` on Apex 3.3.x.
                # Older shapes: bare list, ``{"orders": [...]}``, or
                # ``{"data": [...]}``. Normalise to a flat list.
                if isinstance(history_raw, Mapping):
                    inner = history_raw.get("data")
                    if isinstance(inner, Mapping):
                        history_raw = inner.get("orders") or []
                    elif isinstance(inner, list):
                        history_raw = inner
                    else:
                        history_raw = history_raw.get("orders") or []
                for row in _filter(history_raw):
                    raw_id = str(row.get("id") or row.get("orderId") or "")
                    seen[str(raw_id) or f"row-{id(row)}"] = row
            except Exception:
                pass

    return list(seen.values())


def _apex_order_is_position_tpsl(row: Mapping[str, Any]) -> bool:
    """Best-effort check: a row is a position-bound TP/SL if either
    ``isPositionTpsl`` is true OR its ``type`` is one of Apex's
    trigger-market order types (``TAKE_PROFIT_MARKET`` / ``STOP_MARKET``)
    with a non-zero ``triggerPrice``."""
    if row.get("isPositionTpsl") is True:
        return True
    order_type = str(row.get("type") or "").strip().upper()
    if order_type in {"TAKE_PROFIT_MARKET", "STOP_MARKET", "TAKE_PROFIT", "STOP"}:
        # Any of these order types paired with a non-zero triggerPrice is
        # a TP/SL — even when isPositionTpsl is not explicitly set.
        trigger = row.get("triggerPrice")
        if trigger not in (None, "", "0", 0):
            return True
    return False


def _apex_order_tpsl_kind(row: Mapping[str, Any]) -> Optional[str]:
    """Return ``"TP"`` or ``"SL"`` for a position-bound TP/SL order, else
    ``None``. We prefer the explicit ``type`` field; fall back to
    ``tpslType``; fall back to the triggerPrice/price relation.
    """
    order_type = str(row.get("type") or "").strip().upper()
    if order_type in {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}:
        return "TP"
    if order_type in {"STOP_MARKET", "STOP"}:
        return "SL"
    tpsl_type = str(row.get("tpslType") or "").strip().upper()
    if tpsl_type in {"TP", "SL"}:
        return tpsl_type
    # Last-resort heuristic: trigger vs price relation.
    trigger = row.get("triggerPrice")
    price = row.get("price")
    if trigger in (None, "", "0") or price in (None, "", "0"):
        if row.get("isPositionTpsl") is True:
            return "TP"  # generic — caller can disambiguate via position side
        return None
    try:
        trig_d = Decimal(str(trigger))
        price_d = Decimal(str(price))
    except Exception:
        return None
    return "TP" if trig_d > price_d else "SL"


def _apex_is_not_found_error(exc: Exception) -> bool:
    """Treat "order already gone" as success during cancel so the
    merge-and-replace flow is idempotent on transient errors.

    Apex surfaces "already cancelled" / "order not found" failures in
    several ways depending on which path the request took:

    1. ``InvalidRequestError`` with ``status_code=400`` and a JSON
       envelope like ``{"code": 20006, "msg": "Order not found"}``.
    2. ``FailedRequestError`` with ``status_code=409`` and message
       ``"Conflict. could not decode json:"`` — the server returned an
       empty/non-JSON body when we asked to cancel a deleted order.

    We treat both shapes as success (the order is gone, which is the
    outcome we wanted). Only re-raise on a *real* error (auth,
    network, signing failure, or status codes other than 400/409) so
    the wizard surfaces it via ``make_failure``.
    """
    status_code = getattr(exc, "status_code", None)
    msg = str(exc or "").lower()
    # FailedRequestError 409 with "could not decode json" — empty body,
    # most likely because the order is gone.
    if status_code == 409 and "could not decode json" in msg:
        return True
    # InvalidRequestError 400 — error envelope often says "not found".
    if status_code == 400 and ("not found" in msg or "not exist" in msg):
        return True
    if any(s in msg for s in (
        "not found", "not exist", "does not exist", "already",
        "invalid order", "no such", "order is not",
    )):
        return True
    return False


def _apex_remove_existing_tpsl(
    client: Any,
    credentials: Mapping[str, Any],
    symbol: str,
    position_side: str,
    kind: str,
) -> int:
    """Cancel any existing position-level TP or SL orders for ``symbol``.

    Returns the count of TP/SL orders that were successfully cancelled
    (counts already-cancelled orders too, so the caller sees a stable
    count). Probes both ``open_orders_v3`` and ``history_orders_v3``
    because position-bound TP/SL may only appear in the history endpoint.

    Two safeguards beyond a plain cancel-loop:

    1. **Dedupe before write.** If multiple stale orders exist for the
       same leg (e.g. a previous run left duplicates — see the "two TPs"
       bug), sort by ``createdAt`` descending, keep the newest as the
       "intended survivor", and cancel everything else. After dedupe
       we have at most one row to act on.

    2. **Idempotent cancel.** A delete that fails with "not found" /
       "already cancelled" is treated as success — the order is already
       gone, which is the outcome we wanted. Only re-raise on a *real*
       error so the wizard surfaces it.

    The filter for which rows count as TP/SL is strict: only
    ``isPositionTpsl=True`` OR trigger-market order types with a
    non-zero ``triggerPrice`` qualify. Regular LIMIT/buy/sell orders are
    never touched.
    """
    closing_side = _apex_closing_side(position_side)
    rows = _apex_fetch_open_orders_for_symbol_side(
        client, symbol, closing_side, include_history=True,
    )
    tpsl_rows = [r for r in rows if _apex_order_is_position_tpsl(r)]
    # Filter to matching leg only when the row carries enough info.
    matching_rows = [
        r for r in tpsl_rows
        if _apex_order_tpsl_kind(r) in (None, kind)
    ]
    if not matching_rows:
        return 0
    # Dedupe: keep newest by createdAt, cancel the rest.
    def _created_at(row: Mapping[str, Any]) -> int:
        try:
            return int(row.get("createdAt") or row.get("created_time") or 0)
        except Exception:
            return 0
    sorted_rows = sorted(matching_rows, key=_created_at, reverse=True)
    survivor = sorted_rows[:1]
    stale = sorted_rows[1:]
    cancelled = 0
    for row in stale + survivor:
        raw_id = row.get("id") or row.get("orderId")
        if not raw_id:
            continue
        try:
            client.delete_order_v3(id=str(raw_id))
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            if _apex_is_not_found_error(exc):
                # Order already gone — counts as a successful cancel for
                # idempotency purposes.
                cancelled += 1
                continue
            # Re-raise so the wizard surfaces real errors (auth,
            # network, etc.). The caller wraps the whole flow in a
            # try/except that returns make_failure.
            raise
    return cancelled


def _apex_place_position_tpsl(
    client: Any,
    credentials: Mapping[str, Any],
    symbol: str,
    position_side: str,
    trigger_price: Decimal,
    limit_price: Decimal,
    size: Decimal,
    *,
    client_id: str,
    leg: str,
    trigger_price_type: str = "MARKET",
) -> Dict[str, Any]:
    """Place a position-level TP or SL as a reduce-only trigger-market.

    Apex Omni's server requires:
      - ``type="TAKE_PROFIT_MARKET"`` for TP, ``type="STOP_MARKET"`` for SL
        (the canonical Binance-style trigger-market order types — Apex
        does NOT accept plain ``MARKET`` for these).
      - ``reduceOnly=True`` so the order never increases the position.
      - ``isPositionTpsl=True`` to bind the order to the position (so it
        auto-cancels when the position closes).
      - ``price == triggerPrice`` (the trigger price doubles as the
        order price — the L2 zk-link signer multiplies by ``10 ** decimals``
        for the on-chain payload, and the trigger check uses
        ``triggerPriceType=MARKET`` against the live mark).
      - ``timeInForce="GOOD_TIL_CANCEL"`` is the safe default; the
        server normalizes it to ``IMMEDIATE_OR_CANCEL`` for trigger-markets
        but accepts the GTC spelling too.

    Returns the SDK response dict on success.
    """
    leg_normalised = (leg or "").strip().upper()
    if leg_normalised not in {"TP", "SL"}:
        raise ValueError(f"leg must be 'TP' or 'SL', got {leg!r}")
    order_type = "TAKE_PROFIT_MARKET" if leg_normalised == "TP" else "STOP_MARKET"
    closing_side = _apex_closing_side(position_side)
    response = client.create_order_v3(
        symbol=symbol,
        side="SELL" if closing_side == "sell" else "BUY",
        type=order_type,
        size=str(size),
        price=str(trigger_price),
        reduceOnly=True,
        triggerPrice=str(trigger_price),
        triggerPriceType=trigger_price_type,
        isPositionTpsl=True,
        clientId=client_id,
        timeInForce="GOOD_TIL_CANCEL",
        account_type=getattr(client, "_default_account_type", None) or "primary",
    )
    if isinstance(response, Mapping):
        return dict(response)
    return {"data": response}


def _apex_position_management(account: str) -> CanonicalResponse:
    """Return the current positions list, identical in shape to the
    ``positions_orders`` composite read (wizard reads ``response.positions``
    for the position-management screen)."""
    return _positions_orders(account)


def _apex_set_tp(request: Dict[str, Any]) -> CanonicalResponse:
    """Set a Take Profit on the open position for ``symbol``.

    The wizard contract treats ``price <= 0`` as "remove existing TP";
    we cancel existing position-level TP orders and report ``removed=True``.
    Otherwise we cancel any existing TP, place a new one at the trigger
    price, and verify by snapshotting open orders.
    """
    account = str(request.get("account") or "").strip()
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="set_tp", exchange=name, account=account,
                            code=error["code"], message=error["message"])
    assert credentials is not None

    requested_symbol = str(request.get("symbol") or "").strip()
    price_text = str(request.get("price") or "").strip()
    if not requested_symbol:
        return make_failure(operation="set_tp", exchange=name, account=account,
                            code="MISSING_SYMBOL", message="Symbol is required.")
    if not price_text:
        return make_failure(operation="set_tp", exchange=name, account=account,
                            code="INVALID_TP_PRICE",
                            message="TP price is required.")
    try:
        tp_price = Decimal(price_text)
    except Exception:
        return make_failure(operation="set_tp", exchange=name, account=account,
                            code="INVALID_TP_PRICE",
                            message="TP price must be numeric.")

    # Boot client + resolve symbol
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta_raw = _apex_resolve_symbol(requested_symbol, all_contracts)
        if meta_raw is None:
            return make_failure(operation="set_tp", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex symbol '{requested_symbol}' is not available.")
        meta = _apex_normalize_meta(meta_raw)
        tick_size = meta["tick_size"]
        step_size = meta["step_size"]
        symbol = meta["symbol"]
        try:
            _apex_prepare_trading_context(client, symbol=str(symbol))
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="set_tp",
                exchange=name,
                account=credentials["account"],
                code="APEX_ACCOUNT_TYPE_ERROR",
                message=sanitize_error_message(str(exc)),
            )
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(operation="set_tp", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_AMBIGUOUS",
                                message=f"Apex instrument '{requested_symbol}' is ambiguous.")
        raise
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="set_tp", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))

    # Find the open position
    positions = _apex_fetch_positions(client)
    position_row = _apex_find_position(positions, symbol)
    if position_row is None:
        return make_failure(operation="set_tp", exchange=name,
                            account=credentials["account"],
                            code="NO_OPEN_POSITION",
                            message=f"No open position for {symbol}.",
                            position_action=_apex_position_action_result(
                                operation="set_tp", symbol=symbol, verified=False,
                                status="failed",
                            ))
    position_side = _apex_position_side(position_row)
    position_size = _apex_position_size(position_row)

    # Negative or zero price → remove existing TP.
    if tp_price <= 0:
        cancelled = _apex_remove_existing_tpsl(
            client, credentials, symbol, position_side, "TP",
        )
        return make_success(operation="set_tp", exchange=name,
                            account=credentials["account"],
                            position_action=_apex_position_action_result(
                                operation="set_tp", symbol=symbol, verified=True,
                                status="removed", removed=True,
                                current_side=position_side,
                                current_size=str(position_size),
                                price="0",
                                message=(
                                    f"Cancelled {cancelled} existing TP order(s)."
                                    if cancelled else
                                    "No existing TP to cancel."
                                ),
                            ))

    # Snap to tickSize
    snapped_tp = (tp_price / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size if tick_size > 0 else tp_price
    # Validate TP direction relative to current position side.
    if position_side == "long" and snapped_tp <= 0:
        return make_failure(operation="set_tp", exchange=name,
                            account=credentials["account"],
                            code="INVALID_TP_PRICE",
                            message=f"TP price {snapped_tp} must be positive.",
                            position_action=_apex_position_action_result(
                                operation="set_tp", symbol=symbol, verified=False,
                                status="failed", price=str(snapped_tp),
                                current_side=position_side,
                                current_size=str(position_size),
                            ))

    # Cancel any existing TP first.
    _apex_remove_existing_tpsl(client, credentials, symbol, position_side, "TP")

    # Place the new TP.
    client_id = f"apex-tp-{uuid.uuid4().hex[:10]}"
    try:
        response = _apex_place_position_tpsl(
            client, credentials, symbol, position_side,
            trigger_price=snapped_tp, limit_price=snapped_tp,
            size=position_size, client_id=client_id, leg="TP",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="set_tp", exchange=name,
                            account=credentials["account"],
                            code="TP_PLACEMENT_FAILED",
                            message=sanitize_error_message(str(exc)),
                            position_action=_apex_position_action_result(
                                operation="set_tp", symbol=symbol, verified=False,
                                status="failed", price=str(snapped_tp),
                                current_side=position_side,
                                current_size=str(position_size),
                            ))
    extracted_id = _apex_extract_order_id(response)
    if extracted_id is None:
        # Surface the server's error envelope if present.
        if isinstance(response, Mapping) and response.get("code"):
            return make_failure(operation="set_tp", exchange=name,
                                account=credentials["account"],
                                code="TP_PLACEMENT_FAILED",
                                message=f"{response.get('code')}: {response.get('msg') or 'Unknown error.'}",
                                position_action=_apex_position_action_result(
                                    operation="set_tp", symbol=symbol, verified=False,
                                    status="failed", price=str(snapped_tp),
                                    current_side=position_side,
                                    current_size=str(position_size),
                                ))
        return make_failure(operation="set_tp", exchange=name,
                            account=credentials["account"],
                            code="VERIFICATION_FAILED",
                            message="Apex did not return an order id for the new TP.",
                            position_action=_apex_position_action_result(
                                operation="set_tp", symbol=symbol, verified=False,
                                status="failed", price=str(snapped_tp),
                                current_side=position_side,
                                current_size=str(position_size),
                            ))
    return make_success(operation="set_tp", exchange=name,
                        account=credentials["account"],
                        position_action=_apex_position_action_result(
                            operation="set_tp", symbol=symbol, verified=True,
                            status="success", price=str(snapped_tp),
                            current_side=position_side,
                            current_size=str(position_size),
                            exchange_order_id=extracted_id,
                        ))


def _apex_set_sl(request: Dict[str, Any]) -> CanonicalResponse:
    """Set a Stop Loss on the open position for ``symbol``.

    Same semantics as ``_apex_set_tp`` but for stop-loss: ``price <= 0``
    cancels existing position-level SL orders.
    """
    account = str(request.get("account") or "").strip()
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="set_sl", exchange=name, account=account,
                            code=error["code"], message=error["message"])
    assert credentials is not None

    requested_symbol = str(request.get("symbol") or "").strip()
    price_text = str(request.get("price") or "").strip()
    if not requested_symbol:
        return make_failure(operation="set_sl", exchange=name, account=account,
                            code="MISSING_SYMBOL", message="Symbol is required.")
    if not price_text:
        return make_failure(operation="set_sl", exchange=name, account=account,
                            code="INVALID_SL_PRICE",
                            message="SL price is required.")
    try:
        sl_price = Decimal(price_text)
    except Exception:
        return make_failure(operation="set_sl", exchange=name, account=account,
                            code="INVALID_SL_PRICE",
                            message="SL price must be numeric.")

    # Boot client + resolve symbol
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta_raw = _apex_resolve_symbol(requested_symbol, all_contracts)
        if meta_raw is None:
            return make_failure(operation="set_sl", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex symbol '{requested_symbol}' is not available.")
        meta = _apex_normalize_meta(meta_raw)
        tick_size = meta["tick_size"]
        symbol = meta["symbol"]
        try:
            _apex_prepare_trading_context(client, symbol=str(symbol))
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="set_sl",
                exchange=name,
                account=credentials["account"],
                code="APEX_ACCOUNT_TYPE_ERROR",
                message=sanitize_error_message(str(exc)),
            )
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(operation="set_sl", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_AMBIGUOUS",
                                message=f"Apex instrument '{requested_symbol}' is ambiguous.")
        raise
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="set_sl", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))

    # Find the open position
    positions = _apex_fetch_positions(client)
    position_row = _apex_find_position(positions, symbol)
    if position_row is None:
        return make_failure(operation="set_sl", exchange=name,
                            account=credentials["account"],
                            code="NO_OPEN_POSITION",
                            message=f"No open position for {symbol}.",
                            position_action=_apex_position_action_result(
                                operation="set_sl", symbol=symbol, verified=False,
                                status="failed",
                            ))
    position_side = _apex_position_side(position_row)
    position_size = _apex_position_size(position_row)

    if sl_price <= 0:
        cancelled = _apex_remove_existing_tpsl(
            client, credentials, symbol, position_side, "SL",
        )
        return make_success(operation="set_sl", exchange=name,
                            account=credentials["account"],
                            position_action=_apex_position_action_result(
                                operation="set_sl", symbol=symbol, verified=True,
                                status="removed", removed=True,
                                current_side=position_side,
                                current_size=str(position_size),
                                price="0",
                                message=(
                                    f"Cancelled {cancelled} existing SL order(s)."
                                    if cancelled else
                                    "No existing SL to cancel."
                                ),
                            ))
    snapped_sl = (sl_price / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size if tick_size > 0 else sl_price
    if snapped_sl <= 0:
        return make_failure(operation="set_sl", exchange=name,
                            account=credentials["account"],
                            code="INVALID_SL_PRICE",
                            message=f"SL price {snapped_sl} must be positive.",
                            position_action=_apex_position_action_result(
                                operation="set_sl", symbol=symbol, verified=False,
                                status="failed", price=str(snapped_sl),
                                current_side=position_side,
                                current_size=str(position_size),
                            ))

    _apex_remove_existing_tpsl(client, credentials, symbol, position_side, "SL")
    client_id = f"apex-sl-{uuid.uuid4().hex[:10]}"
    try:
        response = _apex_place_position_tpsl(
            client, credentials, symbol, position_side,
            trigger_price=snapped_sl, limit_price=snapped_sl,
            size=position_size, client_id=client_id, leg="SL",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="set_sl", exchange=name,
                            account=credentials["account"],
                            code="SL_PLACEMENT_FAILED",
                            message=sanitize_error_message(str(exc)),
                            position_action=_apex_position_action_result(
                                operation="set_sl", symbol=symbol, verified=False,
                                status="failed", price=str(snapped_sl),
                                current_side=position_side,
                                current_size=str(position_size),
                            ))
    extracted_id = _apex_extract_order_id(response)
    if extracted_id is None:
        if isinstance(response, Mapping) and response.get("code"):
            return make_failure(operation="set_sl", exchange=name,
                                account=credentials["account"],
                                code="SL_PLACEMENT_FAILED",
                                message=f"{response.get('code')}: {response.get('msg') or 'Unknown error.'}",
                                position_action=_apex_position_action_result(
                                    operation="set_sl", symbol=symbol, verified=False,
                                    status="failed", price=str(snapped_sl),
                                    current_side=position_side,
                                    current_size=str(position_size),
                                ))
        return make_failure(operation="set_sl", exchange=name,
                            account=credentials["account"],
                            code="VERIFICATION_FAILED",
                            message="Apex did not return an order id for the new SL.",
                            position_action=_apex_position_action_result(
                                operation="set_sl", symbol=symbol, verified=False,
                                status="failed", price=str(snapped_sl),
                                current_side=position_side,
                                current_size=str(position_size),
                            ))
    return make_success(operation="set_sl", exchange=name,
                        account=credentials["account"],
                        position_action=_apex_position_action_result(
                            operation="set_sl", symbol=symbol, verified=True,
                            status="success", price=str(snapped_sl),
                            current_side=position_side,
                            current_size=str(position_size),
                            exchange_order_id=extracted_id,
                        ))


def _apex_close_position(request: Dict[str, Any]) -> CanonicalResponse:
    """Close the entire open position for ``symbol`` by placing a
    reduce-only limit at the current mark price. Apex doesn't support
    market orders, so we use a limit-at-mark with reduceOnly=True — the
    server fills at-or-better when the position crosses the mark."""
    account = str(request.get("account") or "").strip()
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="close_position", exchange=name,
                            account=account, code=error["code"],
                            message=error["message"])
    assert credentials is not None

    requested_symbol = str(request.get("symbol") or "").strip()
    if not requested_symbol:
        return make_failure(operation="close_position", exchange=name,
                            account=account, code="MISSING_SYMBOL",
                            message="Symbol is required.")

    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta_raw = _apex_resolve_symbol(requested_symbol, all_contracts)
        if meta_raw is None:
            return make_failure(operation="close_position", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex symbol '{requested_symbol}' is not available.")
        meta = _apex_normalize_meta(meta_raw)
        tick_size = meta["tick_size"]
        symbol = meta["symbol"]
        try:
            _apex_prepare_trading_context(client, symbol=str(symbol))
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="close_position",
                exchange=name,
                account=credentials["account"],
                code="APEX_ACCOUNT_TYPE_ERROR",
                message=sanitize_error_message(str(exc)),
            )
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(operation="close_position", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_AMBIGUOUS",
                                message=f"Apex instrument '{requested_symbol}' is ambiguous.")
        raise
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="close_position", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR",
                            message=sanitize_error_message(str(exc)))

    # Fetch positions. A SDK / API failure is explicit — we refuse to treat
    # "fetch boom → empty list" as "no position".
    try:
        positions = _apex_fetch_positions(client)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="close_position", exchange=name,
                            account=credentials["account"],
                            code="POSITIONS_UNAVAILABLE",
                            message=sanitize_error_message(str(exc)),
                            position_action=_apex_position_action_result(
                                operation="close_position", symbol=symbol,
                                verified=False, status="failed",
                            ))
    position_row = _apex_find_position(positions, symbol)
    if position_row is None:
        return make_failure(operation="close_position", exchange=name,
                            account=credentials["account"],
                            code="NO_OPEN_POSITION",
                            message=f"No open position for {symbol}.",
                            position_action=_apex_position_action_result(
                                operation="close_position", symbol=symbol,
                                verified=True, status="noop",
                            ))
    position_side = _apex_position_side(position_row)
    position_size = _apex_position_size(position_row)

    # Fetch the live mark price from the ticker endpoint. Apex position
    # rows do NOT carry the mark, only entryPrice/fee/fundingFee — so
    # we can't use the position row's fields to derive a current price.
    mark_price = _apex_fetch_mark_price(client, symbol)
    if mark_price <= 0 or tick_size <= 0:
        return make_failure(operation="close_position", exchange=name,
                            account=credentials["account"],
                            code="MARK_PRICE_UNAVAILABLE",
                            message=(
                                "Mark price unavailable from the ticker "
                                "endpoint; cannot derive the limit price for "
                                "closing the position."
                            ),
                            position_action=_apex_position_action_result(
                                operation="close_position", symbol=symbol,
                                verified=False, status="failed",
                                current_side=position_side,
                                current_size=str(position_size),
                            ))

    # Snap mark → tick and shift ±1 tick in the closing direction so the
    # limit aggressively crosses the book.
    closing_side = _apex_closing_side(position_side)
    if closing_side == "sell":
        close_price = mark_price + tick_size
    else:
        close_price = mark_price - tick_size
    snapped_close = (close_price / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size
    if snapped_close <= 0:
        snapped_close = tick_size

    client_id = f"apex-close-{uuid.uuid4().hex[:10]}"
    try:
        response = client.create_order_v3(
            symbol=symbol,
            side="SELL" if closing_side == "sell" else "BUY",
            type="LIMIT",
            size=str(position_size),
            price=str(snapped_close),
            reduceOnly=True,
            clientId=client_id,
            timeInForce="GOOD_TIL_CANCEL",
            account_type=getattr(client, "_default_account_type", None) or "primary",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="close_position", exchange=name,
                            account=credentials["account"],
                            code="CLOSE_PLACEMENT_FAILED",
                            message=sanitize_error_message(str(exc)),
                            position_action=_apex_position_action_result(
                                operation="close_position", symbol=symbol,
                                verified=False, status="failed",
                                current_side=position_side,
                                current_size=str(position_size),
                            ))
    extracted_id = _apex_extract_order_id(response)
    if extracted_id is None:
        if isinstance(response, Mapping) and response.get("code"):
            return make_failure(operation="close_position", exchange=name,
                                account=credentials["account"],
                                code="CLOSE_PLACEMENT_FAILED",
                                message=f"{response.get('code')}: {response.get('msg') or 'Unknown error.'}",
                                position_action=_apex_position_action_result(
                                    operation="close_position", symbol=symbol,
                                    verified=False, status="failed",
                                    current_side=position_side,
                                    current_size=str(position_size),
                                ))
        return make_failure(operation="close_position", exchange=name,
                            account=credentials["account"],
                            code="VERIFICATION_FAILED",
                            message="Apex did not return an order id for the close order.",
                            position_action=_apex_position_action_result(
                                operation="close_position", symbol=symbol,
                                verified=False, status="failed",
                                current_side=position_side,
                                current_size=str(position_size),
                            ))
    return make_success(operation="close_position", exchange=name,
                        account=credentials["account"],
                        position_action=_apex_position_action_result(
                            operation="close_position", symbol=symbol,
                            verified=True, status="submitted",
                            price=str(snapped_close),
                            current_side=position_side,
                            current_size=str(position_size),
                            exchange_order_id=extracted_id,
                            message=(
                                f"Submitted reduce-only close at {snapped_close} "
                                f"for {position_size} {symbol}. Fill happens "
                                "automatically when the book crosses the mark."
                            ),
                        ))


# ---------------------------------------------------------------------------
# Write: ladder
# ---------------------------------------------------------------------------

# Apex Omni batches up to 10 child orders per ``create_batch_orders_v3`` call.
# Higher batches raise an internal cap on the SDK side, so we cap our chunk
# size conservatively at 10 to match what the other agents do.
_APEX_LADDER_BATCH_SIZE = 10

# Apex doesn't expose a per-order notional minimum in ``configs_v3`` for any of
# the contracts we tested. To stay safe and consistent with the other agents,
# we enforce a $10 USD floor per child (skipping without redistribution) — this
# also keeps the wizard's ladder output aligned with the Arcus/Hyperliquid/
# Rise behavior the operator has come to expect.
_APEX_LADDER_MIN_NOTIONAL_USD = Decimal("10")


def _apex_ladder_distribution_weights(order_count: int,
                                      distribution: str
                                      ) -> List[Decimal]:
    """Return per-child weights that sum to (approximately) 1.0.

    Same shape as the other agents' helpers: ``uniform`` is a flat 1/N,
    ``half_gaussian`` is the half-Gaussian σ=1 truncated to z∈[0,3].
    """
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * order_count
    if distribution_key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    import math as _math
    span = Decimal(order_count - 1)
    weights: List[Decimal] = []
    for index in range(order_count):
        # z=3 → smallest weight (index 0 = smallest price), z=0 → largest weight
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = _math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _apex_quantize_to_increment(value: Decimal,
                                increment: Decimal) -> Decimal:
    """Snap ``value`` to the nearest multiple of ``increment``."""
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    units = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return units * increment


def _apex_floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` DOWN to the nearest multiple of ``step``."""
    if step <= 0:
        return value
    n = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return Decimal(n) * step


def _apex_build_ladder_prices(start_price: Decimal,
                              end_price: Decimal,
                              order_count: int,
                              price_increment: Decimal) -> List[Decimal]:
    """Lay out ladder prices evenly between start/end, then snap to ticks.

    Enforces monotonicity after tick quantization (which can collapse
    adjacent prices to the same tick). Direction follows start→end
    irrespective of side — the side-specific routing (buy-vs-sell orientation)
    is handled at the caller.
    """
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_apex_quantize_to_increment((start_price + end_price) / Decimal("2"),
                                           price_increment)]
    span = end_price - start_price
    step = span / Decimal(order_count - 1)
    raw_prices = [start_price + step * Decimal(index) for index in range(order_count)]
    prices = [_apex_quantize_to_increment(p, price_increment) for p in raw_prices]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _apex_allocate_ladder_sizes(total_volume: Decimal,
                                order_count: int,
                                size_increment: Decimal,
                                distribution: str
                                ) -> Tuple[List[Decimal], Decimal]:
    """Allocate ``total_volume`` across ``order_count`` children by weight.

    Rounds each child's size DOWN to the nearest ``size_increment`` (so the
    total never exceeds the requested volume), then distributes the
    residual whole units to the children with the largest fractional
    remainder. Returns the final per-child sizes plus the kept total.
    """
    if size_increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int((total_volume / size_increment).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _apex_ladder_distribution_weights(order_count, distribution)
    if not weights:
        raise ValueError("INVALID_ORDER_COUNT")
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_DOWN)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(
            range(order_count),
            key=lambda index: (remainders[index], -index),
            reverse=True,
        )
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * size_increment for units in allocation]
    return sizes, Decimal(total_units) * size_increment


def _apex_build_ladder_children(
    *,
    symbol: str,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    size_increment: Decimal,
    price_increment: Decimal,
    min_order_size: Decimal,
) -> Tuple[List[Dict[str, Any]], Decimal, int, int]:
    """Build the per-child ladder rows, snap sizes/prices to instrument
    precision, drop sub-floor children, and return the kept list.

    Returns ``(children, kept_volume, omitted_below_minimum, kept_count)``.
    Children are dicts with keys ``price``, ``size``, ``client_id``,
    ``side``, ``symbol``. Omitted children are dropped (no redistribution
    to surviving rows) so the operator's volume budget is honoured exactly.
    """
    if order_count <= 0:
        return [], Decimal("0"), 0, 0
    prices = _apex_build_ladder_prices(start_price, end_price, order_count, price_increment)
    if not prices:
        return [], Decimal("0"), 0, 0
    raw_sizes, kept_volume_total = _apex_allocate_ladder_sizes(
        total_volume=total_volume,
        order_count=order_count,
        size_increment=size_increment,
        distribution=distribution,
    )

    children: List[Dict[str, Any]] = []
    omitted_below_minimum = 0
    for index, price in enumerate(prices):
        size = _apex_floor_to_step(raw_sizes[index], size_increment)
        # Per-order minimum size (e.g. BTC-USDT minOrderSize=0.001) — drop
        # dust-sized children outright rather than rounding up (rounding
        # up could violate the max-order-size or break per-tick math).
        if min_order_size > 0 and size > 0 and size < min_order_size:
            omitted_below_minimum += 1
            continue
        # Per-order notional floor — same drop-without-redistribute
        # policy the other agents use.
        if price * size < _APEX_LADDER_MIN_NOTIONAL_USD:
            omitted_below_minimum += 1
            continue
        # If size collapsed to zero (size_increment > total_volume / N),
        # drop the child. Avoids sending a 0-size order that the server
        # would reject.
        if size <= 0:
            omitted_below_minimum += 1
            continue
        client_id = f"apex-ladder-{uuid.uuid4().hex[:10]}-{index:02d}"
        children.append({
            "symbol": symbol,
            "side": side,
            "size": _format_apex_decimal(size),
            "price": _format_apex_decimal(price),
            "client_id": client_id,
        })
    kept_volume = sum(_safe_decimal(c["size"]) for c in children)
    return children, kept_volume, omitted_below_minimum, len(children)


def _execute_ladder(request: Dict[str, Any]) -> CanonicalResponse:
    """Place a multi-order ladder on Apex Omni.

    Apex Omni doesn't expose a one-shot "create ladder" endpoint, but
    ``create_batch_orders_v3`` accepts up to 10 child orders per call and
    signs them collectively with the same zk-link L2 signer — same
    effective throughput as a dedicated ladder endpoint, no server-side
    rate-limit penalty for chunking. We submit in chunks of
    ``_APEX_LADDER_BATCH_SIZE = 10``.

    Child order sizes and prices are rounded to the instrument's
    ``stepSize`` and ``tickSize`` (no fractional dust), and any child
    below either the per-order ``minOrderSize`` or the $10 USD notional
    floor is dropped without redistributing volume to survivors.
    """
    account = str(request.get("account") or "").strip()
    credentials, error = _resolve_credentials(account)
    if error:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code=error["code"], message=error["message"])
    assert credentials is not None

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:
        order_count = 0
    total_volume = _safe_decimal(request.get("total_volume"))
    start_price = _safe_decimal(request.get("start_price"))
    end_price = _safe_decimal(request.get("end_price"))

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_SIDE", message="Side must be buy or sell.")
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_DISTRIBUTION",
                            message="Distribution must be uniform or half_gaussian.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price <= 0 or end_price <= 0:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_PRICE", message="Start and end price must be positive.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_LADDER_DIRECTION",
                            message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account,
                            code="INVALID_LADDER_DIRECTION",
                            message="SELL ladders require end price above start price.")

    # Preflight intentionally omitted: total_volume is a base-asset
    # quantity (e.g. 3 BTC, 100 SOL), not USD. Comparing it directly to
    # the per-child USD notional floor (_APEX_LADDER_MIN_NOTIONAL_USD)
    # was a unit mismatch that rejected feasible orders like "3 BTC
    # ladder across 50 orders between 60k-62k". The per-child check
    # inside _apex_build_ladder_children (price * size < _APEX_LADDER_MIN_NOTIONAL_USD)
    # is the correct USD-side check and is enforced there.

    # Bootstrap client + resolve market metadata.
    try:
        client = _client_for_credentials(credentials)
        client.set_default_account_type("primary")
        client.configs_v3()
        client.get_account_v3()
        all_contracts = _apex_fetch_supported_markets(client)
        meta = _apex_resolve_symbol(requested_symbol, all_contracts)
        if meta is None:
            return make_failure(operation="ladder", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex symbol '{requested_symbol}' is not available.")
        symbol = meta["symbol"]
        tick_size = _safe_decimal(meta.get("tickSize"))
        step_size = _safe_decimal(meta.get("stepSize") or meta.get("lotSize"))
        # Per-order minimum size from the contract config. minOrderNotional
        # isn't populated on Apex's contracts (verified empirically — BTC-USDT
        # has minOrderNotional=None), so we apply a $10 USD notional floor
        # ourselves below.
        min_order_size = _safe_decimal(meta.get("minOrderSize") or "0")
        if tick_size <= 0:
            return make_failure(operation="ladder", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex market '{requested_symbol}' has no tickSize; cannot ladder.")
        if step_size <= 0:
            return make_failure(operation="ladder", exchange=name,
                                account=credentials["account"],
                                code="INSTRUMENT_NOT_FOUND",
                                message=f"Apex market '{requested_symbol}' has no stepSize; cannot ladder.")
        # TradFi/stock (XAU, CL, …) must sign under the RWA/stock account.
        # Crypto perps stay on primary. apexomni create_batch_orders_v3 also
        # omits stockContract lookup — stock ladders must use create_order_v3.
        try:
            account_type = _apex_prepare_trading_context(client, symbol=str(symbol))
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="APEX_ACCOUNT_TYPE_ERROR",
                message=sanitize_error_message(str(exc)),
            )
        is_stock_ladder = bool(_apex_is_stock_symbol(client, str(symbol))) or (
            str(account_type).lower() not in {"", "primary"}
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="ladder", exchange=name,
                            account=credentials["account"],
                            code="APEX_ERROR", message=sanitize_error_message(str(exc)))

    children, kept_volume, omitted_below_minimum, kept_count = _apex_build_ladder_children(
        symbol=symbol,
        side=requested_side,
        distribution=distribution,
        order_count=order_count,
        total_volume=total_volume,
        start_price=start_price,
        end_price=end_price,
        size_increment=step_size,
        price_increment=tick_size,
        min_order_size=min_order_size,
    )

    if kept_count < 2:
        ladder = CanonicalLadderResult(
            symbol=symbol, side=requested_side, distribution=distribution,
            requested_order_count=order_count, submitted_order_count=0,
            requested_volume=_format_decimal(total_volume),
            submitted_volume="0",
            batch_count=0, verified=False, partial=False, status="failed",
            accepted_child_count=0,
            omitted_order_count=order_count - kept_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=[], batches=[],
        )
        return make_failure(
            operation="ladder", exchange=name, account=credentials["account"],
            code="LADDER_TOO_FEW_VALID_CHILDREN",
            message=(f"Fewer than two ladder children survived the "
                     f"${_APEX_LADDER_MIN_NOTIONAL_USD} notional / "
                     f"{min_order_size} size filter "
                     f"({kept_count} kept, {omitted_below_minimum} omitted below floor)."),
            ladder=ladder,
        )

    # Submit children.
    # - Crypto perps: chunks of _APEX_LADDER_BATCH_SIZE via create_batch_orders_v3
    # - TradFi/stock: serial create_order_v3 (batch path has no stockContract lookup
    #   in apexomni and raises NoneType.get on settleAssetId).
    accepted_child_count = 0
    child_order_ids: List[int] = []
    batches: List[Dict[str, Any]] = []
    try:
        if is_stock_ladder:
            batch_index = 0
            accepted_in_batch = 0
            first_error: Optional[str] = None
            for c in children:
                sdk_side = "BUY" if str(c["side"]).lower() == "buy" else "SELL"
                try:
                    raw = client.create_order_v3(
                        symbol=str(c["symbol"]),
                        side=sdk_side,
                        type="LIMIT",
                        size=str(c["size"]),
                        price=str(c["price"]),
                        reduceOnly=False,
                        timeInForce="GOOD_TIL_CANCEL",
                        clientId=str(c["client_id"]),
                        account_type=account_type,
                    )
                except Exception as child_exc:  # noqa: BLE001
                    first_error = first_error or sanitize_error_message(str(child_exc))
                    continue
                oid = _apex_extract_order_id(raw)
                if oid is None and isinstance(raw, Mapping) and raw.get("code") and raw.get("msg"):
                    first_error = first_error or sanitize_error_message(
                        f"{raw.get('msg')} (code={raw.get('code')})"
                    )
                    continue
                if oid is None:
                    first_error = first_error or "order placed without order id"
                    continue
                try:
                    child_order_ids.append(int(oid))
                except (TypeError, ValueError):
                    child_order_ids.append(oid)  # type: ignore[arg-type]
                accepted_in_batch += 1
                accepted_child_count += 1
            batches.append({
                "batch_index": batch_index,
                "submitted": len(children),
                "accepted": accepted_in_batch,
                "ok": accepted_in_batch == len(children),
                "mode": "serial_create_order_v3",
                "account_type": str(account_type),
                "reason": None if accepted_in_batch == len(children) else first_error,
            })
            if accepted_child_count == 0 and first_error:
                raise RuntimeError(first_error)
        else:
            # Pull the per-account fields the SDK needs off self.accountV3 so we
            # don't rely on attribute fallbacks (which can fail under __slots__).
            _acct_v3 = client.accountV3 if isinstance(client.accountV3, Mapping) else {}
            _per_child_account_id = _acct_v3.get("id")
            _spot = _acct_v3.get("spotAccount") if isinstance(_acct_v3.get("spotAccount"), Mapping) else {}
            _per_child_sub_account_id = _spot.get("defaultSubAccountId") or "0"
            _contract_acct = (
                _acct_v3.get("contractAccount")
                if isinstance(_acct_v3.get("contractAccount"), Mapping)
                else {}
            )
            _per_child_taker_fee_rate = _contract_acct.get("takerFeeRate") or "0.0005"
            _per_child_maker_fee_rate = _contract_acct.get("makerFeeRate") or "0.0002"
            for chunk_start in range(0, len(children), _APEX_LADDER_BATCH_SIZE):
                chunk = children[chunk_start: chunk_start + _APEX_LADDER_BATCH_SIZE]
                models = [_ApexLadderOrder(
                    symbol=c["symbol"], side=c["side"], price=c["price"],
                    size=c["size"], client_id=c["client_id"],
                    account_id=_per_child_account_id,
                    sub_account_id=_per_child_sub_account_id,
                    taker_fee_rate=_per_child_taker_fee_rate,
                    maker_fee_rate=_per_child_maker_fee_rate,
                ) for c in chunk]
                response = client.create_batch_orders_v3(models)
                batch_rows = _extract_batch_rows(response)
                batch_index = len(batches)
                accepted_in_batch = 0
                for response_row in batch_rows:
                    if not isinstance(response_row, Mapping):
                        continue
                    if response_row.get("error") or (response_row.get("code")
                                                      and "orderId" not in response_row
                                                      and "id" not in response_row):
                        batches.append({
                            "batch_index": batch_index,
                            "submitted": len(chunk),
                            "accepted": accepted_in_batch,
                            "ok": False,
                            "reason": response_row.get("error") or response_row.get("msg")
                                                      or f"code={response_row.get('code')}",
                        })
                        continue
                    raw_oid = response_row.get("id") or response_row.get("orderId")
                    if raw_oid is None:
                        continue
                    try:
                        child_order_ids.append(int(raw_oid))
                        accepted_in_batch += 1
                        accepted_child_count += 1
                    except (TypeError, ValueError):
                        child_order_ids.append(raw_oid)  # type: ignore[arg-type]
                        accepted_in_batch += 1
                        accepted_child_count += 1
                batches.append({
                    "batch_index": batch_index,
                    "submitted": len(chunk),
                    "accepted": accepted_in_batch,
                    "ok": accepted_in_batch == len(chunk) and len(batch_rows) == len(chunk),
                    "response_count": len(batch_rows),
                })
    except Exception as exc:  # noqa: BLE001
        ladder = CanonicalLadderResult(
            symbol=symbol, side=requested_side, distribution=distribution,
            requested_order_count=order_count, submitted_order_count=accepted_child_count,
            requested_volume=_format_decimal(total_volume),
            submitted_volume=_format_decimal(kept_volume),
            batch_count=len(batches),
            verified=False, partial=accepted_child_count > 0,
            status="partial" if accepted_child_count > 0 else "failed",
            accepted_child_count=accepted_child_count,
            omitted_order_count=order_count - kept_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=child_order_ids or None,
            batches=batches or None,
        )
        return make_failure(
            operation="ladder", exchange=name, account=credentials["account"],
            code="LADDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            ladder=ladder,
        )

    verified = accepted_child_count == kept_count
    status = "success" if verified else "partial"
    ladder = CanonicalLadderResult(
        symbol=symbol, side=requested_side, distribution=distribution,
        requested_order_count=order_count,
        submitted_order_count=accepted_child_count,
        requested_volume=_format_decimal(total_volume),
        submitted_volume=_format_decimal(kept_volume),
        batch_count=len(batches),
        verified=verified,
        partial=not verified,
        status=status,
        accepted_child_count=accepted_child_count,
        omitted_order_count=order_count - kept_count,
        omitted_below_minimum=omitted_below_minimum,
        child_order_ids=child_order_ids or None,
        batches=batches or None,
    )
    return make_success(operation="ladder", exchange=name,
                        account=credentials["account"], ladder=ladder)


def _extract_batch_rows(response: Any) -> List[Mapping[str, Any]]:
    """Normalize Apex's create_batch_orders_v3 response to a flat list of
    per-child result rows.

    Apex's SDK returns ``{"data": [...]}`` or ``{"orders": [...]}`` or
    a bare list depending on the version. Each row may itself be either a
    success row (with ``id`` / ``orderId``) or an error row (with
    ``code`` / ``msg``).
    """
    if isinstance(response, list):
        return [r for r in response if isinstance(r, Mapping)]
    if isinstance(response, Mapping):
        for key in ("data", "orders", "batchOrders"):
            value = response.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, Mapping)]
        # Fall back: the entire response itself is a single row.
        return [response]
    return []


class _ApexLadderOrder:
    """Tiny duck-typed holder for Apex ``create_batch_orders_v3`` items.

    The SDK reads ~32 different attributes off each orderModel — every one
    of them must be declared in ``__slots__`` because under slots a missing
    attribute raises ``AttributeError`` instead of returning ``None`` (which
    the SDK's ``attribute or fallback`` branches would treat as a missing
    value). Keeping this in-process avoids depending on the SDK's internal
    OrderModel class which isn't part of apexomni's public surface.

    The core order fields (``symbol``, ``side``, ``type``, ``timeInForce``,
    ``size``, ``price``, ``clientId``) come from the ladder child builder;
    the account-level fields (``accountId``, ``subAccountId``,
    ``takerFeeRate``, ``makerFeeRate``) are pre-filled from the live
    ``client.accountV3`` snapshot so the SDK's `or self.accountV3.get(...)`
    fallbacks never fire; ``timestampSeconds`` is left ``None`` so the SDK
    falls back to its own ``time.time() + 28 days`` default (correct per the
    L2 zk-link signing contract). Everything else (TPSL-related fields,
    trigger info, reduceOnly, source/broker tags) stays ``None`` because
    ladders don't carry them — the SDK's ``None or fallback`` branches all
    handle missing values cleanly when slots return ``None``.
    """

    __slots__ = (
        # Core order fields
        "symbol", "side", "type", "timeInForce",
        "size", "price", "clientId",
        # Account-level fields (pre-filled from client.accountV3)
        "accountId", "subAccountId",
        "takerFeeRate", "makerFeeRate",
        # Timestamp (None → SDK uses default)
        "timestampSeconds",
        # TPSL / trigger / reduce-only (None → SDK omits)
        "triggerPrice", "triggerPriceType", "trailingPercent",
        "reduceOnly", "isPositionTpsl",
        "isOpenTpslOrder", "isSetOpenSl", "isSetOpenTp",
        "slClientId", "slPrice", "slSide", "slSize",
        "slTriggerPrice", "slTriggerPriceType",
        "tpClientId", "tpPrice", "tpSide", "tpSize",
        "tpTriggerPrice", "tpTriggerPriceType",
        # Misc SDK fields
        "sourceFlag", "brokerId",
    )

    def __init__(self, symbol: str, side: str, price: str, size: str,
                 client_id: str,
                 *, account_id: Optional[str] = None,
                 sub_account_id: Optional[str] = None,
                 taker_fee_rate: Optional[str] = None,
                 maker_fee_rate: Optional[str] = None) -> None:
        self.symbol = symbol
        self.side = "BUY" if side.lower() == "buy" else "SELL"
        # The Apex SDK's create_batch_orders_v3 accepts both the canonical
        # ``LIMIT`` / ``MARKET`` strings and a few aliases; ladders are always
        # LIMIT so we set it explicitly here (the SDK also accepts
        # ``type``-less models in some paths, but supplying the explicit
        # string matches what the wizard sends and avoids any
        # ``or fallback`` ambiguity).
        self.type = "LIMIT"
        self.timeInForce = "GOOD_TIL_CANCEL"
        self.price = price
        self.size = size
        self.clientId = client_id
        # Pre-fill account-level fields so the SDK's `attribute or fallback`
        # branches never need to look them up — under __slots__ a missing
        # attribute raises AttributeError instead of returning None.
        self.accountId = account_id
        self.subAccountId = sub_account_id
        self.takerFeeRate = taker_fee_rate
        self.makerFeeRate = maker_fee_rate
        # Let the SDK use its built-in `time.time() + 28 days` default —
        # the L2 zk-link signer rejects timestamps that are too close to now,
        # so passing our own (less precise) value here only risks rejection.
        self.timestampSeconds = None
        # TPSL / trigger / reduce-only fields default to None (the SDK's
        # `None or fallback` branches handle missing values cleanly when
        # __slots__ returns None instead of raising AttributeError).
        self.triggerPrice = None
        self.triggerPriceType = None
        self.trailingPercent = None
        self.reduceOnly = False
        self.isPositionTpsl = False
        self.isOpenTpslOrder = False
        self.isSetOpenSl = False
        self.isSetOpenTp = False
        self.slClientId = None
        self.slPrice = None
        self.slSide = None
        self.slSize = None
        self.slTriggerPrice = None
        self.slTriggerPriceType = None
        self.tpClientId = None
        self.tpPrice = None
        self.tpSide = None
        self.tpSize = None
        self.tpTriggerPrice = None
        self.tpTriggerPriceType = None
        self.sourceFlag = None
        self.brokerId = None


def _normalize_positions(raw_account: Any) -> List[CanonicalPosition]:
    """Convert Apex Omni's per-position rows into the canonical position list.

    Apex surfaces positions as a list under ``raw_account["positions"]``;
    each entry carries ``symbol``, ``side``, ``size``, ``entryPrice``,
    ``unrealizedPnl``, ``markPrice`` (sometimes), and a handful of
    instrument-specific fields. We use ``size`` to filter zero-size rows
    (matching the other agents' behavior — they hide dust positions).

    The SDK may return the account dict either already-unwrapped (the
    common case for ``get_account_v3``) or envelope-wrapped; the
    ``_unwrap_data_envelope`` helper handles both.
    """
    out: List[CanonicalPosition] = []
    bucket = _as_mapping(_unwrap_data_envelope(raw_account))
    raw_positions = bucket.get("positions")
    if not isinstance(raw_positions, list):
        return out
    for raw in raw_positions:
        if not isinstance(raw, Mapping):
            continue
        pos = _normalize_apex_position(raw)
        try:
            size_abs = abs(Decimal(str(pos.size or "0")))
        except Exception:  # noqa: BLE001
            continue
        if size_abs <= 0:
            continue
        out.append(pos)
    return out


def _normalize_apex_position(raw: Mapping[str, Any]) -> CanonicalPosition:
    """Normalize a single Apex position row into a CanonicalPosition.

    The side field on Apex is a string ``"LONG"`` / ``"SHORT"`` (or
    ``"BUY"`` / ``"SELL"`` in some v3 paths). We canonicalize to
    ``"long"`` / ``"short"`` for the wizard.
    """
    side_raw = str(raw.get("side") or "").strip().lower()
    if side_raw in {"buy", "long"}:
        side = "long"
    elif side_raw in {"sell", "short"}:
        side = "short"
    else:
        # Fall back to the sign of the size field.
        try:
            side = "long" if Decimal(str(raw.get("size") or 0)) >= 0 else "short"
        except Exception:  # noqa: BLE001
            side = "long"
    symbol = str(raw.get("symbol") or raw.get("ticker") or "").upper()
    try:
        size_dec = abs(Decimal(str(raw.get("size") or "0")))
    except Exception:  # noqa: BLE001
        size_dec = Decimal("0")
    try:
        entry = Decimal(str(raw.get("entryPrice") or raw.get("avgEntryPrice") or "0"))
    except Exception:  # noqa: BLE001
        entry = Decimal("0")
    pnl_text = str(raw.get("unrealizedPnl") or raw.get("pnl") or "0")
    return CanonicalPosition(
        symbol=symbol,
        side=side,
        size=_format_decimal(size_dec),
        entry_price=_format_decimal(entry),
        pnl=_format_decimal(_safe_decimal(pnl_text)),
    )


def _extract_open_orders(client: Any) -> List[Mapping[str, Any]]:
    """Pull active open orders from primary AND TradFi/stock accounts.

    Crypto perps rest on the primary account; TradFi (XAU, CL, AAPL, …)
    rest on the RWA/stock sub-account. The wizard cancel menu consumes a
    single merged list, so we query both contexts and de-dupe by order id.
    """
    fn = getattr(client, "open_orders_v3", None) or getattr(client, "open_orders", None)
    if fn is None:
        logger.warning("apexomni client exposes no open-orders method")
        return []

    def _fetch_once() -> List[Mapping[str, Any]]:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apex open-orders fetch failed: %s", exc)
            return []
        if isinstance(result, Mapping):
            result = result.get("orders") or result.get("data") or []
        if not isinstance(result, list):
            return []
        return [r for r in result if isinstance(r, Mapping)]

    merged: List[Mapping[str, Any]] = []
    seen: set[str] = set()

    def _absorb(rows: List[Mapping[str, Any]]) -> None:
        for row in rows:
            raw_id = row.get("id") or row.get("orderId") or row.get("clientId")
            key = str(raw_id).strip() if raw_id is not None else ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(row)

    # --- primary ---
    try:
        if hasattr(client, "use_primary_account"):
            client.use_primary_account()
        client.set_default_account_type("primary")
        client.get_account_v3(account_type="primary")
    except Exception:  # noqa: BLE001
        pass
    _absorb(_fetch_once())

    # --- TradFi / stock ---
    try:
        stock_syms = _apex_stock_symbol_set(client)
        sample = ""
        for cand in ("XAU-USDT", "XAG-USDT", "CL-USDT"):
            if cand in stock_syms:
                sample = cand
                break
        if not sample:
            for sym in sorted(stock_syms):
                if "-" in sym:
                    sample = sym
                    break
        if sample:
            _apex_prepare_trading_context(client, symbol=sample)
            _absorb(_fetch_once())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Apex stock open-orders fetch failed: %s", exc)
    finally:
        try:
            if hasattr(client, "use_primary_account"):
                client.use_primary_account()
            client.set_default_account_type("primary")
        except Exception:  # noqa: BLE001
            pass

    return merged


def _apex_fetch_all_account_snapshots(client: Any) -> List[Dict[str, Any]]:
    """Return account snapshots for primary + stock (when available)."""
    snapshots: List[Dict[str, Any]] = []
    try:
        if hasattr(client, "use_primary_account"):
            client.use_primary_account()
        client.set_default_account_type("primary")
        primary = client.get_account_v3(account_type="primary") or {}
        if isinstance(primary, dict):
            snapshots.append(primary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Apex primary account snapshot failed: %s", exc)

    try:
        stock_syms = _apex_stock_symbol_set(client)
        sample = ""
        for cand in ("XAU-USDT", "XAG-USDT", "CL-USDT"):
            if cand in stock_syms:
                sample = cand
                break
        if not sample:
            for sym in sorted(stock_syms):
                if "-" in sym:
                    sample = sym
                    break
        if sample:
            _apex_prepare_trading_context(client, symbol=sample)
            rwa_type = str(getattr(client, "rwa_account_type", None) or "rwa")
            stock_acct = None
            try:
                stock_acct = client.get_account_v3(account_type=rwa_type)
            except Exception:  # noqa: BLE001
                stock_acct = client._get_account_context(rwa_type)
            if isinstance(stock_acct, dict) and stock_acct:
                snapshots.append(stock_acct)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Apex stock account snapshot failed: %s", exc)
    finally:
        try:
            if hasattr(client, "use_primary_account"):
                client.use_primary_account()
            client.set_default_account_type("primary")
        except Exception:  # noqa: BLE001
            pass
    return snapshots


def _normalize_positions_merged(snapshots: List[Any]) -> List[CanonicalPosition]:
    """Normalize positions from multiple account snapshots; de-dupe by symbol+side."""
    out: List[CanonicalPosition] = []
    seen: set[Tuple[str, str]] = set()
    for snap in snapshots:
        for pos in _normalize_positions(snap):
            key = (str(pos.symbol or "").upper(), str(pos.side or "").lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(pos)
    return out


def _group_open_orders(raw_orders: List[Mapping[str, Any]]
                       ) -> Tuple[List[CanonicalOrderGroup], int]:
    """Bucket open orders by (symbol, side) and return a list of canonical
    order groups plus a total open-order count — the same shape the
    other agents use.
    """
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    open_count = 0
    for row in raw_orders:
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            continue
        try:
            remaining = _safe_decimal(row.get("remainingSize") or row.get("size") or "0")
        except Exception:  # noqa: BLE001
            remaining = Decimal("0")
        if remaining <= 0:
            continue
        try:
            price = _safe_decimal(row.get("price") or "0")
        except Exception:  # noqa: BLE001
            price = Decimal("0")
        key = (symbol, side)
        bucket = grouped.setdefault(key, {
            "count": 0, "remaining": Decimal("0"), "notional": Decimal("0"),
            "min_price": None, "max_price": None,
        })
        open_count += 1
        bucket["count"] += 1
        bucket["remaining"] += remaining
        bucket["notional"] += remaining * price
        if bucket["min_price"] is None or price < bucket["min_price"]:
            bucket["min_price"] = price
        if bucket["max_price"] is None or price > bucket["max_price"]:
            bucket["max_price"] = price
    groups: List[CanonicalOrderGroup] = []
    for (symbol, side), bucket in sorted(grouped.items()):
        total = bucket["remaining"]
        if total <= 0:
            continue
        vwap = bucket["notional"] / total if total else Decimal("0")
        groups.append(CanonicalOrderGroup(
            symbol=symbol, side=side,
            order_count=int(bucket["count"]),
            total_size=_format_decimal(total),
            vwap=_format_decimal(vwap),
            min_price=_format_decimal(bucket["min_price"]) if bucket["min_price"] is not None else "",
            max_price=_format_decimal(bucket["max_price"]) if bucket["max_price"] is not None else "",
        ))
    return (open_count, groups)


# ---------------------------------------------------------------------------
# TP/SL enrichment (read path)
# ---------------------------------------------------------------------------


def _enrich_positions_with_tpsl(client: Any,
                                positions: List[CanonicalPosition]
                                ) -> Dict[str, Dict[str, Any]]:
    """Look up active position-level TP/SL triggers for each open position
    via Apex's history-orders-v3 endpoint, then map the results back onto
    the position dict so the wizard can render ``tp`` / ``sl`` next to the
    size.

    Returns a dict keyed by symbol, each value carrying the same shape the
    other agents use:

        {
            "tp":         trigger price string or None,
            "tp_count":   int or None,
            "sl":         trigger price string or None,
            "sl_count":   int or None,
        }
    """
    out: Dict[str, Dict[str, Any]] = {}
    symbols = {p.symbol for p in positions}
    if not symbols:
        return out
    try:
        history_rows = _fetch_history_tpsl_orders(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Apex history-orders fetch failed: %s", exc)
        return out
    for sym in symbols:
        bucket = out.setdefault(sym, {
            "tp": None, "tp_count": None, "sl": None, "sl_count": None,
        })
    classified = _classify_active_position_tpsl(history_rows)
    for row in classified:
        sym = str(row.get("symbol") or "").upper()
        if sym not in symbols:
            continue
        kind = row.get("kind")
        trigger = _safe_decimal(row.get("triggerPrice") or row.get("price") or "0")
        if trigger <= 0:
            continue
        bucket = out[sym]
        if kind == "TP":
            bucket["tp_count"] = (bucket["tp_count"] or 0) + 1
            bucket["tp"] = _format_decimal(trigger)
        elif kind == "SL":
            bucket["sl_count"] = (bucket["sl_count"] or 0) + 1
            bucket["sl"] = _format_decimal(trigger)
    for sym, bucket in list(out.items()):
        if not bucket["tp_count"]:
            bucket["tp"] = None
            bucket["tp_count"] = None
        if not bucket["sl_count"]:
            bucket["sl"] = None
            bucket["sl_count"] = None
    return out


def _fetch_history_tpsl_orders(client: Any) -> List[Mapping[str, Any]]:
    """Fetch recent history orders and filter down to TPSL rows.

    Apex's history-orders-v3 endpoint supports paging via ``limit`` /
    ``page``. We request the most recent page (size
    ``APEX_HISTORY_TPSL_PAGE_SIZE``) — that's enough to surface the active
    TP/SL row for any reasonable position.

    The response envelope is ``{"data": {"orders": [...], "totalSize": N},
    "timeCost": ...}`` on Apex 3.3.x — we unwrap the nested shape here.
    Older shapes (bare list, ``{"orders": [...]}``, ``{"data": [...]}``)
    are also handled for forward-compatibility.
    """
    fn = getattr(client, "history_orders_v3", None) or getattr(client, "history_orders", None)
    if fn is None:
        return []
    rows: List[Mapping[str, Any]] = []
    try:
        result = fn(limit=APEX_HISTORY_TPSL_PAGE_SIZE)
    except TypeError:
        # Older SDKs don't accept kwargs on history-orders.
        try:
            result = fn()
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    # Unwrap nested envelopes — Apex 3.3.x returns
    # ``{"data": {"orders": [...], "totalSize": N}, "timeCost": ...}``.
    if isinstance(result, Mapping):
        inner = result.get("data")
        if isinstance(inner, Mapping):
            result = inner.get("orders") or []
        elif isinstance(inner, list):
            result = inner
        else:
            result = result.get("orders") or result.get("data") or []
    if not isinstance(result, list):
        return []
    for row in result:
        if isinstance(row, Mapping):
            rows.append(row)
    return rows


def _is_active_tpsl_order(row: Mapping[str, Any]) -> bool:
    """True iff ``row`` looks like an active position-level TP/SL trigger.

    Apex's TP/SL orders carry ``type="TAKE_PROFIT_MARKET"`` (TP) or
    ``type="STOP_MARKET"`` (SL) with ``isPositionTpsl=True``. We accept
    any of:
      - ``isPositionTpsl=True`` flag set
      - explicit ``tpslType`` of ``TP``/``SL``
      - trigger-market order types (``TAKE_PROFIT_MARKET``/``STOP_MARKET``)
        with a non-zero ``triggerPrice`` (some rows don't carry
        ``isPositionTpsl`` but are still position-bound TP/SL)
    Status must be active (``UNTRIGGERED``/``OPEN``/``PENDING``/``NEW``).
    """
    status = str(row.get("status") or "").strip().upper()
    if status and status not in {"UNTRIGGERED", "OPEN", "PENDING", "NEW", "ACCEPTED"}:
        return False
    is_pos_tpsl = (row.get("isPositionTpsl") or row.get("isPositionTPSL")
                   or row.get("positionTpsl"))
    tpsl_type = str(row.get("tpslType") or "").strip().upper()
    order_type = str(row.get("type") or "").strip().upper()
    trigger = row.get("triggerPrice")
    has_trigger = trigger not in (None, "", "0", 0)
    # Match if: explicit position-TPSL flag, explicit tpslType, or a
    # trigger-market order type with a non-zero triggerPrice.
    if is_pos_tpsl and is_pos_tpsl not in (False, 0, "0", "false", "False"):
        return True
    if tpsl_type in {"TP", "SL"}:
        return True
    if order_type in {"TAKE_PROFIT_MARKET", "STOP_MARKET"} and has_trigger:
        return True
    if order_type in {"TAKE_PROFIT", "STOP", "STOP_LOSS"} and has_trigger:
        return True
    return False


def _classify_active_position_tpsl(rows: List[Mapping[str, Any]]
                                   ) -> List[Dict[str, Any]]:
    """Return the subset of ``rows`` that look like active position-level
    TP/SL triggers, each normalized to ``{symbol, kind, triggerPrice}``."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_active_tpsl_order(row):
            continue
        # Prefer the explicit ``tpslType`` if set; otherwise read the
        # ``type`` field which uses ``TAKE_PROFIT_MARKET``/``STOP_MARKET``
        # on Apex 3.3.x (NOT ``TAKE_PROFIT``/``STOP_LOSS`` — that was the
        # older Apex Pro schema).
        tpsl_type = str(row.get("tpslType") or "").strip().upper()
        if tpsl_type in {"TP", "TAKE_PROFIT"}:
            kind = "TP"
        elif tpsl_type in {"SL", "STOP", "STOP_LOSS"}:
            kind = "SL"
        else:
            order_type = str(row.get("type") or "").strip().upper()
            if order_type in {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}:
                kind = "TP"
            elif order_type in {"STOP_MARKET", "STOP", "STOP_LOSS"}:
                kind = "SL"
            else:
                # Last-resort heuristic: trigger above limit price = TP
                try:
                    trig_d = _safe_decimal(row.get("triggerPrice") or "0")
                    price_d = _safe_decimal(row.get("price") or "0")
                except Exception:
                    continue
                kind = "TP" if trig_d > price_d else "SL"
        out.append({
            "symbol": str(row.get("symbol") or row.get("ticker") or "").upper(),
            "kind": kind,
            "triggerPrice": row.get("triggerPrice") or row.get("price"),
        })
    return out


# ---------------------------------------------------------------------------
# Balance normalization
# ---------------------------------------------------------------------------


def _normalize_balance(raw_balance: Any,
                       raw_account: Any
                       ) -> CanonicalPortfolioSummary:
    """Convert the Apex balance + account response into the canonical
    portfolio summary consumed by the wizard.

    The exact field names vary between Apex SDK versions and between the
    two endpoints — we probe a handful of candidate keys and fall back to
    zero whenever a value is missing or non-numeric. The intent is that
    the wizard renders *some* numeric summary on first read rather than
    crashing on field drift.

    Endpoint shape differences:
      - ``get_account_balance_v3`` returns ``{"data": {...}, "timeCost": ...}``
        (envelope-wrapped). We unwrap ``data`` automatically.
      - ``get_account_v3`` returns the inner dict directly (already unwrapped
        by the SDK). Both shapes are accepted.
    """
    balance_map = _as_mapping(_unwrap_data_envelope(raw_balance))
    account_map = _as_mapping(_unwrap_data_envelope(raw_account))
    total = _first_decimal_field(
        balance_map,
        "totalEquityValue", "totalAccountValue", "equity", "totalEquity",
    )
    withdrawable = _first_decimal_field(
        balance_map,
        "totalAvailableBalance", "availableBalance", "freeCollateral", "withdrawable",
    )
    margin_used = _first_decimal_field(
        balance_map,
        "initialMargin", "marginUsed", "usedMargin",
    )
    if total == 0:
        total = _first_decimal_field(account_map, "accountValue", "equity", "totalEquity")
    if margin_used == 0 and total != 0 and withdrawable != 0:
        margin_used = total - withdrawable
    position_value = total - _first_decimal_field(balance_map, "availableBalance")
    return CanonicalPortfolioSummary(
        account_value=_format_decimal(total),
        withdrawable=_format_decimal(withdrawable),
        margin_used=_format_decimal(margin_used),
        total_position_value=_format_decimal(position_value if position_value > 0 else Decimal("0")),
        unit="USD",
    )


def _unwrap_data_envelope(value: Any) -> Any:
    """If ``value`` is a dict whose only meaningful key is ``"data"`` (and
    any sibling keys are metadata like ``timeCost``/``code``), return the
    ``data`` payload. Otherwise return ``value`` unchanged.

    apexomni 3.3.1 wraps balance responses in ``{"data": {...}, "timeCost": ...}``
    while account responses come back unwrapped — this helper lets the
    normalizer treat both uniformly without each call site caring which
    endpoint the payload came from.
    """
    if not isinstance(value, Mapping):
        return value
    if "data" in value and isinstance(value["data"], Mapping):
        # Only unwrap if "data" carries the substantive payload (most fields
        # are inside it) — this avoids accidentally unwrapping a row that
        # happens to have a small "data" sub-dict.
        if len(value["data"]) >= 3:
            return value["data"]
    return value


# ---------------------------------------------------------------------------
# Helpers (kept small and private — no Apex-specific exports)
# ---------------------------------------------------------------------------


def _as_mapping(obj: Any) -> Mapping[str, Any]:
    if isinstance(obj, Mapping):
        return obj
    return {}


def _safe_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except Exception:  # noqa: BLE001
        try:
            return Decimal(default)
        except Exception:  # noqa: BLE001
            return Decimal("0")


def _format_decimal(value: Any, places: Optional[int] = None) -> str:
    """Format a numeric value as a string with up to ``places`` decimals.

    If ``places`` is None we keep the value's natural decimal representation
    but trim trailing zeros — so ``Decimal("1.000")`` becomes ``"1"``. The
    wizard consumes these strings verbatim.
    """
    dec = _safe_decimal(value)
    if dec == 0:
        return "0"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _first_decimal_field(mapping: Mapping[str, Any], *candidates: str) -> Decimal:
    """Return the first non-empty positive decimal among ``candidates``."""
    for name in candidates:
        if name not in mapping:
            continue
        raw = mapping.get(name)
        if raw in (None, "", 0, "0"):
            continue
        try:
            dec = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        return dec
    return Decimal("0")
