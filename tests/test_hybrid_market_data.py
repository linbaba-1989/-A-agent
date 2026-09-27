"""Offline consumer/routing tests. No network or paid runtime is required."""
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from threading import Event, Thread
import json
import pandas as pd
import pytest
from src.market_clock import BEIJING_TZ, DEFAULT_TRADING_CALENDAR
from src.market_data.contracts import MarketSnapshot, SnapshotBatch, Security
from src.market_data.quality import Health
from src.market_data.free_provider import FreeMarketDataProvider
from src.market_data.free_history_provider import FreeHistoryProvider
from src.market_data.history_cache import FreeHistoryCache
from src.market_data.historical import HistoricalBar, HistoryResult
from src.market_data.hithink_client import ApiResult, HithinkRestClient
from src.market_data.hithink_provider import HithinkOfficialProvider
from src.market_data.hithink_reference import ReferenceCache, HithinkSectorProvider
from src.market_data.hybrid_provider import HybridMarketDataProvider, HISTORY_FALLBACK_STATUSES
from src.market_data.factory import create_market_provider, market_data_mode
from src.acceptance_source import source_request
from src.scanner import MarketScanner
from src.realtime_market import RealtimeMarketFeed
from ui.stock_research_service import StockResearchService
from src.workforce_acceptance import build_fact_bundle

SYMBOL = "600498.SH"
NOW = datetime(2026, 9, 24, 10, tzinfo=BEIJING_TZ)


def history(source="hithink", count=130):
    days, day = [], NOW.date() - timedelta(days=1)
    while len(days) < count:
        if DEFAULT_TRADING_CALENDAR.is_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    return HistoryResult(SYMBOL, [HistoricalBar(SYMBOL, d, 40, 43, 39, 41, 1000, 41000,
                         source=source) for d in reversed(days)], source=source,
                         data_status="COMPLETE", quality_status="VALID")


class Realtime:
    def __init__(self, source="tencent", good=True):
        self.source, self.good, self.calls = source, good, 0
        self.health = Health()
    def snapshot(self, symbols):
        self.calls += 1
        quote = MarketSnapshot(SYMBOL, price=42 if self.good else None, prev_close=41,
            open=41, high=43, low=40, volume_shares=2000, amount_cny=84000,
            turnover_rate=2 if self.source == "tencent" else None,
            total_market_cap=1e9 if self.source == "tencent" else None,
            source=self.source, quote_time=NOW, quote_status="CACHED" if self.good else "UNAVAILABLE")
        result = SnapshotBatch({s: replace(quote, symbol=s) for s in symbols}, symbols, symbols,
            symbols if self.good else [], [], [], 1, 1 if self.good else 0, .1, self.source,
            market_session="closed")
        self.health.observe(result)
        return result


class Daily:
    name = "akshare"
    endpoint = "fixture"
    def __init__(self, source="akshare", good=True):
        self.name, self.good, self.calls = source, good, 0
    def supports(self, *a):
        return True
    def daily(self, *a):
        self.calls += 1
        return history(self.name) if self.good else HistoryResult(SYMBOL, source=self.name)


@pytest.fixture
def hybrid(tmp_path):
    universe = SimpleNamespace(get=lambda: [Security(SYMBOL, "free-name", "SH", "MAIN")])
    real = FreeMarketDataProvider(universe, Realtime(), Realtime("sina"), timer=lambda: 100)
    official = HithinkOfficialProvider(HithinkRestClient(api_key="", transport=lambda *a: pytest.fail("network")), tmp_path/"official", clock=lambda: NOW)
    official.universe.refresh = lambda: ApiResult("CACHED")
    official.universe.rows = {SYMBOL: dict(symbol=SYMBOL, name="official-name", exchange="SH", board="MAIN")}
    official.history.load = lambda s, count=120, *a, **k: history(count=count)
    fallback = FreeHistoryProvider(Daily(), Daily("baostock"), FreeHistoryCache(tmp_path/"free"), clock=lambda: NOW)
    result = HybridMarketDataProvider(real, official, fallback, timer=lambda:100)
    yield result
    result.close()


