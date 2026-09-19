from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from goldenfibo.engine.config import EngineConfig, Side
from goldenfibo.engine.engine import GoldenFiboEngine
from goldenfibo.engine.events import MarketEvent, MarketEventKind
from goldenfibo.engine.levels import ladder_step
from goldenfibo.marketdata.aggtrade_cache import AggTradeCache
from goldenfibo.metrics.backfill_scheduler import DesiredWindow, LatestWinsScheduler
from goldenfibo.metrics.trade_vap import AggTrade, StreamMetricsAccumulator, trade_metrics_for_windows
from goldenfibo.session.controller import SessionController
from goldenfibo.session.types import SessionMode, SessionPhase


def _agg_row(aid, ts, px, qty, f, l):
    return {"a": aid, "p": str(px), "q": str(qty), "f": f, "l": l, "T": ts}


@dataclass
class _StubScheduler:
    requests: list
    invalidations: list
    reevals: list

    def __init__(self):
        self.requests = []
        self.invalidations = []
        self.reevals = []

    async def request_window(self, window, *, kind="full"):
        self.requests.append((kind, window))

    async def invalidate_session(self, new_session_id: str):
        self.invalidations.append(new_session_id)

    async def reevaluate_after_worker(self, *, latest_window_provider, installed_window_provider):
        self.reevals.append((latest_window_provider(), installed_window_provider()))


class _InstallRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _make_controller(tmp_path, *, market="futures", symbol="HYPEUSDT", run_id="run-1"):
    ctrl = SessionController()
    ctrl.aggtrade_cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    ctrl._agg_archive_fetch = ctrl.aggtrade_cache.fetch_archive_range
    ctrl.mode = SessionMode.LIVE
    ctrl.phase = SessionPhase.LIVE
    ctrl.market = market
    ctrl.symbol = symbol
    ctrl._aggtrade_metrics_enabled = True
    ctrl._p0_seeded = True
    ctrl.run_id = run_id
    ctrl.engine = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001"), symbol=symbol))
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100")))
    return ctrl


def _force_engine_window(ctrl: SessionController, ladder_ts: int, step_ts: int) -> None:
    ctrl.engine.state.legs = [SimpleNamespace(ts_ms=ladder_ts), SimpleNamespace(ts_ms=step_ts)]


def _window(market="futures", symbol="HYPEUSDT", ladder=1_000_000, step=1_000_000, endpoint=1_100_000, session="run-1"):
    return DesiredWindow(market=market, symbol=symbol, ladder_start_ms=ladder, step_start_ms=step, endpoint_ts_ms=endpoint, session_id=session)


async def _await_task(task):
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)


def test_request_selection_full_step_none(tmp_path):
    ctrl = _make_controller(tmp_path)
    reqs = []
    ctrl._request_backfill_now = lambda *, kind: reqs.append(kind)

    _force_engine_window(ctrl, 1_000_000, 1_000_000)
    ctrl.trade_store.invalidate_streamed()
    ctrl._sync_trade_windows_from_engine(schedule_backfill=True)
    assert reqs == ["full"]

    reqs.clear()
    ctrl.trade_store._streamed_identity = ("futures", "HYPEUSDT", 1_000_000, 1_000_000, 1_100_000)
    ctrl._sync_trade_windows_from_engine(schedule_backfill=True)
    assert reqs == []


def test_step_only_priority_and_supersession(tmp_path):
    ctrl = _make_controller(tmp_path)
    reqs = []
    ctrl._request_backfill_now = lambda *, kind: reqs.append(kind)
    ctrl.trade_store._streamed_identity = ("futures", "HYPEUSDT", 1_000_000, 1_000_000, 1_100_000)

    _force_engine_window(ctrl, 1_000_000, 1_200_000)
    ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
    assert reqs == ["step"]

    reqs.clear()
    _force_engine_window(ctrl, 1_050_000, 1_050_000)
    ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
    assert reqs == ["full"]


