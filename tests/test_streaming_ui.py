from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import urlopen

from src.realtime_market import RealtimeMarketFeed
from src.streaming_ui import SnapshotBus, StreamingUI, build_payload
from src.market_clock import to_beijing


class Provider:
    def __init__(self):
        self.calls = 0

    def get_stock_universe(self):
        return ['600498.SH', '600000.SH']

    def get_full_ticks(self, symbols):
        self.calls += 1
        return {code: {'lastPrice': 10 + self.calls / 100, 'lastClose': 10,
                       'time': to_beijing(datetime(2026, 9, 16, 10, 0, self.calls)).timestamp() * 1000}
                for code in symbols}

    def _valid_tick(self, tick, require_time=False):
        return True

    def tick_timestamp(self, tick):
        return tick['time'] / 1000

    def normalized_instrument(self, code):
        return {'name': code}


def test_bus_keeps_only_latest_and_deduplicates_unchanged_snapshots():
    bus = SnapshotBus()
    for seq in range(1000):
        bus.publish({'snapshot_seq': seq})
    version, payload = bus.read(timeout=0)
    bus.publish(payload.copy())
    assert bus.version == version == 1000
    assert bus.latest == {'snapshot_seq': 999}


def test_stream_reuses_provider_and_status_policy_through_session_boundaries():
    provider = Provider()
    feed = RealtimeMarketFeed(provider)
    feed._minimum_request_gap = 0
    clock = [datetime(2026, 9, 16, 10, 0, 2)]
    service = StreamingUI(feed, clock=lambda: clock[0])
    try:
        service.tick(True)
        service.tick(True)
        assert service.feed.provider is provider
        assert provider.calls == feed.provider_request_count == 2
        assert service.bus.latest['provider_init_count'] == 1
        assert service.bus.latest['status']['quote_status'] == 'LIVE'
        assert service.bus.latest['snapshot_seq'] == 2
        previous = feed.last_quote_timestamp
        rows = feed.cached()
        assert build_payload(feed, rows, previous, clock[0])['status']['quote_status'] == 'LIVE'
        for hour in [12, 16]:
            clock[0] = datetime(2026, 9, 16, hour, 0)
            service.tick(True)
            assert service.bus.latest['status']['quote_status'] == 'CACHED'
            assert service.bus.latest['from_cache'] is True
        assert provider.calls == 2
    finally:
        service.close()


def test_empty_closed_feed_is_unavailable_and_never_fetches():
    provider = Provider()
    service = StreamingUI(RealtimeMarketFeed(provider), clock=lambda: datetime(2026, 9, 19, 10))
    try:
        service.tick(True)
        assert service.bus.latest['status']['quote_status'] == 'UNAVAILABLE'
        assert service.bus.latest['top20'] == []
        assert service.bus.latest['target'] is None
        assert provider.calls == 0
    finally:
        service.close()


def test_sse_disconnect_reconnect_replays_latest_without_provider_init_or_fetch():
    provider = Provider()
    service = StreamingUI(RealtimeMarketFeed(provider), clock=lambda: datetime(2026, 9, 19, 10))
    try:
        service.tick(False)
        endpoint = service.url.replace('index.html', 'events') + '?enabled=0'
        messages = []
        for _ in range(2):
            with urlopen(endpoint, timeout=5) as response:
                assert response.headers['Content-Type'] == 'text/event-stream'
                for _ in range(20):
                    line = response.readline().decode()
                    if line.startswith('data: '):
                        messages.append(json.loads(line[6:]))
                        break
        assert len(messages) == 2
        assert messages[0]['snapshot_seq'] == messages[1]['snapshot_seq'] == 0
        assert messages[0]['event_id'] == messages[1]['event_id']
        assert messages[1]['provider_init_count'] == 1
        assert provider.calls == 0
    finally:
        service.close()


def test_browser_incremental_renderer_and_reconnection_contract():
    node = shutil.which('node')
    assert node, 'Node is required to execute the actual browser renderer tests'
    result = subprocess.run([node, str(Path(__file__).with_name('streaming_renderer.mjs'))],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bus_event_clock_is_bound_to_published_version_not_read_time():
    bus = SnapshotBus()
    bus.publish({'snapshot_seq': 1})
    first = bus.read_event(timeout=0)
    bus.publish({'snapshot_seq': 1})
    assert bus.read_event(timeout=0) == first
    bus.publish({'snapshot_seq': 2})
    second = bus.read_event(timeout=0)
    assert second[0] == first[0] + 1
    assert second[2] >= first[2] and second[3] >= first[3]


def test_ten_real_http_disconnects_release_clients_without_queues():
    provider = Provider()
    service = StreamingUI(RealtimeMarketFeed(provider), clock=lambda: datetime(2026, 9, 19, 10))
    try:
        service.tick(False)
        for _ in range(10):
            with urlopen(service.url.replace('index.html','events')+'?enabled=0', timeout=5) as response:
                while not response.readline().startswith(b'data: '):
                    pass
        deadline = time.monotonic() + 6
        while service.connection_stats()['active_clients'] and time.monotonic() < deadline:
            time.sleep(.1)
        stats = service.connection_stats()
        assert stats['active_clients'] == 0
        assert stats['connected_total'] == stats['disconnected_total'] == 10
        assert stats['per_client_queue_size'] == 0
        assert stats['bus_slots'] == 1
        assert service.feed.provider_initializations == 1
    finally:
        service.close()


def test_diagnostics_are_opt_in_and_event_ring_is_bounded(monkeypatch):
    monkeypatch.delenv('A_AGENT_STREAM_DIAGNOSTICS', raising=False)
    service = StreamingUI(RealtimeMarketFeed(Provider()))
    try:
        assert service.probe is None
        for seq in range(1000):
            service.sent_events.append({'snapshot_seq':seq})
        assert len(service.sent_events) == 300
    finally:
        service.close()


def test_opt_in_http_diagnostics_report_real_allocations_and_bounded_counts(monkeypatch):
    monkeypatch.setenv('A_AGENT_STREAM_DIAGNOSTICS', '1')
    service = StreamingUI(RealtimeMarketFeed(Provider()), clock=lambda: datetime(2026, 9, 16, 10, 0, 2))
    try:
        service.tick(True)
        with urlopen(service.url.replace('index.html','diagnostics')+'?sample=1', timeout=5) as response:
            result = json.load(response)
        sample = result['sample']
        assert sample['python_allocated_bytes'] > 0
        assert sample['rss_bytes'] > 0
        assert len(sample['top_growth']) <= 10
        assert sample['snapshot_per_symbol_limit'] is None
        assert sample['history_retention_seconds'] == 360
        assert sample['history_retained_realtime_points'] == 0
        assert sample['snapshot_points'] == 2
        assert sample['event_buffer_limit'] == 200
        assert sample['active_clients'] == sample['per_client_queue_size'] == 0
        assert service.probe.samples.maxlen == 60
        trace = service.bus.latest['trace']
        assert trace['feed_snapshot_seq'] == service.bus.latest['snapshot_seq']
        assert trace['provider_raw_max_quote_time'] == trace['feed_quote_time']
        assert trace['request_id'] == 1
    finally:
        service.close()
