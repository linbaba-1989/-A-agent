from types import SimpleNamespace

import pytest

from src.market_data_router import MarketDataRouter


class Provider:
    def __init__(self, configured=True, ok=True, message="status"):
        self.configured = configured
        self.ok = ok
        self.message = message
        self.closed = False

    def check_connection(self):
        return SimpleNamespace(ok=self.ok, message=self.message)

    def connection_diagnostics(self):
        return SimpleNamespace(connected=self.ok, message=self.message)

    def close(self):
        self.closed = True


def test_token_provider_is_primary():
    token, qmt = Provider(), Provider()
    selection = MarketDataRouter(lambda: token, lambda: qmt).select()
    assert selection.provider is token and not selection.fallback
    assert selection.qmt_fallback_status == "configured / available"


def test_token_primary_reports_unavailable_qmt_fallback():
    token, qmt = Provider(), Provider(ok=False)
    selection = MarketDataRouter(lambda: token, lambda: qmt).select()
    assert selection.provider is token
    assert selection.qmt_fallback_status == "configured / unavailable"
    assert qmt.closed


def test_token_failure_falls_back_to_qmt():
    token, qmt = Provider(ok=False, message="token failed"), Provider()
    selection = MarketDataRouter(lambda: token, lambda: qmt).select()
    assert selection.provider is qmt and selection.fallback and token.closed
    assert selection.qmt_fallback_status == "configured / available"


def test_both_failed_stops_market_data():
    selection = MarketDataRouter(lambda: Provider(ok=False), lambda: Provider(ok=False)).select()
    assert selection.provider is None and selection.status == "failed"
    assert selection.qmt_fallback_status == "configured / unavailable"
    with pytest.raises(RuntimeError):
        MarketDataRouter(lambda: Provider(ok=False), lambda: Provider(ok=False)).require_provider()
