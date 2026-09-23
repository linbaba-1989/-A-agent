from types import SimpleNamespace

import pandas as pd

from ui.components.kline_chart import build_kline_figure
from ui.components.stock_header import stock_header_view
from ui.stock_research_service import (StockResearchService, market_speed, range_position,
                                       select_research_symbol, toggle_watchlist, trend_label)
from ui.view_models import FUNDAMENTAL_GAP_MESSAGE, format_amount, raw_json_expanded_default, safe_error


def test_600498_header_formats_real_snapshot_and_a_share_color():
    view = stock_header_view("600498.SH", {"lastPrice": 40.75, "lastClose": 40.13,
                                          "timestamp": "2026-09-11T15:31:51"}, "烽火通信")
    assert view["name"] == "烽火通信"
    assert view["last"] == "40.75"
    assert view["change"] == "+0.62"
    assert view["change_pct"] == "+1.54%"
    assert view["color"] == "#d92d20"
    assert stock_header_view("X", {"lastPrice": 9, "lastClose": 10}, "X")["color"] == "#079455"


def test_research_display_helpers_cover_closed_market_and_unavailable_values():
    assert format_amount(4_190_976_500) == "41.91亿"
    assert market_speed(1.2, "closed") == "unavailable"
    assert trend_label(10, "unavailable", 9, 8) == "中性"
    assert range_position(15, 10, 20) == 50.0
    assert range_position(10, 10, 10) == "unavailable"


def test_watchlist_toggle_and_scanner_navigation_share_symbol():
    watchlist = []
    assert toggle_watchlist(watchlist, "600498.SH") is True
    assert watchlist == ["600498.SH"]
    assert toggle_watchlist(watchlist, "600498.SH") is False
    session = {}
    select_research_symbol(session, "600498.SH")
    assert session == {"selected_symbol": "600498.SH", "nav_page": "个股研究"}


def test_ai_gap_error_and_raw_json_presentation_are_explicit():
    assert "基本面" in FUNDAMENTAL_GAP_MESSAGE and "公告" in FUNDAMENTAL_GAP_MESSAGE
    assert safe_error("provider exploded")[0] == "服务暂时不可用"
    assert raw_json_expanded_default() is False


class FakeProvider:
    def __init__(self):
        self.init_calls = 0

    def get_full_ticks(self, symbols):
        return {symbols[0]: {"time": 1789111911000, "lastPrice": 40.75, "lastClose": 40.13,
                             "open": 39.3, "high": 41.5, "low": 39.02, "volume": 1_036_632,
                             "pvolume": 103_663_200, "amount": 4_190_976_500}}

    def _valid_tick(self, tick): return True
    def normalized_instrument(self, symbol):
        return {"name": "烽火通信", "float_volume": 1_271_608_000, "total_volume": 1_271_608_000}
    def tick_timestamp(self, tick): return tick["time"] / 1000
    def normalize_tick(self, symbol, tick): return {"timestamp": "2026-09-11T15:31:51"}
    def get_history(self, symbols, period, count):
        dates = pd.date_range("2025-09-01", periods=250, freq="B")
        close = pd.Series(range(250), dtype=float) / 20 + 30
        frame = pd.DataFrame({"time": dates.astype("int64") // 1_000_000, "open": close - .2,
                              "high": close + .5, "low": close - .5, "close": close,
                              "volume": 1_000_000, "amount": 40_000_000, "suspendFlag": 0})
        return {symbols[0]: frame}


def test_service_reuses_supplied_provider_and_loads_history_once():
    provider = FakeProvider()
    scanner = SimpleNamespace(snapshot_history=SimpleNamespace(speed=lambda symbol, minute: 1.0),
                              _volume_ratio=lambda tick, history: 1.2)
    result = StockResearchService(provider, scanner, {"market": "已收盘"}).load("600498.SH")
    assert provider.init_calls == 0
    assert len(result.history) == 250
    assert result.facts["speed_1m"] == "unavailable"
    assert result.facts["ma60"] != "unavailable"


def test_service_accepts_qmt_provider_raw_instrument_detail():
    class RawDetailProvider(FakeProvider):
        normalized_instrument = None

        def get_instrument_detail(self, symbol):
            return {"InstrumentName": "烽火通信", "FloatVolume": 1_271_608_000,
                    "TotalVolume": 1_271_608_000}

    scanner = SimpleNamespace(snapshot_history=SimpleNamespace(speed=lambda symbol, minute: 1.0),
                              _volume_ratio=lambda tick, history: 1.2)
    result = StockResearchService(RawDetailProvider(), scanner, {"market": "已收盘"}).load("600498.SH")
    assert result.facts["name"] == "烽火通信"
    assert result.facts["turnover_rate"] != "unavailable"


def test_kline_uses_chinese_up_red_down_green_and_contains_volume():
    history = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=3),
                            "open": [10, 11, 10], "high": [12, 12, 11], "low": [9, 9, 8],
                            "close": [11, 10, 10], "volume": [100, 200, 150]})
    figure = build_kline_figure(history, 20)
    assert figure.data[0].increasing.line.color == "#d92d20"
    assert figure.data[0].decreasing.line.color == "#079455"
    assert figure.data[-1].name == "成交量"


def test_history_prefers_exchange_trading_date_index_over_utc_milliseconds():
    from ui.stock_research_service import prepare_history
    frame = pd.DataFrame({"time": [1789056000000], "open": [10], "high": [11], "low": [9],
                          "close": [10], "volume": [1]}, index=[20260911])
    assert prepare_history(frame).iloc[0]["date"].strftime("%Y-%m-%d") == "2026-09-11"