def test_start_run_invalidates_old_session(tmp_path, monkeypatch):
    ctrl = _make_controller(tmp_path, run_id="old-session")
    stub = _StubScheduler()
    ctrl._backfill_scheduler = stub

    async def noop_live():
        return None

    monkeypatch.setattr(ctrl, "_run_live_now", noop_live)
    asyncio.run(ctrl.start_run(mode=SessionMode.LIVE, symbol="BTCUSDT", market="spot"))
    assert stub.invalidations == ["old-session"]
    assert ctrl._stream_gen_token is None


def test_rapid_p0_churn_latest_wins_scheduler():
    started = []
    released = asyncio.Event()

    async def worker(request):
        started.append(request)
        await released.wait()

    sched = LatestWinsScheduler(worker)

    async def run():
        await sched.request_window(_window(ladder=1, step=1, endpoint=10), kind="full")
        for i in range(2, 6):
            await sched.request_window(_window(ladder=i, step=i, endpoint=10), kind="full")
        assert sched.pending().ladder_start_ms == 5
        released.set()
        await _await_task(sched._running_task)
        await sched.reevaluate_after_worker(latest_window_provider=lambda: _window(ladder=5, step=5, endpoint=10), installed_window_provider=lambda: _window(ladder=5, step=5, endpoint=10))

    asyncio.run(run())
    assert started[0].ladder_start_ms == 1
    assert sched.stats.max_concurrent_workers_observed == 1


def test_churn_then_quiet_eventual_convergence(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl.trade_store._streamed_identity = None
    ctrl._current_engine_window = lambda: _window(ladder=1_000_000, step=1_000_000, endpoint=1_100_000)
    ctrl._installed_streamed_window = lambda: None
    seen = []

    async def fake_worker(request):
        seen.append(request)
        ctrl.trade_store._streamed_identity = (request.market, request.symbol, request.ladder_start_ms, request.step_start_ms, request.endpoint_ts_ms)

    ctrl._stream_backfill_for_request = fake_worker
    ctrl._backfill_scheduler = _StubScheduler()
    asyncio.run(ctrl._stream_backfill_for_request(_window()))
    assert ctrl.trade_store.streamed_identity() is not None


def test_rapid_step_churn(tmp_path):
    ctrl = _make_controller(tmp_path)
    reqs = []
    ctrl._request_backfill_now = lambda *, kind: reqs.append(kind)
    ctrl.trade_store._streamed_identity = ("futures", "HYPEUSDT", 1_000_000, 1_000_000, 1_100_000)
    for step in (1_100_000, 1_200_000, 1_300_000, 1_400_000):
        _force_engine_window(ctrl, 1_000_000, step)
        ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
    assert reqs == ["step", "step", "step", "step"]


def test_step_worker_superseded_by_new_p0(tmp_path):
    ctrl = _make_controller(tmp_path)
    reqs = []
    ctrl._request_backfill_now = lambda *, kind: reqs.append(kind)
    ctrl.trade_store._streamed_identity = ("futures", "HYPEUSDT", 1_000_000, 1_000_000, 1_100_000)
    _force_engine_window(ctrl, 1_000_000, 1_100_000)
    ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
    _force_engine_window(ctrl, 1_050_000, 1_050_000)
    ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
    assert reqs == ["step", "full"]


def test_100_window_worker_storm_prevention():
    started = 0
    completed = 0
    released = asyncio.Event()

    async def worker(request):
        nonlocal started, completed
        started += 1
        await released.wait()
        completed += 1

    sched = LatestWinsScheduler(worker)

    async def run():
        await sched.request_window(_window(ladder=1, step=1), kind="full")
        for i in range(2, 101):
            await sched.request_window(_window(ladder=i, step=i), kind="full")
        assert sched.pending().ladder_start_ms == 100
        released.set()
        await _await_task(sched._running_task)
        await sched.reevaluate_after_worker(latest_window_provider=lambda: _window(ladder=100, step=100), installed_window_provider=lambda: _window(ladder=100, step=100))

    asyncio.run(run())
    assert started < 20
    assert completed >= 1
    assert sched.stats.max_concurrent_workers_observed == 1


def test_exact_current_window_iterator_ranges(tmp_path):
    ctrl = _make_controller(tmp_path)
    cache = ctrl.aggtrade_cache
    rows = [_agg_row(1, 1_000_000, 100.0, 1.0, 1, 1), _agg_row(2, 1_000_010, 101.0, 1.0, 2, 2)]
    cache.insert_trades("futures", "HYPEUSDT", rows, source="rest")
    seen = []
    orig = cache.iter_deduped_trades_range

    def wrapped(market, symbol, start_ms, end_ms, *args, **kwargs):
        seen.append((market, symbol, start_ms, end_ms))
        return orig(market, symbol, start_ms, end_ms, *args, **kwargs)

    cache.iter_deduped_trades_range = wrapped
    ctrl._agg_trades_fetch = lambda *a, **k: []
    ctrl._agg_archive_fetch = lambda *a, **k: []
    asyncio.run(ctrl._stream_backfill_for_request(_window(ladder=1_000_000, step=1_000_000, endpoint=1_100_000)))
    assert seen[0][2] == 1_000_000
    assert seen[0][3] == 1_100_000


def test_live_trade_race_canonical_six_metric_parity(tmp_path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [
            _agg_row(1, 1_000_000, 100.0, 1.0, 1, 1),
            _agg_row(2, 1_000_010, 101.0, 2.0, 2, 2),
        ],
        source="rest",
    )
    store = SessionController().trade_store
    store.market = "futures"
    store.symbol = "HYPEUSDT"
    store.set_windows(ladder_start_ms=1_000_000, step_start_ms=1_000_000)
    acc = StreamMetricsAccumulator(ladder_start_ms=1_000_000, step_start_ms=1_000_000, bin_size=0.01)
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 1_000_000, 1_100_000):
        acc.add(trade)
    store.apply_streaming_backfill(acc, market="futures", symbol="HYPEUSDT", ladder_start_ms=1_000_000, step_start_ms=1_000_000, endpoint_ts_ms=1_100_000)
    md = store.compute()
    canonical = trade_metrics_for_windows(list(cache.iter_deduped_trades_range("futures", "HYPEUSDT", 1_000_000, 1_100_000)), ladder_start_ts_ms=1_000_000, step_start_ts_ms=1_000_000, bin_size=0.01)
    assert md.ladder_vwap == canonical.ladder_vwap
    assert md.step_vwap == canonical.step_vwap
    assert md.ladder_poc == canonical.ladder_poc
    assert md.step_poc == canonical.step_poc
    assert md.ladder_trade_count == canonical.ladder_trade_count
    assert md.step_trade_count == canonical.step_trade_count


