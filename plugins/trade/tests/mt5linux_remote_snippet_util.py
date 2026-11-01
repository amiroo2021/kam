"""Discover and validate RPyC remote Python snippets in x_mt5linux_agent.

Used by offline CI tests and optional live-container checks. Prefer capturing
snippets from the real construction paths (builders) so refactors cannot
silently diverge from test-only copies.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

AGENT_REL = Path("plugins/trade/agents/x_mt5linux_agent.py")

# Wine/RPyC side is a frozen CPython 3.8 embed (mt5linux). These must never
# appear as top-level imports inside remote exec strings.
REMOTE_FORBIDDEN_IMPORTS = frozenset(
    {
        "uuid",  # not present in the frozen Wine runtime (regression: HYPEUSD specs)
        "pathlib",  # prefer avoid; pathlib is present but host pathlib Path objects must not ship
        "subprocess",
        "socket",
        "ctypes",
        "multiprocessing",
        "threading",
        "asyncio",
        "concurrent",
        "importlib",
        "pkgutil",
        "sysconfig",
        "venv",
        "pip",
        "setuptools",
        "numpy",
        "pandas",
        "requests",
        "urllib",
        "http",
        "ssl",
        "hashlib",  # may exist but not required remotely
    }
)

# Explicitly allowed remote imports (everything else is suspicious).
REMOTE_ALLOWED_IMPORTS = frozenset(
    {
        "MetaTrader5",
        "time",
        "math",
        "json",  # present in frozen runtime
        "collections",
        "decimal",
        "typing",  # present but discouraged
        "struct",
        "sys",  # only for diagnostics in tests
    }
)

# Modules confirmed missing on the live Wine RPyC runtime (probed once / cached in live test).
KNOWN_REMOTE_MISSING = frozenset({"uuid"})


@dataclass(frozen=True)
class RemoteSnippet:
    """One remote-exec payload (template or fully materialised source)."""

    name: str
    source: str
    origin: str  # builder name or source:line
    mutates: bool = False
    imports: Tuple[str, ...] = ()
    line: Optional[int] = None

    @property
    def forbidden_imports(self) -> Tuple[str, ...]:
        return tuple(i for i in self.imports if i in REMOTE_FORBIDDEN_IMPORTS or i in KNOWN_REMOTE_MISSING)


@dataclass
class CaptureClient:
    """Stand-in MT5 RPyC client that records execute() payloads and returns safe evals."""

    codes: List[str] = field(default_factory=list)
    evals: List[str] = field(default_factory=list)
    eval_map: Dict[str, Any] = field(default_factory=dict)

    def execute(self, code: str) -> None:
        self.codes.append(str(code))

    def eval(self, expression: str) -> Any:
        self.evals.append(str(expression))
        if expression in self.eval_map:
            return self.eval_map[expression]
        # Sensible defaults for read-only builders
        if expression == "sym_payload":
            return {
                "info": {
                    "name": "HYPEUSD",
                    "trade_contract_size": 100.0,
                    "volume_min": 0.01,
                    "volume_max": 100.0,
                    "volume_step": 0.01,
                    "point": 0.01,
                    "digits": 2,
                    "path": r"Cryptos\HYPEUSD",
                    "description": "Hyperliquid coin",
                    "visible": True,
                    "select": True,
                    "bid": 1.0,
                    "ask": 1.0,
                    "last": 0.0,
                    "trade_tick_size": 0.01,
                    "trade_calc_mode": 2,
                },
                "tick": {"bid": 1.0, "ask": 1.0, "last": 0.0, "time": 1, "volume": 0, "time_msc": 0, "rate_close": None},
                "last_error": [1, "Success"],
            }
        if expression == "symbols_payload":
            return {
                "count": 1,
                "symbols": [
                    {
                        "name": "HYPEUSD",
                        "path": r"Cryptos\HYPEUSD",
                        "visible": True,
                        "select": True,
                        "digits": 2,
                        "point": 0.01,
                        "trade_contract_size": 100.0,
                        "volume_min": 0.01,
                        "volume_max": 100.0,
                        "volume_step": 0.01,
                        "trade_mode": 4,
                        "description": "Hyperliquid coin",
                        "currency_base": "USD",
                    }
                ],
                "total_available": 1,
                "query": "HYPE",
            }
        if expression == "candles_payload":
            return {"symbol": "HYPEUSD", "timeframe": 1, "candles": [], "count": 0}
        if expression == "trade_meta":
            return {
                "name": "HYPEUSD",
                "digits": 2,
                "point": 0.01,
                "trade_tick_size": 0.01,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "trade_stops_level": 0,
                "trade_freeze_level": 0,
                "trade_contract_size": 100.0,
                "filling_mode_flags": 1,
                "filling_mode": 2,
                "trade_mode": 4,
                "bid": 1.0,
                "ask": 1.0,
            }
        if expression == "order_send_out":
            return {"ok": False, "retcode": 10026, "comment": "captured-only", "order": 0, "deal": 0}
        if expression == "order_mod_out":
            return {"ok": False, "retcode": -1, "comment": "captured-only"}
        if expression == "order_cancel_out":
            return {"ok": False, "retcode": -1, "comment": "captured-only", "order": 0}
        if expression == "pos_sltp_out":
            return {"ok": False, "retcode": -1, "comment": "captured-only"}
        if expression == "pos_close_out":
            return {"ok": False, "retcode": -1, "comment": "captured-only"}
        if expression == "recon":
            return {"present": False, "ticket": 1}
        if expression == "ord_list":
            return []
        if expression == "snap":
            return {
                "account": {
                    "login": 1,
                    "server": "S",
                    "balance": 1.0,
                    "equity": 1.0,
                    "profit": 0.0,
                    "currency": "USD",
                    "margin": 0.0,
                    "margin_free": 1.0,
                    "margin_level": None,
                    "leverage": 100,
                    "trade_mode": 2,
                    "trade_allowed": True,
                    "trade_expert": True,
                    "limit_orders": 100,
                    "name": "t",
                    "company": "c",
                },
                "terminal": {
                    "connected": True,
                    "trade_allowed": True,
                    "tradeapi_disabled": False,
                    "company": "c",
                    "name": "n",
                    "build": 1,
                },
                "positions_total": 0,
                "orders_total": 0,
                "positions": [],
                "orders": [],
                "last_error": None,
                "version": (500, 1, "x"),
            }
        return None

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> Any:
        return (1, "Success")

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return True


def agent_source_path(repo_root: Optional[Path] = None) -> Path:
    if repo_root is None:
        # tests/ -> trade/ -> plugins/ -> kam root is parents[3] from this file if under tests
        here = Path(__file__).resolve()
        # Prefer living next to tests
        candidate = here.parents[2] / "agents" / "x_mt5linux_agent.py"  # plugins/trade/agents
        if candidate.is_file():
            return candidate
        return Path.cwd() / AGENT_REL
    return Path(repo_root) / AGENT_REL


def parse_imports(source: str) -> Tuple[str, ...]:
    """Top-level import module names from a remote snippet body."""
    mods: List[str] = []
    for m in re.finditer(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)", source):
        mods.append(m.group(1).split(".")[0])
    for m in re.finditer(r"(?m)^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+", source):
        mods.append(m.group(1).split(".")[0])
    # unique preserve order
    seen: Set[str] = set()
    out: List[str] = []
    for x in mods:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)


def _is_mt5_remote_body(body: str) -> bool:
    b = body.lstrip()
    if "MetaTrader5" in body:
        return True
    if "order_send" in body and ("TRADE_ACTION" in body or "action" in body):
        return True
    if re.search(r"\bm\.(symbol_info|account_info|orders_get|positions_get|copy_rates)", body):
        return True
    return False


def discover_snippets_from_source(src: str, *, path_label: str = "x_mt5linux_agent.py") -> List[RemoteSnippet]:
    """Static discovery of triple-quoted strings that look like remote MT5 payloads."""
    snippets: List[RemoteSnippet] = []
    # Map offset -> enclosing function name
    func_spans: List[Tuple[int, int, str]] = []
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                # approximate with next — use source segments
                func_spans.append((node.lineno, end if end else node.lineno, node.name))
    except SyntaxError:
        func_spans = []

    def func_at_line(ln: int) -> str:
        best = "?"
        for start, end, name in func_spans:
            if start <= ln <= max(end, start):
                best = name
        return best

    for m in re.finditer(r'(?P<preamble>.{0,40}?)(?P<q>f?|r?)"""(?P<body>[\s\S]*?)"""', src):
        body = m.group("body")
        if not _is_mt5_remote_body(body):
            continue
        # Skip module docstring
        lineno = src[: m.start()].count("\n") + 1
        if lineno < 5 and "Architecture" in body:
            continue
        preamble = m.group("preamble")
        # Skip pure docstrings attached to def (no code =)
        if re.search(r"def\s+\w+\([^)]*\)\s*:\s*$", preamble.split("\n")[-1] if preamble else ""):
            if "import MetaTrader5" not in body and "order_send" not in body:
                continue
        mutates = "order_send" in body
        fname = func_at_line(lineno)
        name = f"{fname}@{lineno}"
        snippets.append(
            RemoteSnippet(
                name=name,
                source=body,
                origin=f"{path_label}:{lineno}",
                mutates=mutates,
                imports=parse_imports(body),
                line=lineno,
            )
        )
    return snippets



def materialize_template(body: str, mapping: Optional[Dict[str, Any]] = None) -> str:
    """Best-effort materialisation of f-string bodies with {expr!r}/{expr} placeholders."""
    defaults: Dict[str, Any] = {
        "symbol": "HYPEUSD",
        "query": "HYPE",
        "limit_i": 50,
        "timeframe": 1,
        "count": 10,
        "ticket": 1,
        "login": 1,
        "server": "TEST",
        "password": "",
        "vol": 0.01,
        "px": 1.0,
        "sl_v": 0.0,
        "tp_v": 0.0,
        "fill": 2,
        "otype": 3,
        "magic": 26092601,
        "comment": "test",
        "expiration": 0,
        "vol_expr": "None",
        "price": 1.0,
        "sl": 0.0,
        "tp": 0.0,
        "volume": 0.01,
        "meta": {"name": "HYPEUSD"},
        "TRADE_ACTION_PENDING": 5,
        "TRADE_ACTION_MODIFY": 7,
        "TRADE_ACTION_REMOVE": 8,
        "TRADE_ACTION_DEAL": 1,
        "TRADE_ACTION_SLTP": 6,
        "ORDER_TIME_GTC": 0,
        "ORDER_FILLING_RETURN": 2,
        "ORDER_TYPE_BUY": 0,
        "ORDER_TYPE_SELL": 1,
        "MT5LINUX_MAGIC": 26092601,
    }
    if mapping:
        defaults.update(mapping)
    q = chr(39) + chr(34)

    def unwrap(raw: str) -> str:
        return raw.strip().strip(q)

    def eval_expr(expr: str) -> str:
        e = expr.strip()
        as_repr = False
        if e.endswith("!r"):
            as_repr = True
            e = e[:-2].strip()
        elif e.endswith("!s"):
            e = e[:-2].strip()
        mcall = re.match(r"^(int|float|str|bool|repr)\((.+)\)$", e)
        if mcall:
            fn, inner = mcall.group(1), mcall.group(2)
            raw = eval_expr(inner)
            core = unwrap(raw)
            if fn == "int":
                try:
                    return str(int(float(core)))
                except Exception:
                    return "0"
            if fn == "float":
                try:
                    return str(float(core))
                except Exception:
                    return "0.0"
            if fn in {"str", "repr"} or as_repr:
                return repr(core)
            if fn == "bool":
                return "True"
        if " or " in e:
            for part in e.split(" or "):
                val = eval_expr(part.strip())
                if val not in {"None", "0", "0.0", "''", '""'}:
                    if as_repr and val[:1] not in q:
                        return repr(unwrap(val))
                    return val
            return "None"
        mget = re.match(r"^([A-Za-z_]\w*)\.get\((['\"])(.+?)\2\)$", e)
        if mget:
            obj, key = mget.group(1), mget.group(3)
            base = defaults.get(obj)
            if isinstance(base, dict) and key in base:
                val = base[key]
                return repr(val) if as_repr or isinstance(val, str) else str(val)
            return "None"
        if re.match(r"^[A-Za-z_]\w*$", e):
            if e not in defaults:
                return "0"
            val = defaults[e]
            if val is None:
                return "None"
            if as_repr or isinstance(val, str):
                return repr(val)
            return str(val)
        return "0"

    def repl(match: re.Match) -> str:
        return eval_expr(match.group(1))

    # f-string source uses {{/}} for literal braces; {expr} for interpolations.
    out = re.sub(r"\{([^{}]+)\}", repl, body)
    out = out.replace("{{", "{").replace("}}", "}")
    return out


def compile_snippet(source: str, filename: str = "<remote>") -> ast.AST:
    """Parse/compile snippet with host ast (syntax). Raises SyntaxError."""
    return ast.parse(source, filename=filename, mode="exec")


def capture_built_snippets() -> List[RemoteSnippet]:
    """Build snippets via real agent helpers with a CaptureClient (no live MT5)."""
    from decimal import Decimal

    from plugins.trade.agents import x_mt5linux_agent as m

    client = CaptureClient()
    built: List[RemoteSnippet] = []

    def add(name: str, source: str, *, mutates: bool = False) -> None:
        built.append(
            RemoteSnippet(
                name=name,
                source=source,
                origin=f"builder:{name}",
                mutates=mutates,
                imports=parse_imports(source),
            )
        )

    # 1) snapshot
    add("snapshot", m._snapshot_code(), mutates=False)

    # 2) symbol_info via real builder
    before = len(client.codes)
    m._symbol_info_remote(client, "HYPEUSD")
    assert client.codes[before:], "symbol_info did not emit remote code"
    add("symbol_info_remote", client.codes[-1], mutates=False)

    # 3) symbols/list with query
    before = len(client.codes)

    def run_list(client_, snap, alias, login, server):
        return m._execute_symbols_or_list(
            "PUPRIME33849951",
            "list_instruments",
            {"query": "HYPE", "limit": 20},
        )

    # Call internal run by mocking _run_op path — invoke builder through execute path with patched _with_account
    from unittest import mock

    snap = client.eval_map.get("snap") or CaptureClient().eval("snap")

    def _with(account, fn):
        return fn(client, snap, str(account).upper(), 33849951, "PUPrime-Live2")

    with mock.patch.object(m, "_with_account", side_effect=_with):
        m.execute({"operation": "list_instruments", "account": "PUPRIME33849951", "query": "HYPE", "limit": 20})
        m.execute({"operation": "candles", "account": "PUPRIME33849951", "symbol": "HYPEUSD", "limit": 5})
        m.execute({"operation": "market_price", "account": "PUPRIME33849951", "symbol": "HYPEUSD"})
        m.execute({"operation": "resolve_instrument", "account": "PUPRIME33849951", "symbol": "HYPEUSD"})
        m.execute({"operation": "ticker", "account": "PUPRIME33849951", "symbol": "HYPEUSD"})
        m.execute({"operation": "positions_orders", "account": "PUPRIME33849951"})
        m.execute({"operation": "balance", "account": "PUPRIME33849951"})
        m.execute({"operation": "ping", "account": "PUPRIME33849951"})

    # 4) order helpers — capture only (eval returns reject; no live send)
    m.remote_symbol_trade_meta(client, "HYPEUSD")
    m.order_send_pending(
        client,
        symbol="HYPEUSD",
        side="sell",
        order_type="limit",
        volume=Decimal("0.01"),
        price=Decimal("1"),
        comment="test",
    )
    m.order_modify_pending(client, ticket=1, price=Decimal("1.1"))
    m.order_cancel(client, ticket=1)
    m.position_set_sltp(client, ticket=1, sl=None, tp=Decimal("2"))
    m.position_close(client, ticket=1, volume=None)
    m.reconcile_order(client, 1)
    m.list_account_orders(client, symbol="HYPEUSD")

    # Deduplicate codes while preserving order; classify mutates
    seen: Set[str] = set()
    for idx, code in enumerate(client.codes):
        key = code.strip()
        if key in seen:
            continue
        seen.add(key)
        mut = "order_send" in code
        add(f"captured_{idx}", code, mutates=mut)

    return built


def assert_snippet_safe(snippet: RemoteSnippet) -> None:
    """Raise AssertionError if snippet is unsafe for the Wine remote runtime."""
    bad = snippet.forbidden_imports
    if bad:
        raise AssertionError(
            f"Remote snippet {snippet.name} ({snippet.origin}) imports forbidden/missing modules {bad}. "
            f"Wine RPyC runtime cannot import them (uuid regression). Imports={snippet.imports}"
        )
    # Any import not in allowlist is a hard fail for remote (keeps surface small)
    unknown = [i for i in snippet.imports if i not in REMOTE_ALLOWED_IMPORTS]
    if unknown:
        raise AssertionError(
            f"Remote snippet {snippet.name} ({snippet.origin}) imports non-allowlisted modules {unknown}. "
            f"Allowed={sorted(REMOTE_ALLOWED_IMPORTS)}"
        )
    # Materialise residual f-string placeholders from source templates, then syntax-check.
    body = snippet.source
    if "{" in body and ("!r}" in body or re.search(r"\{[A-Za-z_]", body)):
        body = materialize_template(body)
    try:
        compile_snippet(body, filename=snippet.origin)
    except SyntaxError as exc:
        raise AssertionError(
            f"Remote snippet {snippet.name} syntax error after materialise: {exc}\n---\n{body[:500]}"
        ) from exc


def remote_compile_via_client(client: Any, source: str) -> Dict[str, Any]:
    """Ask the Wine RPyC interpreter to compile(source) without executing order_send."""
    # Avoid sending huge escaping issues: use repr
    payload = (
        "import ast as _ast\n"
        f"_src = {source!r}\n"
        "_remote_compile_result = {'ok': False}\n"
        "try:\n"
        "    _ast.parse(_src, filename='<remote_snip>', mode='exec')\n"
        "    _remote_compile_result = {'ok': True}\n"
        "except Exception as _e:\n"
        "    _remote_compile_result = {'ok': False, 'error': type(_e).__name__, 'msg': str(_e)[:200]}\n"
    )
    client.execute(payload)
    out = client.eval("_remote_compile_result")
    return out if isinstance(out, dict) else {"ok": False, "error": "bad_result"}


def probe_remote_modules(client: Any, names: Sequence[str]) -> Dict[str, bool]:
    code = (
        "import sys as _sys\n"
        f"_names = {list(names)!r}\n"
        "_mod_probe = {}\n"
        "for _n in _names:\n"
        "    try:\n"
        "        __import__(_n)\n"
        "        _mod_probe[_n] = True\n"
        "    except Exception:\n"
        "        _mod_probe[_n] = False\n"
        "try:\n"
        "    _mod_probe['__version__'] = _sys.version\n"
        "except Exception:\n"
        "    pass\n"
    )
    client.execute(code)
    out = client.eval("_mod_probe")
    return out if isinstance(out, dict) else {}
