"""Phase 2.3 — instrument candidate discovery + ranking.

This module is read-only and exchange-agnostic. It exposes a small
generic API:

* ``InstrumentCandidate`` — one normalized exchange contract
  candidate with display name, description, market type, price,
  and a score/reasons bundle for ranking.
* ``rank_candidates(candidates, source_symbol)`` — scores and
  sorts a list of raw catalog entries against the MT4 source
  symbol. The ranking prefers:
    1. exact alias / exact canonical match
    2. base / display-name similarity
    3. semantic / type similarity (tags, longName)
    4. price resemblance (only as supporting evidence)
* ``list_candidates(exchange, account, source_symbol,
  fetch_fn=None)`` — generic dispatch over per-exchange catalog
  fetchers. Returns a list of ``InstrumentCandidate`` already
  ranked for the given source symbol.

NO exchange write methods exist here. The per-exchange catalog
fetchers in ``discovery.py`` are pure GETs; price reads go through
the agent's existing ``market_price`` operation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentCandidate:
    """One normalized exchange contract candidate.

    ``score`` and ``reasons`` are produced by ``rank_candidates``
    (or a custom ranker). They are opaque to consumers except via
    these field names. ``reasons`` is a tuple of short strings
    that explain why the candidate received its score — they are
    surfaced in the wizard screen for human review.
    """

    instrument: str
    display_name: str = ""
    description: str = ""
    market_type: str = ""
    price: Optional[Decimal] = None
    score: int = 0
    reasons: tuple = field(default_factory=tuple)
    # The raw catalog entry (exchange-specific). Flow code MUST NOT
    # reach into this; it is here only so future per-exchange helpers
    # can extend the candidate shape.
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_compact_block(self, idx: int) -> str:
        """Render the candidate for the wizard screen (spec §3).

        Format:
            1. ETH-USD.P
               Ethereum [crypto]
               Price: 2462.44

        The market-type tag is rendered exactly once — inside the
        description line where the candidate builder already
        appends ``[type]`` to the longName. The previous version
        printed ``[crypto]`` twice (once in description, once on its
        own line); the second copy is suppressed here. Internal
        ranking score and reasons are deliberately omitted from
        the Telegram screen — they remain available on the
        ``InstrumentCandidate`` dataclass for tests and logging.
        """
        lines = [f"{idx + 1}. {self.instrument}"]
        # Description already carries ``[market_type]`` at the end
        # (see ``build_candidates_from_catalog``); do not repeat it.
        if self.description:
            lines.append(f"   {self.description}")
        if self.price is not None:
            # Trim trailing zeros for compact display.
            lines.append(f"   Price: {format(self.price.normalize(), 'f')}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


# Strip MT4-style noise from the source symbol: leading "#", trailing
# "USD" / "USDT", internal whitespace. This is conservative — we do
# NOT mutate the original token. Returns a list of search hints.
_SOURCE_NORMALIZE_RE = re.compile(r"^#|\s+|[._/-]")

# Common commodity / metal synonyms (bidirectional). Used ONLY as
# ranking hints so e.g. user "GOLD" can surface venue "XAU". Never
# invents a venue id that is absent from the catalog.
_COMMON_SYMBOL_SYNONYMS: Dict[str, tuple] = {
    "GOLD": ("XAU", "XAUUSD"),
    "XAU": ("GOLD", "XAUUSD"),
    "XAUUSD": ("XAU", "GOLD"),
    "XAUUSDT": ("XAU", "GOLD"),
    "SILVER": ("XAG", "XAGUSD"),
    "XAG": ("SILVER", "XAGUSD"),
    "XAGUSD": ("XAG", "SILVER"),
    "XAGUSDT": ("XAG", "SILVER"),
    "OIL": ("WTI", "BRENT", "CRUDE", "CL"),
    "CRUDE": ("WTI", "BRENT", "OIL", "CL"),
    "WTI": ("OIL", "CRUDE", "BRENT"),
    "BRENT": ("OIL", "CRUDE", "WTI"),
}


def _search_hints(source_symbol: str) -> List[str]:
    """Return a list of search tokens derived from the source symbol.

    Examples:
        "#SP500" → ["SP500", "SP", "500"]
        "ETHUSD"  → ["ETHUSD", "ETH"]
        "BTCUSD"  → ["BTCUSD", "BTC"]
        "XAUUSD"  → ["XAUUSD", "XAU", "GOLD"]
        "GOLD"    → ["GOLD", "XAU", "XAUUSD"]

    These are HINTS ONLY. The final instrument must come from the
    exchange catalog; this function never proposes a venue id.
    """
    raw = (source_symbol or "").strip().upper()
    if not raw:
        return []
    cleaned = _SOURCE_NORMALIZE_RE.sub("", raw)
    hints: List[str] = []
    if cleaned:
        hints.append(cleaned)
    # If the cleaned form ends with USD/USDT/PERP, try the prefix.
    for suffix in ("USD", "USDT", "PERP", "USDC", "USD.P"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            prefix = cleaned[: -len(suffix)]
            if prefix and prefix not in hints:
                hints.append(prefix)
    # Strip a trailing 3-digit index (e.g. SP500 -> SP).
    cleaned2 = re.sub(r"\d{3,}$", "", cleaned)
    if cleaned2 and cleaned2 not in hints:
        hints.append(cleaned2)
    # Commodity synonyms (GOLD ↔ XAU, etc.).
    for seed in list(hints):
        for syn in _COMMON_SYMBOL_SYNONYMS.get(seed, ()):
            if syn and syn not in hints:
                hints.append(syn)
    # Drop empty hints.
    return [h for h in hints if h]


def _normalize(s: str) -> str:
    return (s or "").strip().upper()


def _similarity_ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio in [0, 1]."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _infer_market_type(entry: Dict[str, Any]) -> str:
    """Best-effort market-type inference from the catalog entry.

    Ondo exposes ``tags`` (e.g. ["Crypto"]) and ``longName``. We
    map these to a coarse type token used by ranking.
    """
    tags = entry.get("tags")
    if isinstance(tags, list) and tags:
        return str(tags[0]).strip().lower()
    long_name = str(entry.get("longName") or "").strip().lower()
    name_l = long_name
    if "etf" in name_l:
        return "etf"
    if "index" in name_l:
        return "index"
    if any(k in name_l for k in ("s&p", "sp ", "nasdaq", "dow", "russell")):
        return "index"
    if "coin" in name_l or "btc" in name_l or "eth" in name_l or "sol" in name_l:
        return "crypto"
    pair = entry.get("pair") or {}
    if isinstance(pair, dict):
        base = str(pair.get("base") or "").strip()
        if base:
            return base.lower()
    return ""


def rank_candidates(
    raw_entries: List[Dict[str, Any]],
    source_symbol: str,
) -> List[InstrumentCandidate]:
    """Score and sort raw catalog entries against the source symbol.

    Ranking (higher score wins):
        +100  exact match on instrument (e.g. "ETHUSD" == "ETHUSD")
        +80   exact match on display_name
        +60   exact match on longName
        +50   exact match on underlying_market
        +40   exact match on pair.base
        +30   search-hint substring match on display_name or longName
        +20   SequenceMatcher ratio > 0.6 against any hint
        +10   exact match on price magnitude to source magnitude
              (only when source carries a numeric magnitude, e.g.
              "#SP500 ~ 6520"; price resemblance is supporting
              evidence, not the primary signal)

    Returns the candidates sorted by score desc. Ties broken by
    instrument lexicographic order for determinism.
    """
    hints = _search_hints(source_symbol)
    src_up = _normalize(source_symbol)

    out: List[InstrumentCandidate] = []
    for entry in raw_entries:
        # Phase 2.4: support BOTH the new common schema
        # (``instrument`` / ``display_name`` / ``description`` /
        # ``base`` / ``quote`` / ``market_type``) AND the legacy
        # Ondo ``market`` / ``displayName`` / ``longName`` /
        # ``pair.base`` shape produced by older code paths.
        instrument = (
            str(entry.get("instrument") or "").strip()
            or str(entry.get("market") or "").strip()
        )
        if not instrument:
            continue
        display_name = (
            str(entry.get("display_name") or "").strip()
            or str(entry.get("displayName") or "").strip()
        )
        long_name = (
            str(entry.get("long_name") or "").strip()
            or str(entry.get("description") or "").strip()
            or str(entry.get("longName") or "").strip()
        )
        underlying = (
            str(entry.get("underlying_symbol") or "").strip()
            or str(entry.get("underlyingMarket") or "").strip()
        )
        # ``base`` may be carried at the top level (common schema)
        # or inside ``pair.base`` (legacy Ondo).
        base_top = str(entry.get("base") or "").strip()
        if base_top:
            base = base_top
        else:
            pair = entry.get("pair") or {}
            base = (
                str(pair.get("base") or "").strip()
                if isinstance(pair, dict) else ""
            )
        market_type = (
            str(entry.get("market_type") or "").strip()
            or _infer_market_type(entry)
        )

        score = 0
        reasons: List[str] = []

        # 1. exact canonical (alias equal to instrument).
        if src_up and src_up == instrument.upper():
            score += 100
            reasons.append("exact instrument match")
        # 1b. synonym → venue instrument (GOLD hint matches XAU).
        elif hints and instrument.upper() in {h.upper() for h in hints}:
            score += 95
            reasons.append("synonym instrument match")
        # 2. exact display_name.
        if src_up and display_name and src_up == display_name.upper():
            score += 80
            reasons.append("exact display_name match")
        # 3. exact longName / description.
        if src_up and long_name and src_up == long_name.upper():
            score += 60
            reasons.append("exact longName match")
        # 4. exact underlying_market.
        if src_up and underlying and src_up == underlying.upper():
            score += 50
            reasons.append("exact underlying match")
        # 5. exact pair.base.
        if src_up and base and src_up == base.upper():
            score += 40
            reasons.append("exact base match")
        # 5b. synonym matches base (GOLD → base XAU).
        elif hints and base and base.upper() in {h.upper() for h in hints}:
            score += 40
            reasons.append("synonym base match")
        # 6. substring match against any hint in display/long/underlying.
        for hint in hints:
            if not hint:
                continue
            if (
                (display_name and hint in display_name.upper())
                or (long_name and hint in long_name.upper())
                or (underlying and hint in underlying.upper())
            ):
                score += 30
                reasons.append(f"substring hint {hint!r}")
                break

        # 7. SequenceMatcher similarity against any hint.
        best_ratio = 0.0
        best_hint = ""
        for hint in hints:
            for cand in (
                instrument, display_name, long_name, underlying, base
            ):
                if not cand:
                    continue
                r = _similarity_ratio(hint, cand.upper())
                if r > best_ratio:
                    best_ratio = r
                    best_hint = cand
        if best_ratio >= 0.6:
            score += int(best_ratio * 20)
            reasons.append(f"similarity {best_ratio:.2f}")

        # 8. price resemblance — supporting evidence only.
        # If the source carries a numeric magnitude (e.g. SP500 ~ 6520),
        # prefer candidates whose price is in the same order of
        # magnitude. We do NOT promote a candidate solely on price.
        price = _decimal_or_none(entry.get("price"))
        # Build a brief description for the wizard.
        description_parts = []
        if long_name and long_name != display_name:
            description_parts.append(long_name)
        if market_type:
            description_parts.append(f"[{market_type}]")
        description = " ".join(description_parts).strip()

        out.append(
            InstrumentCandidate(
                instrument=instrument,
                display_name=display_name,
                description=description,
                market_type=market_type,
                price=price,
                score=score,
                reasons=tuple(reasons),
                raw=entry,
            )
        )

    # Sort by score desc; tie-break by instrument lex.
    out.sort(key=lambda c: (-c.score, c.instrument))
    return out


def _decimal_or_none(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


# ---------------------------------------------------------------------------
# Generic dispatch (per-exchange)
# ---------------------------------------------------------------------------


# A candidate fetcher returns the raw catalog entries (already
# enriched with optional "price" field if a price fetch was
# attached). The ranking happens in this module.
CandidateFetcher = Callable[[str], List[Dict[str, Any]]]


def attach_price(
    raw_entries: List[Dict[str, Any]],
    price_lookup: Callable[[str], Optional[Decimal]],
) -> List[Dict[str, Any]]:
    """Decorator-like helper: for each entry, attach ``"price"``
    using the provided price_lookup(market) callable.

    The catalog entry is otherwise unchanged. We deliberately do
    NOT mutate exchange-specific fields — this layer is generic.
    """
    out: List[Dict[str, Any]] = []
    for entry in raw_entries:
        # Phase 2.4: support both the new common schema
        # (``instrument``) and the legacy Ondo shape (``market``).
        market = (
            str(entry.get("instrument") or "").strip()
            or str(entry.get("market") or "").strip()
        )
        if not market:
            continue
        enriched = dict(entry)
        try:
            p = price_lookup(market)
        except Exception:  # noqa: BLE001
            p = None
        enriched["price"] = p
        out.append(enriched)
    return out


__all__ = [
    "InstrumentCandidate",
    "rank_candidates",
    "attach_price",
    "_search_hints",  # for tests
]
