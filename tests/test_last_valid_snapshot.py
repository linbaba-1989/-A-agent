from datetime import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from src.last_valid_snapshot import TOKEN_SOURCE, LastValidSnapshot
from src.realtime_market import RealtimeMarketFeed
from src.streaming_ui import StreamingUI, build_payload
from tests.test_streaming_ui import Provider

CLOSED = datetime(2026, 9, 21, 20)


class TokenProvider(Provider):
    provider_name = TOKEN_SOURCE


def seed(path):
    feed = RealtimeMarketFeed(TokenProvider(), snapshot_cache=path)
    feed.ensure_closed_snapshot('closed', CLOSED)
    return feed


@pytest.mark.parametrize('session,hour', [('closed', 20), ('lunch_break', 12), ('pre_open', 8)])
def test_persisted_snapshot_is_cached_with_prices_without_live_evidence(tmp_path, session, hour):
    path = tmp_path / 'snapshot.json'
    original = seed(path)
    feed = RealtimeMarketFeed(TokenProvider(), snapshot_cache=path)
    assert feed.cached()[0]['lastPrice'] == original.cached()[0]['lastPrice']
    payload = build_payload(feed, feed.cached(), None, CLOSED.replace(hour=hour))
    assert payload['status']['market_session'] == session
    assert payload['status']['quote_status'] == 'CACHED'
    assert payload['target']['lastPrice'] == 10.01
    assert len(payload['top20']) == 2
    assert feed.provider.calls == feed.provider_request_count == feed.quote_evidence.fresh_fetch_count == 0
    stored = json.loads(path.read_text(encoding='utf-8'))
    assert stored['source'] == TOKEN_SOURCE
    assert {'symbol', 'last_price', 'last_close', 'quote_timestamp', 'volume', 'amount',
            'turnover', 'snapshot_seq', 'saved_at', 'source'} <= stored['rows'][0].keys()


def test_closed_one_shot_fills_shared_snapshot_and_does_not_poll(tmp_path):
    feed = RealtimeMarketFeed(TokenProvider(), snapshot_cache=tmp_path / 'snapshot.json')
    service = StreamingUI(feed, clock=lambda: CLOSED)
    try:
        service.tick(False)  # Dynamic mode still works with high-frequency refresh disabled.
        version = service.bus.version
        for _ in range(8):
            service.tick(True)
        assert feed.provider.calls == feed.provider_request_count == 1
        assert feed.cached(['600498.SH'])[0]['lastPrice'] == 10.01
        assert service.bus.latest['status']['quote_status'] == 'CACHED'
        assert service.bus.latest['from_cache']
        assert service.bus.version == version  # No repeated quote events.
    finally:
        service.close()


def test_restart_replays_before_one_shot_refresh_and_preserves_cache_on_failure(tmp_path):
    path = tmp_path / 'snapshot.json'
    seed(path)
    entered, release = Event(), Event()

    class Blocked(TokenProvider):
        def get_full_ticks(self, symbols):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            raise RuntimeError('offline')

    feed = RealtimeMarketFeed(Blocked(), snapshot_cache=path)
    service = StreamingUI(feed, clock=lambda: CLOSED)
    try:
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(service.tick, True)
            assert entered.wait(5)
            assert service.bus.latest['target']['lastPrice'] == 10.01
            assert service.bus.latest['status']['quote_status'] == 'CACHED'
            release.set()
            pending.result()
        service.tick(True)
        assert feed.provider.calls == 1
        assert LastValidSnapshot(path).load()[0]['lastPrice'] == 10.01
    finally:
        release.set()
        service.close()


def test_qmt_cannot_load_or_overwrite_token_cache(tmp_path):
    path = tmp_path / 'snapshot.json'
    seed(path)
    before = path.read_bytes()
    qmt = Provider()
    qmt.provider_name = 'QMT Local'
    feed = RealtimeMarketFeed(qmt, snapshot_cache=path)
    assert feed.cached() == []
    feed.snapshot(force=True)
    assert path.read_bytes() == before
    rows = feed.cached()
    assert not LastValidSnapshot(path).save(rows)
    payload = json.loads(before)
    payload['rows'][0]['source'] = 'QMT Local'
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert LastValidSnapshot(path).load() == []


