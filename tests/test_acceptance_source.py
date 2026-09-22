from datetime import datetime, timedelta
from threading import Lock
from types import SimpleNamespace

import pytest

from src.acceptance_source import (SourceRequest, SourceResources, acceptance_preflight,
                                  acceptance_running, continuous_seconds_remaining,
                                  shared_beta_feed, source_request)
from src.market_clock import AShareTradingCalendar, to_beijing
from src.market_data_router import MarketDataRouter


class Provider:
    def __init__(self, configured=True, ok=True):
        self.configured, self.ok = configured, ok
        self.closed = False
        self.checks = 0

    def check_connection(self):
        self.checks += 1
        return SimpleNamespace(ok=self.ok, message="unavailable")

    def connection_diagnostics(self):
        return SimpleNamespace(connected=self.ok, message="unavailable")

    def close(self):
        self.closed = True


def no_qmt():
    pytest.fail("QMT factory must never run in locked acceptance")


def make_feed(provider, scanner=None):
    return SimpleNamespace(provider=provider, provider_initializations=1, _lock=Lock())


def locked(token=None):
    token = token or Provider()
    selection = MarketDataRouter(lambda: token, no_qmt).select(acceptance_source="xtdatacenter")
    return selection, make_feed(token)


def test_a_lock_uses_token_and_never_checks_qmt():
    selection, feed = locked()
    assert selection.name == 'XtDataCenter Token'
    assert selection.provider is feed.provider
    assert selection.required_source == selection.name
    assert selection.token_configured
    assert selection.fallback_enabled is selection.fallback is False
    assert selection.qmt_fallback_status == 'disabled'


@pytest.mark.parametrize('configured,ok', [(False, True), (True, False)])
def test_b_unavailable_token_blocks_without_qmt_even_when_qmt_would_work(configured, ok):
    token = Provider(configured, ok)
    selection = MarketDataRouter(lambda: token, no_qmt).select(acceptance_source='xtdatacenter')
    assert selection.provider is None
    assert selection.status == 'SOURCE_BLOCKED'
    assert token.closed
    assert not selection.fallback_enabled


def test_exception_is_fail_closed_and_message_does_not_expose_token():
    class Broken(Provider):
        def check_connection(self):
            raise RuntimeError('secret-token-string')
    token = Broken()
    selection = MarketDataRouter(lambda: token, no_qmt).select(acceptance_source='xtdatacenter')
    assert selection.status == 'SOURCE_BLOCKED'
    assert selection.reason == 'xtdc_check_failed:RuntimeError'
    assert token.closed


def test_c_normal_user_can_manually_choose_qmt():
    qmt = Provider()
    selection = MarketDataRouter(no_qmt, lambda: qmt).select(preferred_source='qmt')
    assert selection.provider is qmt
    assert selection.name == 'QMT Local'
    assert not selection.fallback  # explicit selection, not fallback


def test_d_beta_uses_exact_shared_source_and_rejects_mismatched_feed():
    selection, feed = locked()
    ctx = {'selection':selection, 'realtime_feed':feed}
    assert shared_beta_feed(ctx) is feed
    ctx['realtime_feed'] = make_feed(Provider())
    assert shared_beta_feed(ctx) is None


def test_e_policy_and_owner_survive_reruns_without_reinitializing():
    token = Provider()
    owner = SourceResources(lambda: MarketDataRouter(lambda: token, no_qmt), lambda p: object(), make_feed)
    state = {'acceptance_mode':True, 'acceptance_source':'xtdatacenter'}
    owner.activate(source_request(state, {}))
    first_feed = owner.feed
    for _ in range(5):
        owner.activate(source_request(state, {}))
        assert owner.feed is first_feed
    assert token.checks == 1
    assert first_feed.provider_initializations == 1
    owner.close()


def test_acceptance_requires_explicit_mode_and_normal_ignores_legacy_locks():
    state = {'market_source':'qmt', 'acceptance_source':'xtdatacenter'}
    assert source_request(state, {}) == SourceRequest('qmt')
    assert source_request(state, {'A_AGENT_ACCEPTANCE_SOURCE':'xtdatacenter',
                                  'A_AGENT_STREAM_DIAGNOSTICS':'1'}) == SourceRequest('qmt')
    assert source_request({**state, 'acceptance_mode':True}, {}) == SourceRequest('xtdatacenter','xtdatacenter')
    assert source_request(state, {'A_AGENT_ACCEPTANCE_MODE':'1'}) == SourceRequest('xtdatacenter','xtdatacenter')
    assert source_request({**state, 'acceptance_mode':False},
                          {'A_AGENT_ACCEPTANCE_MODE':'1'}) == SourceRequest('qmt')


