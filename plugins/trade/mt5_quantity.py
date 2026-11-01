"""MT5Linux quantity display and conversion (underlying units ↔ broker lots).

Pure Decimal math. Does not talk to the terminal. Callers supply live
``trade_contract_size`` / ``volume_min`` / ``volume_max`` / ``volume_step``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Mapping, Optional, Tuple


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            d = value
        elif isinstance(value, float):
            d = Decimal(str(value))
        else:
            text = str(value).strip().replace(",", "")
            if not text:
                return None
            d = Decimal(text)
        if not d.is_finite():
            return None
        return d
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fmt(d: Decimal) -> str:
    text = format(d.normalize(), "f") if d != 0 else "0"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# Quote suffixes stripped when deriving a crypto base ticker from the symbol name.
_QUOTE_SUFFIXES = (
    "USDT",
    "USDC",
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "AUD",
    "CAD",
    "CHF",
    "NZD",
)


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    currency_base: str = ""
    currency_profit: str = ""
    description: str = ""
    path: str = ""
    trade_calc_mode: Optional[int] = None
    digits: Optional[int] = None

    @classmethod
    def from_mapping(cls, symbol: str, info: Mapping[str, Any]) -> Optional["ContractSpec"]:
        contract = _dec(info.get("trade_contract_size") if "trade_contract_size" in info else info.get("contract_size"))
        vmin = _dec(info.get("volume_min") if "volume_min" in info else info.get("minimum_size"))
        vmax = _dec(info.get("volume_max") if "volume_max" in info else info.get("maximum_size"))
        vstep = _dec(info.get("volume_step") if "volume_step" in info else info.get("size_increment"))
        if contract is None or contract <= 0:
            return None
        if vmin is None or vmin <= 0:
            vmin = Decimal("0.01")
        if vstep is None or vstep <= 0:
            vstep = vmin
        if vmax is None or vmax <= 0:
            vmax = Decimal("100")
        tcm = info.get("trade_calc_mode")
        try:
            tcm_i = int(tcm) if tcm is not None else None
        except (TypeError, ValueError):
            tcm_i = None
        dig = info.get("digits")
        try:
            dig_i = int(dig) if dig is not None else None
        except (TypeError, ValueError):
            dig_i = None
        return cls(
            symbol=str(info.get("name") or symbol or "").strip() or str(symbol),
            contract_size=contract,
            volume_min=vmin,
            volume_max=vmax,
            volume_step=vstep,
            currency_base=str(info.get("currency_base") or "").strip(),
            currency_profit=str(info.get("currency_profit") or "").strip(),
            description=str(info.get("description") or "").strip(),
            path=str(info.get("path") or "").strip(),
            trade_calc_mode=tcm_i,
            digits=dig_i,
        )


def _symbol_base_guess(symbol: str) -> str:
    up = str(symbol or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")
    if not up:
        return ""
    if up.startswith("XAU"):
        return "XAU"
    if up.startswith("XAG"):
        return "XAG"
    for suf in _QUOTE_SUFFIXES:
        if up.endswith(suf) and len(up) > len(suf):
            return up[: -len(suf)]
    return up


def infer_unit_label(spec: ContractSpec) -> Tuple[str, str]:
    """Return (unit_label, mode) where mode is 'underlying' or 'lots'.

    Crypto CFD paths / familiar crypto tickers → base asset tokens.
    Forex (calc mode 0 or known FX pairs) → base currency units.
    Metals XAU/XAG → troy oz label.
    Otherwise → broker lots (do not invent a token name).
    """
    path_u = (spec.path or "").upper().replace("\\", "/")
    sym_u = (spec.symbol or "").upper()
    base_guess = _symbol_base_guess(spec.symbol)
    desc_u = (spec.description or "").upper()

    is_crypto = (
        "CRYPTO" in path_u
        or base_guess
        in {
            "BTC",
            "ETH",
            "SOL",
            "HYPE",
            "ZEC",
            "XRP",
            "ADA",
            "DOGE",
            "LTC",
            "BNB",
            "DOT",
            "AVAX",
            "LINK",
            "MATIC",
            "UNI",
            "ATOM",
            "NEAR",
            "APT",
            "ARB",
            "OP",
            "SUI",
            "TIA",
            "INJ",
            "FIL",
            "AAVE",
            "MKR",
            "CRV",
            "PEPE",
            "WIF",
            "BONK",
            "SHIB",
            "TRX",
            "TON",
            "VET",
            "THT",
            "BCH",
            "ETC",
            "XLM",
            "ALGO",
            "ICP",
            "FTM",
            "SAND",
            "MANA",
            "AXS",
            "GALA",
            "IMX",
            "RENDER",
            "FET",
            "TAO",
        }
        or any(tok in desc_u for tok in ("BITCOIN", "ETHEREUM", "SOLANA", "CRYPTO"))
    )
    if is_crypto and base_guess:
        return base_guess, "underlying"

    if base_guess in {"XAU", "XAG"} or "XAU" in sym_u or "XAG" in sym_u or "GOLD" in desc_u or "SILVER" in desc_u:
        # Contract size is ounces (or grams on some brokers) — still a real underlying unit.
        metal = "XAU" if ("XAU" in sym_u or "GOLD" in desc_u) else "XAG"
        return f"{metal} oz", "underlying"

    # Forex: calc mode 0 (Forex) or EURUSD-style with currency_base matching left side.
    if spec.trade_calc_mode == 0 or (
        spec.currency_base
        and base_guess
        and spec.currency_base.upper() == base_guess
        and len(base_guess) == 3
        and base_guess.isalpha()
    ):
        unit = (spec.currency_base or base_guess).upper()
        if unit:
            return unit, "underlying"

    return "lot", "lots"


@dataclass(frozen=True)
class QuantityPlan:
    mode: str  # underlying | lots
    unit_label: str
    contract_size: Decimal
    volume_min_lots: Decimal
    volume_max_lots: Decimal
    volume_step_lots: Decimal
    min_underlying: Optional[Decimal]
    step_underlying: Optional[Decimal]
    max_underlying: Optional[Decimal]

    @property
    def prompt_label(self) -> str:
        if self.mode == "underlying":
            # "SOL" → "SOL tokens"; "EUR" stays "EUR"; "XAU oz" stays.
            unit = self.unit_label
            if unit.endswith(" oz") or unit.upper() in {"EUR", "GBP", "AUD", "CAD", "CHF", "NZD", "JPY", "USD"}:
                return f"Enter Volume ({unit}):"
            if len(unit) <= 6 and unit.isalpha():
                return f"Enter Volume ({unit} tokens):"
            return f"Enter Volume ({unit}):"
        return "Enter Volume (MT5 lots):"

    def help_lines(self) -> list[str]:
        lines: list[str] = []
        if self.mode == "underlying" and self.min_underlying is not None and self.step_underlying is not None:
            lines.append(
                f"Minimum: {_fmt(self.min_underlying)} {self.unit_label} = {_fmt(self.volume_min_lots)} MT5 lots"
            )
            lines.append(
                f"Increment: {_fmt(self.step_underlying)} {self.unit_label} = {_fmt(self.volume_step_lots)} MT5 lots"
            )
            lines.append(
                f"Contract size: {_fmt(self.contract_size)} {self.unit_label} per lot"
            )
            if self.max_underlying is not None:
                lines.append(
                    f"Maximum: {_fmt(self.max_underlying)} {self.unit_label} = {_fmt(self.volume_max_lots)} MT5 lots"
                )
        else:
            lines.append(f"Minimum: {_fmt(self.volume_min_lots)} lots")
            lines.append(f"Increment: {_fmt(self.volume_step_lots)} lots")
            lines.append(f"Contract size: {_fmt(self.contract_size)} (broker units per lot)")
            lines.append(f"Maximum: {_fmt(self.volume_max_lots)} lots")
        return lines


def build_quantity_plan(spec: ContractSpec) -> QuantityPlan:
    unit, mode = infer_unit_label(spec)
    if mode == "underlying":
        return QuantityPlan(
            mode=mode,
            unit_label=unit,
            contract_size=spec.contract_size,
            volume_min_lots=spec.volume_min,
            volume_max_lots=spec.volume_max,
            volume_step_lots=spec.volume_step,
            min_underlying=spec.volume_min * spec.contract_size,
            step_underlying=spec.volume_step * spec.contract_size,
            max_underlying=spec.volume_max * spec.contract_size,
        )
    return QuantityPlan(
        mode="lots",
        unit_label="lot",
        contract_size=spec.contract_size,
        volume_min_lots=spec.volume_min,
        volume_max_lots=spec.volume_max,
        volume_step_lots=spec.volume_step,
        min_underlying=None,
        step_underlying=None,
        max_underlying=None,
    )


def lots_to_underlying(lots: Decimal, contract_size: Decimal) -> Decimal:
    return lots * contract_size


def underlying_to_lots(underlying: Decimal, contract_size: Decimal) -> Decimal:
    if contract_size <= 0:
        raise ValueError("contract_size must be positive")
    return underlying / contract_size


def _quantize_to_step(lots: Decimal, step: Decimal, *, rounding=ROUND_HALF_UP) -> Decimal:
    if step <= 0:
        return lots
    units = (lots / step).to_integral_value(rounding=rounding)
    return units * step


@dataclass(frozen=True)
class ConversionResult:
    ok: bool
    lots: Optional[Decimal]
    underlying: Optional[Decimal]
    mode: str
    unit_label: str
    message: str = ""
    nearest_lots: Optional[Decimal] = None
    nearest_underlying: Optional[Decimal] = None


def convert_user_quantity(user_value: Any, plan: QuantityPlan) -> ConversionResult:
    """Convert a user-entered quantity into broker lots.

    Never silently changes exposure: if the exact lot size is off-step or out of
    range, ok=False and nearest valid quantities are suggested.
    """
    raw = _dec(user_value)
    if raw is None or raw <= 0:
        return ConversionResult(
            ok=False,
            lots=None,
            underlying=None,
            mode=plan.mode,
            unit_label=plan.unit_label,
            message="Please enter a positive numeric value.",
        )

    if plan.mode == "underlying":
        underlying = raw
        lots = underlying_to_lots(underlying, plan.contract_size)
        display_underlying = underlying
    else:
        lots = raw
        underlying = lots_to_underlying(lots, plan.contract_size) if plan.contract_size > 0 else None
        display_underlying = underlying

    # Exact step check (no silent rounding).
    step = plan.volume_step_lots
    if step > 0:
        ratio = lots / step
        if ratio != ratio.to_integral_value(rounding=ROUND_DOWN):
            down = _quantize_to_step(lots, step, rounding=ROUND_DOWN)
            up = down + step
            # clamp suggestions into [min,max]
            cands = []
            for cand in (down, up):
                if cand < plan.volume_min_lots or cand > plan.volume_max_lots:
                    continue
                if cand <= 0:
                    continue
                cands.append(cand)
            msg = (
                f"{_fmt(raw)} {plan.unit_label} is not a valid broker increment "
                f"(step {_fmt(plan.step_underlying or step)} {plan.unit_label})."
            )
            if cands:
                parts = []
                for c in cands:
                    u = lots_to_underlying(c, plan.contract_size) if plan.mode == "underlying" else c
                    if plan.mode == "underlying":
                        parts.append(f"{_fmt(u)} {plan.unit_label} ({_fmt(c)} lots)")
                    else:
                        parts.append(f"{_fmt(c)} lots")
                msg += " Closest valid: " + " or ".join(parts) + "."
            nearest = cands[0] if cands else None
            return ConversionResult(
                ok=False,
                lots=None,
                underlying=display_underlying,
                mode=plan.mode,
                unit_label=plan.unit_label,
                message=msg,
                nearest_lots=nearest,
                nearest_underlying=(
                    lots_to_underlying(nearest, plan.contract_size) if nearest is not None else None
                ),
            )

    if lots < plan.volume_min_lots:
        return ConversionResult(
            ok=False,
            lots=None,
            underlying=display_underlying,
            mode=plan.mode,
            unit_label=plan.unit_label,
            message=(
                f"Below minimum: need at least {_fmt(plan.min_underlying or plan.volume_min_lots)} "
                f"{plan.unit_label if plan.mode == 'underlying' else 'lots'} "
                f"({_fmt(plan.volume_min_lots)} MT5 lots)."
            ),
            nearest_lots=plan.volume_min_lots,
            nearest_underlying=plan.min_underlying,
        )
    if lots > plan.volume_max_lots:
        return ConversionResult(
            ok=False,
            lots=None,
            underlying=display_underlying,
            mode=plan.mode,
            unit_label=plan.unit_label,
            message=(
                f"Above maximum: at most {_fmt(plan.max_underlying or plan.volume_max_lots)} "
                f"{plan.unit_label if plan.mode == 'underlying' else 'lots'} "
                f"({_fmt(plan.volume_max_lots)} MT5 lots)."
            ),
            nearest_lots=plan.volume_max_lots,
            nearest_underlying=plan.max_underlying,
        )

    return ConversionResult(
        ok=True,
        lots=lots,
        underlying=display_underlying if plan.mode == "underlying" else underlying,
        mode=plan.mode,
        unit_label=plan.unit_label,
        message="",
    )


def format_volume_confirm(result: ConversionResult, plan: QuantityPlan) -> list[str]:
    if not result.ok or result.lots is None:
        return []
    lines = []
    if plan.mode == "underlying" and result.underlying is not None:
        lines.append(f"Volume: {_fmt(result.underlying)} {plan.unit_label}")
        lines.append(f"MT5 volume: {_fmt(result.lots)} lot" + ("s" if result.lots != 1 else ""))
    else:
        lines.append(f"Volume: {_fmt(result.lots)} MT5 lots")
        if result.underlying is not None and plan.contract_size > 0:
            lines.append(f"Contract exposure: {_fmt(result.underlying)} (×{_fmt(plan.contract_size)} per lot)")
    return lines