def test_no_cache_provider_unavailable_is_unavailable_without_retries(tmp_path):
    class Unavailable(TokenProvider):
        def get_full_ticks(self, symbols):
            self.calls += 1
            raise RuntimeError('offline')

    feed = RealtimeMarketFeed(Unavailable(), snapshot_cache=tmp_path / 'absent.json')
    for _ in range(4):
        feed.ensure_closed_snapshot('closed', CLOSED)
    payload = build_payload(feed, feed.cached(), None, CLOSED)
    assert payload['status']['quote_status'] == 'UNAVAILABLE'
    assert payload['target'] is None
    assert feed.provider.calls == 1


def test_offline_restart_uses_token_cache_without_provider(tmp_path):
    path = tmp_path / 'snapshot.json'
    seed(path)
    feed = RealtimeMarketFeed(None, snapshot_cache=path)
    service = StreamingUI(feed, clock=lambda: CLOSED)
    try:
        service.tick(True)
        assert service.bus.latest['status']['quote_status'] == 'CACHED'
        assert service.bus.latest['target']['lastPrice'] == 10.01
        assert feed.provider_request_count == 0
    finally:
        service.close()


def test_single_symbol_fetch_cannot_create_full_market_cache(tmp_path):
    path = tmp_path / 'snapshot.json'
    feed = RealtimeMarketFeed(TokenProvider(), snapshot_cache=path)
    rows = feed.snapshot(['600498.SH'], force=True)
    feed.enrich_static(rows)
    assert not path.exists()


def test_empty_refresh_does_not_destroy_last_valid_snapshot(tmp_path):
    path = tmp_path / 'snapshot.json'
    seed(path)
    before = path.read_bytes()

    class Empty(TokenProvider):
        def get_full_ticks(self, symbols):
            self.calls += 1
            return {}

    feed = RealtimeMarketFeed(Empty(), snapshot_cache=path)
    feed.ensure_closed_snapshot('closed', CLOSED)
    assert build_payload(feed, feed.cached(), None, CLOSED)['status']['quote_status'] == 'CACHED'
    assert path.read_bytes() == before


def test_corrupt_cache_and_no_provider_stay_unavailable(tmp_path):
    path = tmp_path / 'snapshot.json'
    path.write_text('{broken', encoding='utf-8')
    feed = RealtimeMarketFeed(None, snapshot_cache=path)
    payload = build_payload(feed, feed.cached(), None, CLOSED)
    assert payload['status']['quote_status'] == 'UNAVAILABLE'
    assert payload['top20'] == []


def test_restart_preserves_proven_speeds_only_for_identical_closing_quote(tmp_path):
    from src.market_clock import to_beijing

    class Closing(TokenProvider):
        minute = 0

        def get_full_ticks(self, symbols):
            self.calls += 1
            return {symbol: {'lastPrice': 10 + self.minute, 'lastClose': 10,
                             'time': to_beijing(CLOSED.replace(hour=14, minute=self.minute)).timestamp() * 1000}
                    for symbol in symbols}

    path = tmp_path / 'snapshot.json'
    provider = Closing()
    feed = RealtimeMarketFeed(provider, snapshot_cache=path)
    feed.snapshot(force=True, now=CLOSED)
    provider.minute = 5
    feed.snapshot(force=True, now=CLOSED)
    expected = feed.cached()[0]['speed_5m']
    assert expected == 50
    restored = RealtimeMarketFeed(provider, snapshot_cache=path)
    restored.ensure_closed_snapshot('closed', CLOSED)
    assert restored.cached()[0]['speed_5m'] == expected
    provider.minute = 6
    newer = RealtimeMarketFeed(provider, snapshot_cache=path)
    newer.ensure_closed_snapshot('closed', CLOSED)
    assert newer.cached()[0]['speed_5m'] == 'unavailable'