def test_entering_lock_reuses_already_connected_token_provider():
    token, qmt = Provider(), Provider()
    owner = SourceResources(lambda: MarketDataRouter(lambda: token, lambda: qmt), lambda p: object(), make_feed)
    owner.activate(SourceRequest())
    first = owner.feed
    owner.activate(SourceRequest('xtdatacenter', 'xtdatacenter'))
    assert owner.feed is first and owner.feed.provider is token
    assert token.checks == 1 and not token.closed
    assert owner.selection.fallback_enabled is False
    owner.close()


def test_acceptance_probe_failure_keeps_shared_production_feed():
    token = Provider()
    owner = SourceResources(lambda: MarketDataRouter(lambda: token, no_qmt), lambda p: object(), make_feed)
    request = SourceRequest('xtdatacenter','xtdatacenter')
    owner.activate(request)
    token.ok = False
    assert not owner.verify_acceptance_connection()
    assert not token.closed
    assert owner.selection.status == 'connected' and owner.feed.provider is token
    owner.activate(request)  # a rerun is not an implicit retry
    assert token.checks == 2
    owner.close()


def test_closed_token_backend_is_not_reinitialized_by_preflight_start_check():
    token = Provider()
    owner = SourceResources(lambda: MarketDataRouter(lambda: token, no_qmt), lambda p: object(), make_feed)
    owner.activate(SourceRequest('xtdatacenter','xtdatacenter'))
    token.backend = None
    assert not owner.verify_acceptance_connection()
    assert token.checks == 1
    owner.close()


def test_switch_from_qmt_closes_old_stream_before_old_source_then_locks():
    token, qmt = Provider(), Provider()
    events = []
    qmt.close = lambda: events.append('qmt_close')
    owner = SourceResources(lambda: MarketDataRouter(lambda: token, lambda: qmt), lambda p: object(), make_feed)
    owner.activate(SourceRequest('qmt'))
    owner.streaming_service = SimpleNamespace(close=lambda: events.append('stream_close'))
    owner.activate(SourceRequest('xtdatacenter','xtdatacenter'))
    assert events == ['stream_close', 'qmt_close']
    assert owner.selection.provider is token
    assert owner.streaming_service is None
    owner.close()


@pytest.mark.parametrize('clock,seconds', [
    ('09:29:00',0), ('09:30:00',7200), ('11:20:00',600), ('11:20:01',599),
    ('11:30:00',0), ('12:00:00',0), ('13:00:00',7020), ('14:47:00',600),
    ('14:47:01',599), ('14:57:00',0), ('15:00:00',0)])
def test_continuous_window_boundaries(clock, seconds):
    assert continuous_seconds_remaining(datetime.fromisoformat('2026-09-21T'+clock)) == seconds


def test_preflight_requires_every_condition_and_only_reads_evidence():
    selection, feed = locked()
    now = datetime(2026,9,21,9,35)
    result = acceptance_preflight(selection, feed, now=now, workers=1)
    assert result['ready'] and result['status'] == 'READY'
    assert selection.provider.checks == 1  # preflight does not initialize/fetch
    for count in (None,0,2):
        assert not acceptance_preflight(selection, feed, now=now, workers=count)['ready']
    feed.provider_initializations = None
    assert 'PROVIDER_INIT_COUNT_UNAVAILABLE' in acceptance_preflight(selection, feed, now=now, workers=1)['reasons']


@pytest.mark.parametrize('now', [datetime(2026,9,21,11,21), datetime(2026,9,21,14,48),
                               datetime(2026,9,21,15,30), datetime(2026,9,20,10)])
def test_preflight_blocks_short_window_closed_and_non_trading_day(now):
    selection, feed = locked()
    assert not acceptance_preflight(selection, feed, now=now, workers=1)['ready']


def test_preflight_respects_calendar_closure_and_rejects_source_mismatch():
    selection, feed = locked()
    result = acceptance_preflight(selection, feed, now=datetime(2026,9,21,10), workers=1,
                                  calendar=AShareTradingCalendar(non_trading_days={'2026-09-21'}))
    assert 'NOT_TRADING_DAY' in result['reasons']
    selection.fallback_enabled = True
    result = acceptance_preflight(selection, feed, now=datetime(2026,9,21,10), workers=1)
    assert result['status'] == 'SOURCE_BLOCKED'
    assert 'QMT_FALLBACK_NOT_DISABLED' in result['reasons']


def test_ten_minute_entry_grant_survives_rerun_but_not_source_change_or_expiry():
    selection, feed = locked()
    now = to_beijing(datetime(2026,9,21,11,20))
    grant = {'generation':1, 'ends_at':(now+timedelta(minutes=10)).timestamp()}
    later = now + timedelta(minutes=2)
    preflight = acceptance_preflight(selection, feed, now=later, workers=1)
    assert not preflight['ready']
    assert acceptance_running(grant, preflight, 1, later)
    assert not acceptance_running(grant, preflight, 2, later)
    assert not acceptance_running(grant, preflight, 1, now+timedelta(minutes=10))
