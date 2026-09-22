from datetime import datetime
from types import SimpleNamespace

import pytest

from src.realtime_market import (RealtimeMarketFeed, RealtimePoint, RealtimeSnapshotBuffer, SnapshotConsumerState,
                                 live_state, market_quote_timestamp, price_flash, rank_rows,
                                 refresh_interval_seconds)
from src.market_clock import OPEN
from ui.pages.realtime_market import _events


def ts(text):
    return datetime.fromisoformat(text).timestamp()


def test_price_flash_uses_a_share_colors_and_unchanged_has_no_flash():
    assert price_flash(10, 10.1) == "price-flash-up"
    assert price_flash(10, 9.9) == "price-flash-down"
    assert price_flash(10, 10) == ""


def test_buffer_capacity_hint_does_not_truncate_time_window():
    buf = RealtimeSnapshotBuffer(maxlen=3)
    for minute, price in enumerate((10, 11, 12, 13)):
        buf.append("A", RealtimePoint(ts(f"2026-09-14 10:0{minute}:00"), price))
    assert len(buf.points("A")) == 4
    assert buf.speed("A", 1) == round((13 / 12 - 1) * 100, 4)
    assert buf.speed("A", 3) == 30.0


def test_one_three_five_minute_speeds_use_same_session_snapshots():
    buf = RealtimeSnapshotBuffer()
    for minute, price in ((0, 10), (1, 11), (3, 12), (5, 15)):
        buf.append("A", RealtimePoint(ts(f"2026-09-14 10:0{minute}:00"), price))
    assert buf.speed("A", 1) == round((15 / 12 - 1) * 100, 4)
    assert buf.speed("A", 3) == round((15 / 11 - 1) * 100, 4)
    assert buf.speed("A", 5) == 50.0


def test_buffer_does_not_cross_lunch_or_trading_day():
    buf = RealtimeSnapshotBuffer()
    buf.append("A", RealtimePoint(ts("2026-09-14 11:30:00"), 10))
    buf.append("A", RealtimePoint(ts("2026-09-14 13:00:00"), 11))
    assert len(buf.points("A")) == 1 and buf.speed("A", 1) == "unavailable"
    buf.append("A", RealtimePoint(ts("2026-09-15 09:30:00"), 12))
    assert len(buf.points("A")) == 1 and buf.points("A")[0].last_price == 12


def test_market_closed_stops_refresh_and_live_requires_progress():
    stamp = ts("2026-09-14 10:00:00")
    assert refresh_interval_seconds(False, True, 2) is None
    assert refresh_interval_seconds(True, False, 2) is None
    assert refresh_interval_seconds(True, True, 2) == 2
    assert live_state(None, stamp) == "STALE"
    assert live_state(stamp - 1, stamp) == "LIVE"
    assert live_state(None, ts("2026-09-14 15:31:00")) == "CLOSED"


def test_top_n_excludes_unavailable_and_never_invents_rows():
    rows = [{"symbol": "A", "change_pct": 1}, {"symbol": "B", "change_pct": "unavailable"},
            {"symbol": "C", "change_pct": 3}]
    assert [row["symbol"] for row in rank_rows(rows, "change_pct", 1)] == ["C"]
    assert rank_rows([], "change_pct", 20) == []
    assert _events({}, [{"symbol": "A", "change_pct": 8, "amount": 2_000_000_000,
                         "speed_1m": 2, "name": "真实股票"}]) == []


class Provider:
    def __init__(self):
        self.calls = 0

    def get_stock_universe(self): return ["A"]
    def get_full_ticks(self, symbols):
        self.calls += 1
        return {"A": {"lastPrice": 10 + self.calls, "lastClose": 10, "time": ts("2026-09-14 10:00:00") * 1000,
                      "volume": 100, "amount": 1000}}
    def _valid_tick(self, tick): return True
    def tick_timestamp(self, tick): return tick["time"] / 1000


def test_refresh_reuses_one_provider_instance():
    provider = Provider()
    feed = RealtimeMarketFeed(provider)
    feed.snapshot(); feed._last_request_finished = 0; feed.snapshot()
    assert feed.provider is provider and feed.provider_initializations == 1 and provider.calls == 2