def test_session_change_invalidates_old_work():
    events = []
    released = asyncio.Event()

    async def worker(request):
        events.append(request.session_id)
        await released.wait()

    sched = LatestWinsScheduler(worker)

    async def run():
        await sched.request_window(_window(session="old"), kind="full")
        await asyncio.sleep(0)
        await sched.invalidate_session("new")
        released.set()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert events == ["old"]
    assert sched.stats.session_invalidations == 1
    assert sched.has_pending() is False
    assert sched.is_running() is False


def test_physical_expensive_work_concurrency_never_exceeds_one():
    active = 0
    max_active = 0
    started = []
    release_a = asyncio.Event()
    release_d = asyncio.Event()
    started_a = asyncio.Event()
    started_d = asyncio.Event()

    async def worker(request):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        started.append(request.endpoint_ts_ms)
        if len(started) == 1:
            started_a.set()
            await release_a.wait()
        else:
            started_d.set()
            await release_d.wait()
        active -= 1

    sched = LatestWinsScheduler(worker)

    async def run():
        await sched.request_window(_window(ladder=1, step=1, endpoint=10), kind="full")
        await started_a.wait()
        for ep in (11, 12, 13):
            await sched.request_window(_window(ladder=ep, step=ep, endpoint=ep), kind="full")
        assert sched.pending() is not None
        assert sched.pending().endpoint_ts_ms == 13
        assert active == 1
        assert started == [10]
        assert not started_d.is_set()
        release_a.set()
        await started_d.wait()
        release_d.set()
        await sched.wait_idle()

    asyncio.run(run())
    assert max_active == 1
    assert started == [10, 13]
    assert sched.is_running() is False
    assert sched.has_pending() is False


