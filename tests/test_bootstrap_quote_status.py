from datetime import datetime, timedelta

import pytest

from src.market_clock import (QuoteEvidence, OPEN, LUNCH_BREAK, CLOSED,
                              QUOTE_GRACE_SECONDS, to_beijing)
from src.realtime_market import RealtimeMarketFeed
from ui.view_models import quote_status_display


NOW = to_beijing(datetime(2026, 9, 21, 9, 45))


def test_first_fresh_recent_market_quote_is_bootstrap_live():
    state = QuoteEvidence()
    state.observe(NOW - timedelta(seconds=1), 5555, 1, OPEN, NOW)
    assert state.status(OPEN, NOW) == 'LIVE'
    assert state.fresh_fetch_count == 1
    assert not state.stop_eligible(OPEN, NOW)


def test_advancing_second_snapshot_stays_live():
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    later = NOW + timedelta(seconds=2)
    state.observe(later, 5555, 2, OPEN, later)
    assert state.status(OPEN, later) == 'LIVE'
    assert state.last_advanced_at == later
    assert state.consecutive_non_advance_count == 0


def test_multiple_unchanged_fetches_require_grace_before_stale_stop():
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    for seq, seconds in enumerate(range(2, QUOTE_GRACE_SECONDS, 2), 2):
        moment = NOW + timedelta(seconds=seconds)
        state.observe(NOW, 5555, seq, OPEN, moment)
        assert state.status(OPEN, moment) == 'LIVE'
        assert not state.stop_eligible(OPEN, moment)
    end = NOW + timedelta(seconds=QUOTE_GRACE_SECONDS)
    state.observe(NOW, 5555, 100, OPEN, end)
    assert state.status(OPEN, end) == 'STALE'
    assert state.stop_eligible(OPEN, end)


@pytest.mark.parametrize('session', [LUNCH_BREAK, CLOSED])
def test_non_trading_session_is_cached(session):
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    assert state.status(session, NOW) == 'CACHED'


def test_empty_success_is_unavailable_even_after_live():
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    state.observe(None, 0, 2, OPEN, NOW)
    assert state.status(OPEN, NOW) == 'UNAVAILABLE'


def test_old_day_cannot_bootstrap_live_or_stop_on_first_frame():
    state = QuoteEvidence()
    state.observe(NOW - timedelta(days=3), 5555, 1, OPEN, NOW)
    assert state.status(OPEN, NOW) == 'STALE'
    assert not state.stop_eligible(OPEN, NOW + timedelta(seconds=20))


def test_duplicate_seq_and_readers_do_not_count_as_fresh_fetches():
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    state.observe(NOW, 5555, 1, OPEN, NOW + timedelta(seconds=20))
    for _ in range(10):
        state.status(OPEN, NOW)
    assert state.fresh_fetch_count == 1
    assert state.consecutive_non_advance_count == 0
    assert not state.stop_eligible(OPEN, NOW + timedelta(seconds=20))


def test_afternoon_requires_new_evidence_and_resets_grace():
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    afternoon = NOW.replace(hour=13, minute=0)
    assert state.status(OPEN, afternoon) != 'LIVE'
    state.observe(afternoon, 5555, 2, OPEN, afternoon)
    assert state.status(OPEN, afternoon) == 'LIVE'
    assert state.fresh_fetch_count == 1


class Provider:
    def __init__(self):
        self.calls = 0
        self.moment = NOW

    def get_stock_universe(self):
        return ['600498.SH', '600000.SH']

    def get_full_ticks(self, symbols):
        self.calls += 1
        return {symbol: {'lastPrice': 10, 'lastClose': 10,
                         'time': (NOW if symbol == '600498.SH' else self.moment).timestamp() * 1000}
                for symbol in symbols}

    def _valid_tick(self, tick, require_time=False):
        return True

    def tick_timestamp(self, tick):
        return tick['time'] / 1000


def test_shared_feed_market_progress_not_idle_symbol_drives_every_display():
    provider = Provider()
    feed = RealtimeMarketFeed(provider)
    feed.ensure_fresh_provider_probe(OPEN, now=NOW)
    assert provider.calls == 1
    initial = quote_status_display({}, now=NOW, market_session_value=OPEN, feed=feed)
    assert initial['quote_status'] == 'LIVE'
    provider.moment = NOW + timedelta(seconds=2)
    feed.snapshot(force=True, market_session=OPEN, now=provider.moment)
    feed.snapshot(['600498.SH'], force=True, market_session=OPEN, now=provider.moment)
    display = quote_status_display({}, NOW.timestamp(), now=provider.moment,
                                   market_session_value=OPEN, feed=feed)
    assert display['quote_status'] == 'LIVE'
    assert display['last_quote_timestamp'] == provider.moment.timestamp()
    assert feed.quote_evidence.fresh_fetch_count == 2
    assert feed.last_request_diagnostic['quote_status'] == display['quote_status']


def test_recent_timestamp_without_fresh_evidence_is_not_live():
    status = quote_status_display({}, NOW, now=NOW, market_session_value=OPEN)
    assert status['quote_status'] == 'CACHED'


def test_cache_aging_without_new_fresh_requests_cannot_confirm_freeze():
    state = QuoteEvidence()
    state.observe(NOW, 5555, 1, OPEN, NOW)
    later = NOW + timedelta(seconds=QUOTE_GRACE_SECONDS + 1)
    state.observe(later, 5555, 2, OPEN, later)
    # Two advancing fresh frames followed only by idle readers: old data, but
    # no evidence that fresh provider requests have stopped advancing.
    assert not state.stop_eligible(OPEN, later + timedelta(seconds=20))