def test_hybrid_primary_routes_without_official_realtime(hybrid):
    hybrid.official.index_snapshot = lambda *a: pytest.fail("unexpected official realtime")
    assert hybrid.get_stock_universe() == [SYMBOL]
    quote = hybrid.quote(SYMBOL)
    assert quote.source == "tencent" and quote.volume_shares == 2000
    result = hybrid.get_daily_history(SYMBOL)
    assert result.source == "hithink" and len(result.bars) == 120
    assert hybrid.history_fallback.primary.calls == 0


def test_sina_fallback_has_no_borrowed_enhanced_fields(hybrid):
    hybrid.realtime.primary.good = False
    for _ in range(3):
        quote = hybrid.quote(SYMBOL)
    assert quote.source == "sina" and quote.total_market_cap is None
    assert quote.turnover_rate is None
    assert hybrid._tick(quote)["field_provenance"]["total_market_cap"] is None


@pytest.mark.parametrize("status", sorted(HISTORY_FALLBACK_STATUSES))
def test_allowed_official_failures_use_complete_alternative(hybrid, status):
    hybrid.official.history.load = lambda *a, **k: HistoryResult(SYMBOL, source="hithink", warnings=[status])
    result = hybrid.get_daily_history(SYMBOL)
    assert result.source == "akshare" and result.fallback_used
    assert {b.source for b in result.bars} == {"akshare"}
    assert "hithink:" + status in result.warnings
    assert hybrid.quote(SYMBOL).source == "tencent"


@pytest.mark.parametrize("status", ["DATA_NOT_READY", "SYMBOL_NOT_FOUND", "PROTOCOL_ERROR", "SERVER_ERROR", "UPSTREAM_TIMEOUT"])
def test_other_failures_do_not_silently_fallback(hybrid, status):
    hybrid.official.history.load = lambda *a, **k: HistoryResult(SYMBOL, source="hithink", warnings=[status])
    assert hybrid.get_daily_history(SYMBOL).source == "hithink"
    assert hybrid.history_fallback.primary.calls == 0


def test_akshare_failure_uses_baostock(hybrid):
    hybrid.official.history.load = lambda *a, **k: HistoryResult(SYMBOL, source="hithink", warnings=["AUTH_INVALID"])
    hybrid.history_fallback.primary.good = False
    result = hybrid.get_daily_history(SYMBOL)
    assert result.source == "baostock" and result.quality_status == "VALID"
    assert {b.source for b in result.bars} == {"baostock"}


def test_auth_missing_real_client_degrades_universe_history_and_index(hybrid, tmp_path):
    original = HithinkOfficialProvider(HithinkRestClient(api_key="", transport=lambda *a: pytest.fail("network")),
                                      tmp_path/"missing-key", clock=lambda: NOW)
    hybrid.official = original
    assert hybrid.get_stock_universe() == [SYMBOL]
    assert hybrid.provider_status()["universe"].startswith("free_universe:")
    assert hybrid.provider_status()["official_auth"] == "AUTH_NOT_CONFIGURED"
    assert hybrid.get_daily_history(SYMBOL).source == "akshare"
    assert hybrid.get_index_snapshot().capability_status == "AUTH_NOT_CONFIGURED"
    assert hybrid.quote(SYMBOL).source == "tencent"


def test_index_and_calendar_routing_cached(hybrid):
    calls = []
    hybrid.official.index_snapshot = lambda symbols: calls.append(symbols) or SimpleNamespace(capability_status="SUCCESS")
    assert hybrid.get_index_snapshot() is hybrid.get_index_snapshot()
    assert len(calls) == 1 and len(calls[0]) == 3
    assert hybrid.get_index_history("000001.SH").source == "hithink"
    assert hybrid.get_calendar() is hybrid.official.calendar


