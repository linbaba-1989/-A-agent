"""Differential history tests against the pre-compact speed calculation."""
from collections import deque
from datetime import datetime
import gc

import pytest

from src.compact_market_history import CompactMarketHistory, RealtimePoint
from src.indicators import UNAVAILABLE
from src.market_clock import OPEN, to_beijing
from src.realtime_market import RealtimeMarketFeed, SnapshotConsumerState, rank_rows
from src.streaming_ui import SnapshotBus, build_payload


BASE = to_beijing(datetime(2026, 9, 16, 10)).timestamp()


class LegacyReference:
    """Old append/speed algorithm; no count truncation for a time-window oracle."""
    def __init__(self, maxlen=None):
        self.series = deque(maxlen=maxlen)
        self.session = None

    def append(self, timestamp, price):
        session = CompactMarketHistory.trading_session(timestamp)
        if session is None:
            return
        if session != self.session:
            self.series.clear()
            self.session = session
        if self.series and timestamp < self.series[-1][0]:
            return
        if self.series and timestamp == self.series[-1][0]:
            self.series[-1] = (timestamp, price)
        else:
            self.series.append((timestamp, price))

    def speed(self, minutes):
        current = self.series[-1]
        eligible = [point for point in self.series if point[0] <= current[0] - minutes * 60]
        if not eligible:
            return UNAVAILABLE
        reference = max(eligible, key=lambda point: point[0])
        if reference[1] <= 0:
            return UNAVAILABLE
        return round((current[1] / reference[1] - 1) * 100, 4)


@pytest.mark.parametrize("intervals", [(1,), (2,), (5,), (1, 2, 5, 1.25, 2.75), (.5, .25, 1)])
def test_one_three_five_minute_speeds_match_old_formula_through_wrap_and_growth(intervals):
    history = CompactMarketHistory(maxlen=8)
    oracle, old_300 = LegacyReference(), LegacyReference(300)
    elapsed = 0.0
    for frame in range(1500):
        elapsed += intervals[frame % len(intervals)]
        # Deterministic fractional values exercise rounding with float64 precision.
        price = 33.333333 + (frame % 37 - 18) * .010003
        stamp = BASE + elapsed
        history.append_price("600498.SH", stamp, price)
        oracle.append(stamp, price)
        old_300.append(stamp, price)
        for minutes in (1, 3, 5):
            result = history.speed("600498.SH", minutes)
            assert result == oracle.speed(minutes)
            # All values the old 300-point buffer can compute remain identical.
            if old_300.speed(minutes) != UNAVAILABLE:
                assert result == old_300.speed(minutes)
    assert history.diagnostics()["snapshot_points"] < len(oracle.series)


def test_sparse_asynchronous_symbols_keep_own_time_and_boundary_predecessor():
    history = CompactMarketHistory(maxlen=8)
    symbols = ("600498.SH", "600000.SH", "000001.SZ")
    oracles = {symbol: LegacyReference() for symbol in symbols}
    for frame in range(1000):
        for index, symbol in enumerate(symbols):
            if frame % (index + 1):
                continue
            stamp = BASE + frame + index * .123
            price = 10 + (frame % 50) * .02
            history.append_price(symbol, stamp, price)
            oracles[symbol].append(stamp, price)
        for symbol in symbols:
            for minutes in (1, 3, 5):
                assert history.speed(symbol, minutes) == oracles[symbol].speed(minutes)
    # A 20-minute gap keeps one old reference, never invents an intermediate tick.
    history.append_price(symbols[0], BASE + 2200, 20)
    oracles[symbols[0]].append(BASE + 2200, 20)
    assert len(history.points(symbols[0])) == 2
    for minutes in (1, 3, 5):
        assert history.speed(symbols[0], minutes) == oracles[symbols[0]].speed(minutes)


def test_missing_exact_window_timestamp_uses_nearest_earlier_not_later():
    history = CompactMarketHistory()
    for second, price in ((0, 10), (59, 11), (61, 19), (179, 12), (181, 18), (299, 13), (301, 17), (360, 20)):
        history.append_price("A", BASE + second, price)
    assert history.speed("A", 1) == round((20 / 13 - 1) * 100, 4)
    assert history.speed("A", 3) == round((20 / 12 - 1) * 100, 4)
    assert history.speed("A", 5) == round((20 / 11 - 1) * 100, 4)


def test_session_boundaries_duplicate_out_of_order_and_invalid_quotes():
    history = CompactMarketHistory()
    stamp = lambda text: to_beijing(datetime.fromisoformat(text)).timestamp()
    morning = stamp("2026-09-16 11:29:00")
    assert history.append_price("A", morning, 10)
    assert history.append_price("A", morning, 11)
    assert len(history.points("A")) == 1
    assert history.prices("A") == [11]
    assert not history.append_price("A", morning - 1, 20)
    assert not history.append_price("A", stamp("2026-09-16 12:00:00"), 20)
    assert not history.append_price("A", float("nan"), 20)
    assert not history.append_price("A", morning + 1, float("inf"))
    for text in ("2026-09-16 13:00:00", "2026-09-17 09:30:00"):
        assert history.append_price("A", stamp(text), 12)
        assert len(history.points("A")) == 1
        assert all(history.speed("A", minutes) == UNAVAILABLE for minutes in (1, 3, 5))
    assert not history.append_price("A", morning, 50)
    assert history.prices("A") == [12]


