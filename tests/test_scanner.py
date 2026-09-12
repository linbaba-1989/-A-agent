import pandas as pd

from src.models import MarketDiagnostics, ProviderStatus
from src.qmt_provider import QMTProvider
from src.scanner import MarketScanner, calculate_score


class FakeProvider(QMTProvider):
    def connection_diagnostics(self):
        return MarketDiagnostics(True, "test-only", xtquant_imported=True, rpc_request_success=True, full_tick_success=True)

    def status(self):
        return ProviderStatus(True, "fake", {"xtquant_path": "test-only"})

    def get_stock_universe(self):
        return ["000001.SZ", "600000.SH"]

    def get_full_ticks(self, symbols, batch_size=500):
        return {
            symbol: {"lastPrice": 12, "lastClose": 10, "open": 10, "high": 12, "low": 9,
                     "volume": 2000, "pvolume": 200_000, "amount": 2_400_000, "time": 1_700_000_000_000}
            for symbol in symbols
        }

    def get_instrument_detail(self, symbol):
        return {"InstrumentName": symbol, "FloatVolume": 10_000_000, "TotalVolume": 10_000_000,
                "IsTrading": True, "InstrumentStatus": 0}

    def get_local_history(self, symbols, count=61, batch_size=300):
        size = 61
        frame = pd.DataFrame({"time": range(size), "open": range(1, size + 1), "high": range(2, size + 2),
                              "low": range(size), "close": range(1, size + 1),
                              "volume": [1000] * size, "amount": [1_000_000] * size,
                              "suspendFlag": [0] * size})
        return {symbol: frame for symbol in symbols}


def test_scanner_reports_counts_and_score_breakdown():
    result = MarketScanner(FakeProvider()).scan(30)
    assert result.diagnostics.stock_pool_size == 2
    assert result.diagnostics.full_tick_count == 2
    assert result.diagnostics.valid_quote_count == 2
    assert len(result.rows) == 2
    assert 0 <= result.rows[0]["local_score"] <= 100
    assert result.rows[0]["turnover_rate"] == 2.0
    assert result.rows[0]["turnover_rate_raw"] == 2.0


def test_score_is_transparent_sum():
    row = {"lastPrice": 11, "ma5": 10, "ma10": 9, "ma20": 8, "change_pct": 3,
           "speed_5m": 1, "volume_ratio": 1.5, "high_5d": 10.5, "high_20d": 12}
    scores = calculate_score(row)
    assert scores["local_score"] == sum(value for key, value in scores.items() if key != "local_score")


def test_scanner_stops_when_qmt_is_disconnected():
    provider = FakeProvider()
    provider.connection_diagnostics = lambda: MarketDiagnostics(False, None, message="QMT offline")
    result = MarketScanner(provider).scan()
    assert result.rows == []
    assert not result.diagnostics.connected
    assert "offline" in result.diagnostics.message
