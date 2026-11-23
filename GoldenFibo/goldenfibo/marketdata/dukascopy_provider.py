from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import os
import random
import socket
import threading
import time
from typing import Callable, Mapping

from .dukascopy_cache import VERIFIED_COVERAGE_STATUSES, DukascopyKlineCache
from .dukascopy_format import DukascopyCandle, DukascopyFormatError, parse_bi5_m1
from .historical import HistoricalRequest, check_disk_budget

logger = logging.getLogger(__name__)

# EURUSD and XAUUSD are enabled after native .bi5 fixture validation.
# JPY, silver, and crypto divisors remain fail-closed until independently verified.
VERIFIED_INSTRUMENT_SCALES = {"eurusd": 100_000.0, "xauusd": 1_000.0}


class DukascopyProviderError(RuntimeError):
    pass


class DukascopyInvalidRangeError(DukascopyProviderError):
    pass


class DukascopyNoDataError(DukascopyProviderError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def default_is_closed(instrument: str, day: date) -> bool:
    """Conservative calendar: FX closes weekends; crypto does not."""
    return instrument.lower() != "btcusd" and day.weekday() >= 5


class DukascopyProvider:
    provider_id = "dukascopy"
    _request_lock = threading.Lock()
    _cooldown_lock = threading.Lock()
    _cooldown_until = 0.0

    def __init__(
        self,
        cache: DukascopyKlineCache,
        *,
        fetch_artifact: Callable[[str, date, str], bytes],
        instrument_scales: Mapping[str, float] | None = None,
        instrument_catalog: Callable[[str], float | None] | None = None,
        is_closed: Callable[[str, date], bool] | None = None,
        disk_threshold_bytes: int | None = None,
        journal_reserve_bytes: int | None = None,
        refresh_tail_days: int = 2,
        max_retries: int = 3,
        retry_backoff_seconds: float = 5.0,
        retry_jitter_seconds: float = 0.5,
    ) -> None:
        self.cache = cache
        self.fetch_artifact = fetch_artifact
        configured_catalog = os.environ.get("GOLDENFIBO_DUKASCOPY_INSTRUMENT_SCALES_JSON", "")
        env_scales: dict[str, float] = {}
        if configured_catalog:
            try:
                raw_catalog = json.loads(configured_catalog)
                if not isinstance(raw_catalog, dict):
                    raise ValueError
                env_scales = {str(k).lower(): float(v) for k, v in raw_catalog.items() if float(v) > 0}
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DukascopyProviderError("invalid GOLDENFIBO_DUKASCOPY_INSTRUMENT_SCALES_JSON") from exc
        self.instrument_scales = {
            **VERIFIED_INSTRUMENT_SCALES,
            **env_scales,
            **{k.lower(): float(v) for k, v in (instrument_scales or {}).items() if float(v) > 0},
        }
        self.instrument_catalog = instrument_catalog
        self.is_closed = is_closed or default_is_closed
        configured_threshold = os.environ.get("GOLDENFIBO_DUKASCOPY_MIN_FREE_BYTES", str(1 << 30))
        self.disk_threshold_bytes = int(disk_threshold_bytes if disk_threshold_bytes is not None else configured_threshold)
        configured_journal = os.environ.get("GOLDENFIBO_DUKASCOPY_JOURNAL_RESERVE_BYTES", str(64 << 20))
        self.journal_reserve_bytes = int(journal_reserve_bytes if journal_reserve_bytes is not None else configured_journal)
        self.refresh_tail_days = max(0, refresh_tail_days)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.retry_jitter_seconds = max(0.0, float(retry_jitter_seconds))

    def validate_instrument(self, instrument: str) -> str:
        normalized = instrument.strip().lower().replace("/", "")
        if not normalized or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in normalized):
            raise DukascopyProviderError("invalid Dukascopy instrument")
        if normalized not in self.instrument_scales and self.instrument_catalog is not None:
            scale = self.instrument_catalog(normalized)
            if scale is not None and float(scale) > 0:
                self.instrument_scales[normalized] = float(scale)
        if normalized not in self.instrument_scales:
            raise DukascopyProviderError(
                f"Dukascopy price scale for {instrument} is not independently verified; refusing to decode"
            )
        return normalized

    def _check_disk(self, missing_days: int) -> None:
        # 1,440 M1 rows/day, both side columns, SQLite pages and rollback/WAL
        # headroom. The configured threshold is a reserve, not download size.
        #
        # check_disk_budget compares this estimate against CURRENTLY AVAILABLE
        # free space (shutil.disk_usage(...).free). The bytes the existing
        # cache already occupies are already reflected in that free-space
        # figure, so the estimate must be the INCREMENTAL growth only.
        # Adding the existing cache size again double-counted it and refused
        # downloads that actually fit on disk.
        incremental = max(0, missing_days) * 1440 * 300
        check_disk_budget(
            self.cache.path,
            incremental,
            threshold_bytes=self.disk_threshold_bytes,
            journal_reserve_bytes=self.journal_reserve_bytes,
        )

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        for owner in (exc, getattr(exc, "response", None)):
            for name in ("status_code", "status", "code"):
                value = getattr(owner, name, None)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        pass
        return None

    @classmethod
    def _is_rate_limited(cls, exc: Exception) -> bool:
        return cls._status_code(exc) == 429

    @classmethod
    def _error_category(cls, exc: Exception) -> str:
        status = cls._status_code(exc)
        if status == 429:
            return "http_429"
        if status is not None and 400 <= status < 500:
            return "http_4xx"
        if status is not None and 500 <= status:
            return "http_5xx"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(exc, (ConnectionError, OSError)):
            return "connection"
        return "permanent"

    @classmethod
    def _is_retryable(cls, exc: Exception) -> bool:
        return cls._error_category(exc) in {"timeout", "connection", "http_5xx", "http_429"}

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
        if headers is None:
            return None
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    @classmethod
    def _set_cooldown(cls, delay: float) -> None:
        with cls._cooldown_lock:
            cls._cooldown_until = max(cls._cooldown_until, time.monotonic() + max(0.0, delay))

    @classmethod
    def _wait_for_cooldown(cls) -> None:
        with cls._cooldown_lock:
            deadline = cls._cooldown_until
        delay = deadline - time.monotonic()
        if delay <= 0:
            return
        time.sleep(delay)
        with cls._cooldown_lock:
            if cls._cooldown_until == deadline:
                cls._cooldown_until = 0.0

    def _fetch_once(self, instrument: str, day: date, side: str) -> bytes:
        # A process-wide lock prevents concurrent Dukascopy artifact requests.
        with self._request_lock:
            self._wait_for_cooldown()
            return self.fetch_artifact(instrument, day, side)

    def _fetch_with_retry(self, instrument: str, day: date, side: str) -> bytes:
        last: Exception | None = None
        total_attempts = self.max_retries + 1
        for attempt in range(total_attempts):
            started = time.monotonic()
            try:
                payload = self._fetch_once(instrument, day, side)
                if not payload:
                    raise DukascopyProviderError("provider returned an empty artifact")
                return payload
            except Exception as exc:  # bounded retry for transport/provider failures
                last = exc
                category = self._error_category(exc)
                elapsed = time.monotonic() - started
                logger.warning(
                    "Dukascopy artifact attempt failed instrument=%s date=%s side=%s attempt=%d/%d elapsed=%.3fs category=%s",
                    instrument,
                    day.isoformat(),
                    side.upper(),
                    attempt + 1,
                    total_attempts,
                    elapsed,
                    category,
                )
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                exponential = self.retry_backoff_seconds * (2**attempt)
                retry_after = self._retry_after_seconds(exc) if category == "http_429" else None
                delay = max(exponential, retry_after or 0.0)
                delay += random.uniform(0.0, self.retry_jitter_seconds)
                if category == "http_429":
                    self._set_cooldown(delay)
                elif delay:
                    time.sleep(delay)
        assert last is not None
        raise last

    @staticmethod
    def _validate_side(candles: list[DukascopyCandle], day: date, side: str) -> None:
        if not candles:
            raise DukascopyProviderError(f"{side} artifact for {day} contained no candles")
        previous = None
        for candle in candles:
            if previous is not None and candle.open_time_ms - previous > 60_000:
                raise DukascopyProviderError(f"{side} artifact for {day} has an unverified intra-day gap")
            previous = candle.open_time_ms

    def fetch_range(self, request: HistoricalRequest, *, on_progress: Callable[[dict], None] | None = None) -> list[list]:
        instrument = self.validate_instrument(request.instrument)
        requested_side = (request.price_side or "bid").lower()
        if requested_side not in {"bid", "ask"}:
            raise DukascopyProviderError("Dukascopy price side must be bid or ask")
        if request.end_ms <= request.start_ms:
            raise DukascopyInvalidRangeError("Dukascopy requested interval is invalid or empty")
        start_day = datetime.fromtimestamp(request.start_ms / 1000, tz=timezone.utc).date()
        end_day = datetime.fromtimestamp((request.end_ms - 1) / 1000, tz=timezone.utc).date()
        requested_days: list[date] = []
        cursor = start_day
        while cursor <= end_day:
            requested_days.append(cursor)
            cursor += timedelta(days=1)
        now_day = datetime.now(timezone.utc).date()
        refresh = {now_day - timedelta(days=n) for n in range(self.refresh_tail_days)}
        missing = self.cache.missing_days(instrument, start_day, end_day, refresh_days=refresh, price_side=requested_side)
        self._check_disk(len(missing))
        coverage = self.cache.coverage(instrument, start_day, end_day)
        coverage_by_day = {entry.day: entry for entry in coverage}
        complete_days = sum(
            bool(
                (entry := coverage_by_day.get(day))
                and self.cache.side_is_verified(entry, requested_side)
            )
            for day in requested_days
        )
        closure_days = sum(
            bool(
                (entry := coverage_by_day.get(day))
                and self.cache.side_status(entry, requested_side) == "verified_provider_empty"
            )
            for day in requested_days
        )
        failed_days = sum(
            bool(
                (entry := coverage_by_day.get(day))
                and self.cache.side_status(entry, requested_side) == "failed"
            )
            for day in requested_days
        )
        unrecorded_days = sum(day not in coverage_by_day for day in requested_days)
        cached_candles = len(self.cache.rows(instrument, request.start_ms, request.end_ms, price_side=requested_side))
        logger.warning(
            "Dukascopy cache coverage instrument=%s side=%s requested_start=%s requested_end_exclusive=%s requested_days=%d complete_days=%d closure_days=%d failed_days=%d unrecorded_days=%d missing_days=%d cached_candles=%d",
            instrument,
            requested_side.upper(),
            start_day.isoformat(),
            datetime.fromtimestamp(request.end_ms / 1000, tz=timezone.utc).isoformat(),
            len(requested_days),
            complete_days,
            closure_days,
            failed_days,
            unrecorded_days,
            len(missing),
            cached_candles,
        )
        if on_progress:
            on_progress({"stage": "loading_cache", "provider": self.provider_id, "requested_candles": max(0, (request.end_ms-request.start_ms)//60_000), "cached_candles": cached_candles, "missing_artifacts": len(missing), "stats": {"path": str(self.cache.path)}})
        for index, day in enumerate(missing, 1):
            if self.is_closed(instrument, day):
                # A market-closure reclassification must not downgrade a side
                # that already holds a genuine data verification. Report the
                # closure for the sides that have none; mark_day resolves the
                # rest from the row it reads in its own transaction.
                prior = self.cache.coverage(instrument, day, day)
                prior_entry = prior[0] if prior else None
                prior_bid_status = self.cache.side_status(prior_entry, "bid")
                prior_ask_status = self.cache.side_status(prior_entry, "ask")
                self.cache.mark_day(
                    instrument,
                    day,
                    "verified_provider_empty",
                    note="provider market closure",
                    bid_complete=True,
                    ask_complete=True,
                    bid_status="" if prior_bid_status in VERIFIED_COVERAGE_STATUSES else "verified_provider_empty",
                    ask_status="" if prior_ask_status in VERIFIED_COVERAGE_STATUSES else "verified_provider_empty",
                )
                continue
            if on_progress:
                on_progress({"stage": "downloading_gaps", "provider": self.provider_id, "artifact_done": index - 1, "artifact_total": len(missing), "detail": day.isoformat()})
            prior = self.cache.coverage(instrument, day, day)
            prior_entry = prior[0] if prior else None
            prior_bid_status = self.cache.side_status(prior_entry, "bid")
            prior_ask_status = self.cache.side_status(prior_entry, "ask")
            try:
                self._check_disk(1)
                payload = self._fetch_with_retry(instrument, day, requested_side)
                candles = parse_bi5_m1(payload, day, price_scale=self.instrument_scales[instrument], price_side=requested_side)
                self._validate_side(candles, day, requested_side)
                self.cache.upsert_side(instrument, candles)
                # The acquired side is verified; the peer side keeps whatever
                # verification it already had. Only the acquired side is
                # reported explicitly — the peer side is resolved inside
                # mark_day from the row it reads in its own transaction, so a
                # stale value cannot overwrite a newer verification.
                self.cache.mark_day(
                    instrument,
                    day,
                    "verified_data",
                    bid_complete=requested_side == "bid",
                    ask_complete=requested_side == "ask",
                    bid_status="verified_data" if requested_side == "bid" else "",
                    ask_status="verified_data" if requested_side == "ask" else "",
                )
            except Exception as exc:
                # Only the side that failed is marked failed. The peer side
                # keeps its own verification.
                self.cache.mark_day(
                    instrument,
                    day,
                    "failed",
                    note=str(exc),
                    bid_status=prior_bid_status if prior_bid_status in VERIFIED_COVERAGE_STATUSES else "failed",
                    ask_status=prior_ask_status if prior_ask_status in VERIFIED_COVERAGE_STATUSES else "failed",
                )
                raise DukascopyProviderError(f"Dukascopy {instrument} {day} failed: {exc}") from exc
        coverage = self.cache.coverage(instrument, start_day, end_day)
        coverage_by_day = {entry.day: entry for entry in coverage}
        rows = self.cache.rows(instrument, request.start_ms, request.end_ms, price_side=requested_side)
        logger.warning(
            "Dukascopy cache result instrument=%s side=%s verified_candles=%d",
            instrument,
            requested_side.upper(),
            len(rows),
        )
        if on_progress:
            on_progress({"stage": "validating", "provider": self.provider_id, "final_candles": len(rows), "stats": {"path": str(self.cache.path)}})
        if not rows:
            closure_only = bool(requested_days) and all(
                (entry := coverage_by_day.get(day)) is not None
                and self.cache.side_status(entry, requested_side) == "verified_provider_empty"
                for day in requested_days
            )
            if closure_only:
                raise DukascopyNoDataError(
                    "Dukascopy requested interval contains only verified market-closure days",
                    reason="verified_closure_only",
                )
            raise DukascopyNoDataError(
                "Dukascopy requested interval has no verified candles",
                reason="unavailable_coverage",
            )
        previous = None
        for row in rows:
            current = int(row[0])
            if previous is not None and current - previous > 60_000:
                previous_day = datetime.fromtimestamp(previous / 1000, tz=timezone.utc).date()
                current_day = datetime.fromtimestamp(current / 1000, tz=timezone.utc).date()
                cursor = previous_day + timedelta(days=1)
                intervening_days: list[date] = []
                while cursor < current_day:
                    intervening_days.append(cursor)
                    cursor += timedelta(days=1)
                verified_closure_gap = bool(intervening_days) and all(
                    (entry := coverage_by_day.get(day)) is not None
                    and self.cache.side_status(entry, requested_side) == "verified_provider_empty"
                    for day in intervening_days
                )
                if not verified_closure_gap:
                    raise DukascopyProviderError(
                        "requested-side coverage contains an unverified intra-day gap"
                    )
            previous = current
        if on_progress:
            on_progress({"stage": "cache_ready", "provider": self.provider_id, "final_candles": len(rows), "stats": {"path": str(self.cache.path)}})
        return rows