def test_numeric_storage_stabilizes_and_never_retains_realtimepoint_objects():
    history = CompactMarketHistory()
    symbols = [f"S{i}" for i in range(64)]
    history.reserve_symbols(symbols)
    before = sum(isinstance(obj, RealtimePoint) for obj in gc.get_objects())
    checkpoints = []
    for frame in range(1000):
        for symbol in symbols:
            history.append_price(symbol, BASE + frame, 10 + frame / 100)
        if frame in (500, 999):
            checkpoints.append(history.diagnostics())
    assert sum(isinstance(obj, RealtimePoint) for obj in gc.get_objects()) == before
    assert checkpoints[0]["snapshot_points"] == checkpoints[1]["snapshot_points"] == 64 * 361
    assert checkpoints[0]["history_storage_bytes"] == checkpoints[1]["history_storage_bytes"]
    assert history.speed(symbols[0], 5) != UNAVAILABLE
    # UI views are disposable and do not populate history with Python objects.
    assert len(history.points(symbols[0])) == 361
    assert history.prices(symbols[0], 3) == [10 + frame / 100 for frame in (997, 998, 999)]


def test_symbol_index_expansion_preserves_wrapped_rings_and_price_precision():
    history = CompactMarketHistory(maxlen=8)
    oracle = LegacyReference()
    for frame in range(450):
        price = 12.123456789 + frame * .000000001
        history.append_price("A", BASE + frame * 2, price)
        oracle.append(BASE + frame * 2, price)
    history.reserve_symbols(f"NEW{i}" for i in range(130))
    for minutes in (1, 3, 5):
        assert history.speed("A", minutes) == oracle.speed(minutes)
    assert history.points("A")[-1].last_price == oracle.series[-1][1]
    assert history.points("missing") == history.prices("missing") == []
    assert history.speed("missing", 1) == UNAVAILABLE


def test_feed_top20_speeds_flash_payload_and_single_source_contract(monkeypatch):
    symbols = ["600498.SH", "600000.SH", "000001.SZ"] + [f"S{i}" for i in range(24)]

    class FakeProvider:
        def __init__(self):
            self.frame = 0
            self.calls = 0

        def get_stock_universe(self):
            return symbols

        def get_full_ticks(self, selected):
            self.calls += 1
            return {symbol: {"time": (BASE + self.frame * 2) * 1000,
                             "lastPrice": 10 + (index + 1) * .001 * (self.frame // 2),
                             "lastClose": 10, "volume": 100 + self.frame, "amount": 1000 + self.frame}
                    for index, symbol in enumerate(selected)}

        def _valid_tick(self, tick, require_time=False):
            return True

        def tick_timestamp(self, tick):
            return tick["time"] / 1000

    provider = FakeProvider()
    feed = RealtimeMarketFeed(provider, snapshot_cache=None)
    consumer, bus = SnapshotConsumerState(), SnapshotBus()
    oracles = {symbol: LegacyReference() for symbol in symbols}
    # Production snapshot ingestion must bypass the compatibility point constructor.
    def forbidden(*args, **kwargs):
        raise AssertionError("per-tick Python point construction")
    monkeypatch.setattr("src.realtime_market.RealtimePoint", forbidden)
    for frame in range(200):
        provider.frame = frame
        now = datetime.fromtimestamp(BASE + frame * 2, tz=to_beijing().tzinfo)
        rows = feed.snapshot(force=True, market_session=OPEN, now=now)
        for row in rows:
            oracle = oracles[row["symbol"]]
            oracle.append(row["quote_timestamp"], row["lastPrice"])
            for minutes in (1, 3, 5):
                assert row[f"speed_{minutes}m"] == oracle.speed(minutes)
        viewed = consumer.consume(rows)
        if frame % 2:
            assert all(row["flash_class"] == "" for row in viewed)
        assert all(row["flash_class"] == "" for row in consumer.consume(rows))
        payload = build_payload(feed, rows, BASE + max(0, frame - 1) * 2, now)
        bus.publish(payload)
        expected = rank_rows(rows, "change_pct", 20)
        assert [row["symbol"] for row in payload["top20"]] == [row["symbol"] for row in expected]
        assert payload["target"]["lastPrice"] == rows[0]["lastPrice"]
        assert payload["provider_init_count"] == 1
        assert feed.provider is provider
    assert provider.calls == feed.provider_request_count == 200
    assert payload["status"]["quote_status"] == "LIVE"
    assert len(feed._latest_rows) == len(feed._latest_ticks) == len(symbols)
    assert len(feed.request_diagnostics) == 200
    assert bus.latest is payload