def test_overlapping_snapshot_is_skipped_without_provider_request():
    provider = Provider()
    feed = RealtimeMarketFeed(provider)
    feed._lock.acquire()
    try:
        assert feed.snapshot() == [] and provider.calls == 0
    finally:
        feed._lock.release()
    assert feed.skipped_due_to_lock == 1 and feed.overlapping_request_count == 0


def test_multiple_consumers_reuse_recent_central_snapshot():
    provider = Provider()
    feed = RealtimeMarketFeed(provider)
    whole_market = feed.snapshot()
    watchlist = feed.snapshot(["A"])
    research = feed.snapshot(["A"])
    assert whole_market and watchlist and research
    assert provider.calls == 1 and feed.provider_request_count == 1 and feed.cache_hit_count == 2


def view_row(seq, price, from_cache=False):
    return {"symbol": "600498.SH", "snapshot_seq": seq, "lastPrice": price,
            "from_cache": from_cache}


def test_down_flash_occurs_once_then_ten_cache_reads_do_not_repeat():
    consumer = SnapshotConsumerState()
    assert consumer.consume([view_row(1, 40.12)])[0]["flash_class"] == ""
    assert consumer.consume([view_row(2, 40.11)])[0]["flash_class"] == "price-flash-down"
    for _ in range(10):
        assert consumer.consume([view_row(2, 40.11, True)])[0]["flash_class"] == ""


def test_up_flash_occurs_once_and_new_sequence_same_price_does_not_flash():
    consumer = SnapshotConsumerState()
    consumer.consume([view_row(1, 40.10)])
    assert consumer.consume([view_row(2, 40.11)])[0]["flash_class"] == "price-flash-up"
    assert consumer.consume([view_row(2, 40.11, True)])[0]["flash_class"] == ""
    assert consumer.consume([view_row(3, 40.11)])[0]["flash_class"] == ""


def test_consumers_receive_flash_independently_once_per_sequence():
    first, second = SnapshotConsumerState(), SnapshotConsumerState()
    for consumer in (first, second):
        consumer.consume([view_row(1, 40.10)])
    assert first.consume([view_row(2, 40.11)])[0]["flash_class"] == "price-flash-up"
    assert second.consume([view_row(2, 40.11)])[0]["flash_class"] == "price-flash-up"
    assert first.consume([view_row(2, 40.11, True)])[0]["flash_class"] == ""
    assert second.consume([view_row(2, 40.11, True)])[0]["flash_class"] == ""


def test_feed_cache_contains_sequence_metadata_but_never_flash_event():
    provider = Provider()
    feed = RealtimeMarketFeed(provider)
    fresh = feed.snapshot()
    cached = feed.snapshot(["A"])
    assert fresh[0]["snapshot_seq"] == cached[0]["snapshot_seq"] == 1
    assert not fresh[0]["from_cache"] and cached[0]["from_cache"]
    assert "flash_class" not in fresh[0] and "flash_class" not in cached[0]


class SequencedProvider:
    def __init__(self, quote_times, prices):
        self.quote_times = list(quote_times)
        self.prices = list(prices)
        self.calls = 0

    def get_stock_universe(self):
        return ["A"]

    def get_full_ticks(self, symbols):
        index = min(self.calls, len(self.quote_times) - 1)
        self.calls += 1
        quote_time = ts(self.quote_times[index])
        price = self.prices[min(index, len(self.prices) - 1)]
        return {symbol: {"lastPrice": price, "lastClose": 10, "time": quote_time * 1000,
                         "volume": 100, "amount": 1000} for symbol in symbols}

    def _valid_tick(self, tick, require_time=False):
        return bool(tick and tick.get("lastPrice") and tick.get("lastClose") and
                    (not require_time or tick.get("time")))

    def tick_timestamp(self, tick):
        return tick["time"] / 1000


def test_provider_raw_time_progresses_to_feed_live_with_request_diagnostics():
    provider = SequencedProvider(("2026-09-15 09:44:00", "2026-09-15 09:45:00"), (10, 10.1))
    feed = RealtimeMarketFeed(provider)
    first = feed.snapshot(force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 45))
    second = feed.snapshot(force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 45, 1))

    diagnostics = feed.diagnostics()
    assert provider.calls == feed.provider_request_count == 2
    assert [first[0]["snapshot_seq"], second[0]["snapshot_seq"]] == [1, 2]
    assert diagnostics[0]["quote_status"] == "STALE"
    assert diagnostics[1]["quote_status"] == "LIVE"
    assert diagnostics[1]["provider_raw_max_quote_time"].startswith("2026-09-15T09:45:00")
    assert diagnostics[1]["feed_quote_time"].startswith("2026-09-15T09:45:00")
    assert diagnostics[1]["valid_quote_count"] == diagnostics[1]["provider_raw_valid_quote_count"] == 1
    assert diagnostics[1]["from_cache"] is False
    assert diagnostics[1]["request_started_at"] and diagnostics[1]["request_finished_at"]


