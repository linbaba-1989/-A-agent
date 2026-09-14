import pandas as pd
from types import SimpleNamespace

from src.history_service import HistoricalDataService
from src.indicators import UNAVAILABLE
from src.market_cache import realtime_ma
from src.scanner import MarketScanner
from ui.stock_research_service import StockResearchService


def frame(size=65):
    return pd.DataFrame({"time": range(size), "open": range(size), "high": range(1, size + 1),
                         "low": range(size), "close": range(1, size + 1),
                         "volume": [100] * size, "amount": [1000] * size,
                         "suspendFlag": [0] * size})


class HistoryProvider:
    def __init__(self, initially_empty=()):
        self.initially_empty = set(initially_empty)
        self.downloaded = set()
        self.backend = self

    def request(self, command, **payload):
        assert command == "download_history_data"
        self.downloaded.update(payload["symbols"])

    def get_local_history(self, symbols, count=65):
        return {symbol: (frame(count) if symbol not in self.initially_empty or symbol in self.downloaded
                         else pd.DataFrame()) for symbol in symbols}

    def get_history(self, symbols, period, count):
        return self.get_local_history(symbols, count)

    def get_full_ticks(self, symbols):
        return {symbol: {"time": 1_789_111_911_000, "lastPrice": 66, "lastClose": 65,
                         "open": 65, "high": 67, "low": 64, "volume": 100,
                         "pvolume": 10_000, "amount": 6_600} for symbol in symbols}

    def _valid_tick(self, tick): return True
    def normalized_instrument(self, symbol):
        return {"name": symbol, "float_volume": 1_000_000, "total_volume": 1_000_000}
    def tick_timestamp(self, tick): return tick["time"] / 1000
    def normalize_tick(self, symbol, tick): return {"timestamp": "2026-09-11T15:31:51"}


def test_unopened_research_symbol_gets_ma_from_scanner_initialization():
    provider = HistoryProvider(initially_empty={"B.SZ"})
    service = HistoricalDataService(provider)
    service.initialize(["A.SH", "B.SZ"], 65)
    assert service.status == "ready"
    assert "B.SZ" in provider.downloaded
    indicators = service.indicators("B.SZ", None)
    assert realtime_ma(indicators["closes"], 66, 60) != UNAVAILABLE


def test_cache_not_ready_is_distinct_from_unavailable_and_short_history():
    service = HistoricalDataService(HistoryProvider())
    assert service.reason("A.SH") == "cache_not_ready"
    service.status = "partial"
    assert service.reason("A.SH") == "history_unavailable"
    service.frames["A.SH"] = frame(20)
    assert service.reason("A.SH") == "insufficient_history"


def test_sixty_or_more_bars_calculate_all_realtime_mas():
    service = HistoricalDataService(HistoryProvider())
    service.initialize(["A.SH"], 65)
    values = service.indicators("A.SH", None)["closes"]
    assert all(realtime_ma(values, 66, window) != UNAVAILABLE for window in (5, 10, 20, 60))
    assert service.info().status == "ready"


def test_scanner_cache_is_the_shared_history_service_cache():
    provider = HistoryProvider()
    scanner = MarketScanner(provider)
    scanner.history_service.initialize(["A.SH"], 65)
    before = scanner.history_service.indicators("A.SH", None)["closes"][-1]
    scanner.history_service.ensure("A.SH", 250)
    after = scanner.history_service.indicators("A.SH", None)["closes"][-1]
    assert before == 65 and after == 250
    assert scanner.history_frame_cache is scanner.history_service.frames


def test_scanner_and_stock_research_use_identical_ma_source():
    provider = HistoryProvider()
    scanner = MarketScanner(provider)
    scanner.history_service.initialize(["A.SH"], 65)
    scanner.snapshot_history = SimpleNamespace(speed=lambda symbol, minute: UNAVAILABLE)
    snapshot = StockResearchService(provider, scanner, {"market": "已收盘"}).load("A.SH", 65)
    indicators = scanner.history_service.indicators("A.SH", None)
    assert snapshot.facts["ma20"] == realtime_ma(indicators["closes"], 66, 20)
