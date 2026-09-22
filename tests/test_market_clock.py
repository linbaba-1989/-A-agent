from datetime import date, datetime
from time import perf_counter
from types import SimpleNamespace

from src.market_clock import (AShareTradingCalendar, AUCTION, CLOSED, LUNCH_BREAK, OPEN, PRE_OPEN,
                              market_session, quote_status, should_fetch_quotes)
from src.realtime_market import RealtimeMarketFeed, refresh_interval_seconds
from ui.view_models import apply_realtime_quote_status, public_market_status, quote_status_display


def stamp(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def test_market_session_uses_beijing_clock_and_a_share_calendar():
    assert market_session(datetime(2026, 9, 15, 8, 59)) == PRE_OPEN
    assert market_session(datetime(2026, 9, 15, 9, 15)) == AUCTION
    assert market_session(datetime(2026, 9, 15, 9, 45)) == OPEN
    assert market_session(datetime(2026, 9, 15, 11, 30)) == LUNCH_BREAK
    assert market_session(datetime(2026, 9, 15, 13, 0)) == OPEN
    assert market_session(datetime(2026, 9, 15, 15, 0)) == CLOSED
    assert market_session(datetime(2026, 9, 13, 9, 45)) == CLOSED
    assert market_session(datetime(2026, 9, 25, 9, 45)) == CLOSED


def test_calendar_supports_explicit_exchange_overrides():
    calendar = AShareTradingCalendar(non_trading_days={date(2026, 9, 15)},
                                     extra_trading_days={date(2026, 9, 19)})
    assert market_session(datetime(2026, 9, 15, 9, 45), calendar) == CLOSED
    assert market_session(datetime(2026, 9, 19, 9, 45), calendar) == OPEN


def test_quote_status_is_distinct_from_market_session_and_requires_progress():
    now = datetime(2026, 9, 15, 9, 45)
    old = stamp("2026-09-11 15:31:51")
    today = stamp("2026-09-15 09:44:59")
    morning = stamp("2026-09-15 11:29:59")
    assert quote_status(None, None, OPEN, now) == "UNAVAILABLE"
    assert quote_status(None, old, OPEN, now) == "STALE"
    assert quote_status(old, today, OPEN, now) == "STALE"
    assert quote_status(today, today + 1, OPEN, now) == "LIVE"
    assert quote_status(today, today, OPEN, now) == "STALE"
    assert quote_status(morning, morning, OPEN, datetime(2026, 9, 15, 13, 1)) == "STALE"
    assert quote_status(None, old, LUNCH_BREAK, now) == "CACHED"
    assert should_fetch_quotes(AUCTION)
    assert should_fetch_quotes(OPEN)
    assert not should_fetch_quotes(LUNCH_BREAK)
    assert refresh_interval_seconds(LUNCH_BREAK, True, 2) is None


class StaleProvider:
    def __init__(self):
        self.calls = 0

    def get_stock_universe(self):
        return ["A"]

    def get_full_ticks(self, symbols):
        self.calls += 1
        return {"A": {"lastPrice": 11, "lastClose": 10,
                       "time": stamp("2026-09-11 15:31:51") * 1000}}

    def _valid_tick(self, tick):
        return bool(tick)

    def tick_timestamp(self, tick):
        return tick["time"] / 1000


def test_open_session_ignores_old_cache_and_forces_provider_fetch():
    provider = StaleProvider()
    feed = RealtimeMarketFeed(provider)
    feed._latest_rows["A"] = {"symbol": "A", "quote_timestamp": stamp("2026-09-11 15:31:51"),
                               "quote_time": "2026-09-11T15:31:51", "lastPrice": 11}
    feed._last_request_finished = perf_counter()

    rows = feed.ensure_fresh_provider_probe(OPEN, now=datetime(2026, 9, 15, 9, 45))

    assert provider.calls == 1
    assert feed.provider_request_count == 1
    assert rows[0]["from_cache"] is False
    assert rows[0]["quote_time"].startswith("2026-09-11T15:31:51")

    feed.ensure_fresh_provider_probe(OPEN, now=datetime(2026, 9, 15, 13, 0))
    assert provider.calls == 2


def test_lunch_and_closed_sessions_only_read_cache():
    provider = StaleProvider()
    feed = RealtimeMarketFeed(provider)
    feed._latest_rows["A"] = {"symbol": "A", "quote_timestamp": stamp("2026-09-11 15:31:51"),
                               "quote_time": "2026-09-11T15:31:51", "lastPrice": 11}
    feed.ensure_fresh_provider_probe(LUNCH_BREAK, now=datetime(2026, 9, 15, 11, 45))
    feed.ensure_fresh_provider_probe(CLOSED, now=datetime(2026, 9, 15, 15, 30))
    assert provider.calls == 0


def test_public_market_status_does_not_use_cached_market_status():
    selection = SimpleNamespace(name="XtDataCenter Token", status="connected",
                                qmt_fallback_status="configured / unavailable")
    status = public_market_status(
        selection,
        {"market_status": "closed", "latest_quote_time": "2026-09-11T15:31:51"},
        now=datetime(2026, 9, 15, 9, 45),
    )
    assert status["market_session"] == OPEN
    assert status["market"] == "交易中"
    assert status["quote_status"] == "STALE"
    assert status["last_quote_time"] == "2026-09-11T15:31:51"


def test_realtime_status_compat_wrapper_delegates_to_unified_quote_status():
    selection = SimpleNamespace(name="XtDataCenter Token", status="connected",
                                qmt_fallback_status="configured / unavailable")
    now = datetime(2026, 9, 15, 9, 45)
    status = public_market_status(selection, {"market_status": "closed"}, now=now)
    updated = apply_realtime_quote_status(status, stamp("2026-09-11 15:31:51"), now=now)
    assert updated["market_session"] == OPEN
    assert updated["quote_status"] == "STALE"


def test_unified_quote_status_display_maps_open_lunch_closed_and_empty_evidence():
    old = stamp("2026-09-11 15:31:51")
    previous = stamp("2026-09-15 09:44:00")
    current = stamp("2026-09-15 09:45:00")
    now = datetime(2026, 9, 15, 9, 45)
    base = {"market_session": OPEN, "market": "交易中", "market_status": "open", "quote_status": None,
            "last_quote_time": "unavailable"}

    assert quote_status_display(base, old, now=now, market_session_value=OPEN)["quote_status"] == "STALE"
    assert quote_status_display(base, current, previous, now=now, market_session_value=OPEN)["quote_status"] == "LIVE"
    assert quote_status_display(base, old, now=now, market_session_value=LUNCH_BREAK)["quote_status"] == "CACHED"
    assert quote_status_display(base, old, now=now, market_session_value=CLOSED)["quote_status"] == "CACHED"
    assert quote_status_display(base, None, now=now, market_session_value=OPEN)["quote_status"] == "UNAVAILABLE"