def test_provider_raw_time_not_progressing_is_stale_during_open():
    provider = SequencedProvider(("2026-09-15 09:44:00", "2026-09-15 09:44:00"), (10, 10))
    feed = RealtimeMarketFeed(provider)
    feed.snapshot(force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 45))
    feed.snapshot(force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 46))

    diagnostics = feed.diagnostics()
    assert provider.calls == 2
    assert diagnostics[-1]["quote_status"] == "STALE"
    assert diagnostics[-1]["provider_raw_max_quote_time"] == diagnostics[-2]["provider_raw_max_quote_time"]
    assert diagnostics[-1]["feed_snapshot_seq"] == 2


def test_successful_sequence_increment_replaces_rows_and_cache_marks_from_cache():
    provider = SequencedProvider(("2026-09-15 09:44:00", "2026-09-15 09:44:00"), (10, 11))
    feed = RealtimeMarketFeed(provider)
    first = feed.snapshot(["A"], force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 45))
    second = feed.snapshot(["A"], force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 46))
    cached = feed.cached(["A"])

    diagnostic = feed.diagnostics()[-1]
    assert first[0]["lastPrice"] == 10 and second[0]["lastPrice"] == 11
    assert second[0]["snapshot_seq"] == first[0]["snapshot_seq"] + 1 == 2
    assert diagnostic["rows_replaced_count"] == diagnostic["valid_quote_count"] == 1
    assert diagnostic["rows_changed_count"] == 1
    assert cached[0]["snapshot_seq"] == 2 and cached[0]["from_cache"] is True


def test_snapshot_sequence_does_not_increment_when_provider_fetch_fails():
    class FailingProvider(SequencedProvider):
        def get_full_ticks(self, symbols):
            self.calls += 1
            raise RuntimeError("provider_fetch_failed")

    feed = RealtimeMarketFeed(FailingProvider(("2026-09-15 09:44:00",), (10,)))
    with pytest.raises(RuntimeError, match="provider_fetch_failed"):
        feed.snapshot(["A"], force=True, market_session=OPEN, now=datetime(2026, 9, 15, 9, 45))
    assert feed.snapshot_seq == 0
    assert feed.diagnostics()[-1]["error"] == "RuntimeError"


def test_market_quote_time_uses_newest_valid_row_not_first_or_symbol_cache():
    old = ts("2026-09-15 09:44:00")
    new = ts("2026-09-15 09:45:00")
    rows = [{"symbol": "600498.SH", "quote_timestamp": old},
            {"symbol": "600000.SH", "quote_timestamp": new},
            {"symbol": "000001.SZ", "quote_timestamp": "unavailable"}]
    assert market_quote_timestamp(rows) == new


def test_non_trading_symbol_does_not_regress_market_timestamp():
    class MultiSymbolProvider(SequencedProvider):
        def get_stock_universe(self):
            return ["600498.SH", "600000.SH"]

        def get_full_ticks(self, symbols):
            self.calls += 1
            values = {"600498.SH": ts("2026-09-15 09:40:00"),
                      "600000.SH": ts("2026-09-15 09:45:00")}
            return {symbol: {"lastPrice": 10, "lastClose": 10, "time": values[symbol] * 1000}
                    for symbol in symbols}

    feed = RealtimeMarketFeed(MultiSymbolProvider(("unused",), (10,)))
    rows = feed.snapshot(["600498.SH", "600000.SH"], force=True, market_session=OPEN,
                         now=datetime(2026, 9, 15, 9, 45))
    single = feed.snapshot(["600498.SH"], force=True, market_session=OPEN,
                           now=datetime(2026, 9, 15, 9, 46))
    assert market_quote_timestamp(rows) == ts("2026-09-15 09:45:00")
    assert market_quote_timestamp(single, feed.last_quote_timestamp) == ts("2026-09-15 09:45:00")
