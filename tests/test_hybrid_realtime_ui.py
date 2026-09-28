from datetime import datetime
from types import SimpleNamespace

import pytest

from src.acceptance_source import shared_beta_feed
from src.last_valid_snapshot import LastValidSnapshot
from src.market_clock import to_beijing
from src.market_data.contracts import MarketSnapshot
from src.realtime_market import RealtimeMarketFeed
from src.realtime_presentation import beta_unavailable_message, presentation_rows, supports_realtime
from src.streaming_ui import StreamingUI, build_payload

NOW = to_beijing(datetime(2026, 9, 28, 10, 0, 5))


class Hybrid:
    hybrid = True
    provider_name = "HybridMarketDataProvider"

    def __init__(self):
        self.source = "tencent"
        self.time = NOW.timestamp()-1
        self.realtime = SimpleNamespace(provider_name="FreeMarketDataProvider", active_source="tencent",
            primary=SimpleNamespace(source="tencent"), fallback=SimpleNamespace(source="sina"))

    def get_stock_universe(self):
        return ["600498.SH"]

    def get_full_ticks(self, symbols):
        return {s: dict(lastPrice=42, lastClose=40, time=self.time*1000,
                       source=self.source, quote_status="LIVE", volume_unit="shares") for s in symbols}

    def _valid_tick(self, tick, require_time=True):
        return bool(tick and tick.get("lastPrice"))

    def tick_timestamp(self, tick):
        return tick['time']/1000

    def normalized_instrument(self, symbol):
        return {"name": "烽火通信"}


def context(provider):
    feed = RealtimeMarketFeed(provider, snapshot_cache=None)
    selection = SimpleNamespace(provider=provider, name="Hybrid", required_source=None)
    return dict(provider=provider, realtime_feed=feed, selection=selection)


def test_hybrid_dynamic_feed_does_not_require_xtdc():
    ctx = context(Hybrid())
    assert shared_beta_feed(ctx) is ctx['realtime_feed']
    ctx['selection'].name = 'arbitrary_future_provider'
    assert shared_beta_feed(ctx) is ctx['realtime_feed']
    ctx['selection'].provider = Hybrid()
    assert shared_beta_feed(ctx) is None


def test_hybrid_does_not_load_xtdc_last_valid_cache(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail('Hybrid tried to read Token disk cache')
    monkeypatch.setattr(LastValidSnapshot, 'load', forbidden)
    feed = RealtimeMarketFeed(Hybrid(), snapshot_cache=tmp_path/'legacy.json')
    assert feed.cached() == []
    assert feed._snapshot_store is None


def test_hybrid_realtime_banner_uses_actual_provider():
    ctx = context(Hybrid())
    message = beta_unavailable_message(ctx)
    assert 'Hybrid' in message and 'UNAVAILABLE' in message
    assert 'XtDataCenter' not in message


def test_old_trade_date_not_current_cached_during_session():
    provider = Hybrid()
    provider.time = to_beijing(datetime(2026,9,23,15)).timestamp()
    feed = RealtimeMarketFeed(provider)
    rows = feed.snapshot(now=NOW, market_session='open')
    payload = build_payload(feed, rows, None, NOW)
    assert payload['top20'] == [] and payload['target'] is None
    assert payload['status']['quote_status'] == 'UNAVAILABLE'
    assert payload['status']['source'] == 'tencent'


@pytest.mark.parametrize('source', ['tencent', 'sina'])
def test_hybrid_sse_provenance(source):
    p = Hybrid()
    p.source = source
    feed = RealtimeMarketFeed(p)
    rows = feed.snapshot(now=NOW, market_session='open')
    payload = build_payload(feed, rows, None, NOW)
    target = payload['target']
    assert target['market_data_mode'] == 'hybrid'
    assert target['provider'] == 'HybridMarketDataProvider'
    assert target['realtime_provider'] == 'FreeMarketDataProvider'
    assert target['source'] == source
    assert target['trade_date'] == '2026-09-28'
    assert target['quote_status'] == 'LIVE'
    assert payload['top20'][0] == target


def test_injected_legacy_cache_cannot_reach_hybrid_payload():
    feed = RealtimeMarketFeed(Hybrid())
    rows = feed.snapshot(now=NOW, market_session='open')
    rows[0]['source'] = 'XtDataCenter Token'
    feed._latest_rows = {r['symbol']:r for r in rows}
    payload = build_payload(feed, feed.cached(), None, NOW)
    assert payload['target'] is None and payload['top20'] == []
    assert payload['status']['rejected_rows'] == 1
    assert payload['status']['quote_status'] == 'UNAVAILABLE'


def test_midnight_cache_rollover_does_not_replay_yesterday():
    feed = RealtimeMarketFeed(Hybrid())
    feed.snapshot(now=NOW, market_session='open')
    tomorrow = to_beijing(datetime(2026,9,29,10))
    payload = build_payload(feed, feed.cached(), None, tomorrow)
    assert payload['top20'] == []
    assert payload['status']['last_quote_time'] is None


def test_batch_stale_not_promoted_by_sse_timestamp():
    feed = RealtimeMarketFeed(Hybrid())
    rows = feed.snapshot(now=NOW, market_session='open')
    rows[0]['quote_status'] = 'STALE'
    assert build_payload(feed, rows, None, NOW)['status']['quote_status'] == 'STALE'


def test_free_snapshot_capability_consumed_without_legacy_tick_interface():
    p = SimpleNamespace(provider_name='FreeMarketDataProvider', get_stock_universe=lambda:['600498.SH'],
        primary=SimpleNamespace(source='tencent'), fallback=SimpleNamespace(source='sina'))
    p.snapshot = lambda symbols: SimpleNamespace(snapshots={'600498.SH':MarketSnapshot(
        symbol='600498.SH',price=42,prev_close=40,quote_time=NOW,source='tencent',quote_status='CACHED')})
    assert supports_realtime(p)
    feed = RealtimeMarketFeed(p)
    rows = feed.snapshot(now=NOW,market_session='open')
    assert rows[0]['market_data_mode'] == 'free'
    assert rows[0]['source'] == 'tencent'


def test_hybrid_sse_service_uses_same_provider():
    p = Hybrid()
    feed = RealtimeMarketFeed(p)
    service = StreamingUI(feed,clock=lambda:NOW)
    try:
        service.tick(True)
        assert service.feed.provider is p
        assert service.bus.latest['target']['source'] == 'tencent'
    finally:
        service.close()


def test_hybrid_unavailable_apptest_never_constructs_offline_feed(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import ui.components.streaming_beta as beta
    monkeypatch.setattr(beta, 'offline_token_feed', lambda:pytest.fail('legacy offline feed constructed'))
    app = AppTest.from_string('''
from types import SimpleNamespace
from ui.components.streaming_beta import render_streaming_beta
render_streaming_beta(dict(selection=SimpleNamespace(provider=None,name="Hybrid",required_source=None),
                          provider=None,realtime_feed=None))
''').run()
    assert not app.exception
    assert 'Hybrid' in app.info[0].value
    assert 'XtDataCenter' not in app.info[0].value