def test_history_block_does_not_block_realtime(hybrid):
    entered, release = Event(), Event()
    def blocked(*a, **k):
        entered.set()
        assert release.wait(5)
        return history()
    hybrid.official.history.load = blocked
    worker = Thread(target=lambda: hybrid.get_daily_history(SYMBOL))
    worker.start()
    try:
        assert entered.wait(2)
        assert hybrid.quote(SYMBOL).source == "tencent"
    finally:
        release.set()
        worker.join(5)


def test_research_and_json_fact_bundle_provenance(hybrid):
    hybrid.get_stock_universe()
    scanner = MarketScanner(hybrid)
    snapshot = StockResearchService(hybrid, scanner, {}).load(SYMBOL, 120)
    assert len(snapshot.history) == 120
    assert snapshot.facts["realtime_source"] == "tencent"
    assert snapshot.facts["history_source"] == "hithink"
    assert snapshot.facts["reference_source"] == "hithink"
    assert snapshot.facts["field_provenance"]["total_market_cap"] == "tencent"
    assert snapshot.facts["field_provenance"]["ma5"] == ["tencent", "hithink"]
    assert snapshot.facts["ma5"] != "unavailable"
    json.dumps(build_fact_bundle(SYMBOL, hybrid, scanner), allow_nan=False)


def test_scanner_units_ma_and_existing_sampled_speed(hybrid):
    scanner = MarketScanner(hybrid)
    result = scanner.scan()
    row = result.rows[0]
    assert row["source"] == "tencent" and row["history_source"] == "hithink"
    assert row["turnover_rate"] == 2 and row["volume_ratio"] == 2
    assert row["ma5"] != "unavailable" and scanner.volume_multiplier == 1
    start = NOW.timestamp()
    for minute in range(6):
        scanner.snapshot_history.update(SYMBOL, start + minute*60, {"lastPrice":40 + minute})
    assert scanner.snapshot_history.speed(SYMBOL, 5) == 12.5


def test_feed_closed_snapshot_once(hybrid):
    feed = RealtimeMarketFeed(hybrid, MarketScanner(hybrid), snapshot_cache=None)
    feed.ensure_closed_snapshot("closed", now=NOW)
    count = hybrid.realtime.primary.calls
    rows = feed.ensure_closed_snapshot("closed", now=NOW)
    assert rows and hybrid.realtime.primary.calls == count
    assert rows[0]["source"] == "tencent"


def test_sector_cache_and_reverse_memberships(hybrid, tmp_path):
    calls = []
    def get(endpoint, params):
        calls.append(endpoint)
        rows = [dict(thscode="881101.TI", name="fixture-group")] if endpoint == "catalog" else [dict(thscode=SYMBOL, ticker="600498", name="fixture-stock")]
        return ApiResult("SUCCESS", data={"item": rows})
    from src.market_data.sector_service import SectorService
    hybrid.sectors = SectorService(HithinkSectorProvider(SimpleNamespace(get=get), ReferenceCache(tmp_path, lambda:NOW)))
    hybrid.get_sector_catalog()
    hybrid.get_sector_catalog()
    assert calls == ["catalog"]
    assert hybrid.get_sector_members("881101.TI").status == "SUCCESS"
    assert hybrid.get_sector_members("881101.TI").status == "CACHED"
    assert calls.count("members") == 1
    assert hybrid.get_stock_sectors(SYMBOL)["groups"][0]["index_thscode"] == "881101.TI"


def test_modes_do_not_touch_paid_runtime(monkeypatch):
    monkeypatch.delenv("XTDC_TOKEN", raising=False)
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    p = create_market_provider(mode="hybrid", router_factory=lambda: pytest.fail("paid runtime"))
    assert p.provider_status()["official_auth"] == "AUTH_NOT_CONFIGURED"
    p.close()
    assert source_request({}, {"A_AGENT_MARKET_DATA_MODE":"hybrid"}).preferred_source == "hybrid"
    assert source_request({}, {}).preferred_source == "auto"
    assert market_data_mode({}) == "auto"