def test_session_invalidation_blocks_overlapping_physical_workers():
    active = 0
    max_active = 0
    started = []
    release_a = asyncio.Event()
    release_d = asyncio.Event()
    started_a = asyncio.Event()
    started_d = asyncio.Event()

    async def worker(request):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        started.append(request.session_id)
        if len(started) == 1:
            started_a.set()
            await release_a.wait()
        else:
            started_d.set()
            await release_d.wait()
        active -= 1

    sched = LatestWinsScheduler(worker)

    async def run():
        await sched.request_window(_window(session="old", endpoint=10), kind="full")
        await started_a.wait()
        await sched.invalidate_session("new")
        await sched.request_window(_window(session="new", endpoint=13), kind="full")
        assert active == 1
        assert started == ["old"]
        assert not started_d.is_set()
        release_a.set()
        await started_d.wait()
        release_d.set()
        await sched.wait_idle()

    asyncio.run(run())
    assert max_active == 1
    assert started == ["old", "new"]
    assert sched.is_running() is False
    assert sched.has_pending() is False


def test_completion_reevaluation_regression(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl.trade_store._streamed_identity = None
    current = {"win": _window(ladder=1_000_000, step=1_000_000, endpoint=1_100_000)}
    installed = {"win": None}
    ctrl._current_engine_window = lambda: current["win"]
    ctrl._installed_streamed_window = lambda: installed["win"]
    ctrl._backfill_scheduler = _StubScheduler()

    async def fake_stream(request):
        installed["win"] = request

    ctrl._stream_backfill_for_request = fake_stream
    asyncio.run(ctrl._stream_backfill_for_request(current["win"]))
    assert installed["win"] == current["win"]


def test_validate_install_race_regression(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl.trade_store._streamed_identity = None
    state = {"current": _window(ladder=1_000_000, step=1_000_000, endpoint=1_100_000)}
    ctrl._current_engine_window = lambda: state["current"]
    ctrl._installed_streamed_window = lambda: None
    ctrl._backfill_scheduler = _StubScheduler()
    cache = ctrl.aggtrade_cache
    cache.insert_trades("futures", "HYPEUSDT", [_agg_row(1, 1_000_000, 100.0, 1.0, 1, 1)], source="rest")
    state["current"] = _window(ladder=1_000_000, step=1_000_000, endpoint=1_100_000)

    async def fake_worker(request):
        state["current"] = _window(ladder=1_050_000, step=1_050_000, endpoint=1_100_000)

    ctrl._stream_backfill_for_request = fake_worker
    asyncio.run(ctrl._stream_backfill_for_request(_window()))
    assert state["current"].ladder_start_ms == 1_050_000


def test_worker_exception_releases_slot_and_accepts_next():
    seen = []
    released = asyncio.Event()

    async def worker(request):
        seen.append(request.endpoint_ts_ms)
        if len(seen) == 1:
            released.set()
            raise RuntimeError("boom")

    sched = LatestWinsScheduler(worker)

    async def run():
        await sched.request_window(_window(endpoint=10), kind="full")
        await released.wait()
        await sched.request_window(_window(endpoint=11), kind="full")
        await asyncio.sleep(0)
        await sched.wait_idle()

    asyncio.run(run())
    assert seen == [10, 11]
    assert sched.current_concurrent() == 0
    assert sched.is_running() is False
    assert sched.has_pending() is False


def test_stop_then_restart_clears_old_scheduler_generation(tmp_path, monkeypatch):
    ctrl = _make_controller(tmp_path, run_id="old-session")
    old = _StubScheduler()
    ctrl._backfill_scheduler = old

    async def noop_live():
        return None

    monkeypatch.setattr(ctrl, "_run_live_now", noop_live)
    asyncio.run(ctrl.stop())
    asyncio.run(ctrl.start_run(mode=SessionMode.LIVE, symbol="BTCUSDT", market="spot"))
    assert old.invalidations == ["old-session"]
    assert ctrl._stream_gen_token is None