@pytest.mark.parametrize("mode,expected", [("auto", "auto"), ("xtdc", "xtdatacenter"), ("qmt", "qmt")])
def test_paid_and_auto_factory_routes_unchanged(mode, expected):
    calls, sentinel = [], object()
    router = SimpleNamespace(select=lambda **k: calls.append(k) or SimpleNamespace(provider=sentinel))
    assert create_market_provider(mode=mode, router_factory=lambda: router) is sentinel
    assert calls == [{"preferred_source":expected}]


def test_history_service_replaces_old_source_and_refreshes(hybrid):
    from src.history_service import HistoricalDataService
    service = HistoricalDataService(hybrid)
    service._store({SYMBOL: history("baostock", 150).frame()})
    result = service.ensure(SYMBOL, 120)
    assert len(result) == 120 and result.attrs["source"] == "hithink"
    assert {s for s in result["source"]} == {"hithink"}


def test_router_hybrid_never_constructs_paid_provider(monkeypatch, hybrid):
    from src.market_data_router import MarketDataRouter
    monkeypatch.setattr("src.market_data.hybrid_provider.HybridMarketDataProvider", lambda: hybrid)
    def forbidden():
        pytest.fail("paid provider constructed")
    router = MarketDataRouter(xtdc_factory=forbidden, qmt_factory=forbidden)
    assert router.select(preferred_source="hybrid").provider is hybrid


def test_offline_app_pages_without_paid_token(monkeypatch, hybrid):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    monkeypatch.setenv("A_AGENT_MARKET_DATA_MODE", "hybrid")
    monkeypatch.setenv("A_AGENT_ACCEPTANCE_MODE", "0")
    monkeypatch.setenv("XTDC_TOKEN", "")
    monkeypatch.setenv("QMT_PATH", "")
    monkeypatch.setattr("src.market_data.hybrid_provider.HybridMarketDataProvider", lambda: hybrid)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py"))
    for page in ("总览", "实时行情", "个股研究"):
        app.session_state["nav_page"] = page
        app.run(timeout=30)
        assert not list(app.exception), page
    # The injected official client has no key and forbids HTTP; pages still render.
    assert any("AUTH_NOT_CONFIGURED" in str(w.value) for w in app.warning)
    rendered = "\n".join(m.value for m in app.markdown)
    assert "2000股" in rendered and "2000手" not in rendered


def test_background_index_cannot_block_full_realtime(hybrid):
    hybrid.get_stock_universe()
    entered, release = Event(), Event()
    def blocked(*a):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(capability_status="RATE_LIMITED")
    hybrid.official.index_snapshot = blocked
    try:
        assert hybrid.cached_index_snapshot() is None
        assert entered.wait(2)
        assert hybrid.snapshot_all().source == "tencent"
    finally:
        release.set()
        hybrid._index_worker.join(5)


def test_official_quality_ignores_only_gaps_outside_requested_tail(tmp_path):
    official = HithinkOfficialProvider(HithinkRestClient(api_key=""), tmp_path, clock=lambda:NOW)
    result = history(count=130)
    outside = (result.bars[0].trade_date - timedelta(days=1)).isoformat()
    result.quality_status, result.data_status = "DEGRADED", "PARTIAL"
    result.warnings = ["missing_session:" + outside]
    official.history.daily = lambda *a, **k: result
    assert official.history.load(SYMBOL,120).quality_status == "VALID"
    result.warnings = ["missing_session:" + result.bars[-2].trade_date.isoformat()]
    result.quality_status = "DEGRADED"
    other = HithinkOfficialProvider(HithinkRestClient(api_key=""), tmp_path/"other", clock=lambda:NOW)
    other.history.daily = lambda *a, **k: result
    assert other.history.load(SYMBOL,120).quality_status == "DEGRADED"
